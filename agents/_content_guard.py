"""Local checks that prevent stale or duplicate uploads. No network calls."""
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import unicodedata


def load_history(path):
    path = Path(path)
    if not path.exists():
        return []
    try:
        history = json.loads(path.read_text(encoding='utf-8-sig'))
    except (ValueError, OSError) as exc:
        raise ValueError(f'Cannot read {path.name}; repair history before continuing.') from exc
    if not isinstance(history, list) or any(not isinstance(row, dict) for row in history):
        raise ValueError(f'{path.name} must contain a list of records.')
    return history


def write_json_atomic(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent,
                                         prefix=path.name + '.', suffix='.tmp', delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(value, stream, indent=2, ensure_ascii=True)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def file_sha256(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def normalize_title(title):
    title = unicodedata.normalize('NFKC', title).casefold()
    title = re.sub(r'#shorts\b', '', title)
    return ' '.join(re.findall(r'\w+', title))


def short_title(title):
    title = re.sub(r'#shorts\b', '', title, flags=re.I).strip()
    return title[:92].rstrip() + ' #Shorts'


def active_formats(script_record):
    formats = [fmt for fmt in ('short', 'long') if script_record.get(fmt + '_script')]
    if not formats:
        raise ValueError('Latest script has no active formats.')
    return formats


def build_upload_plan(project, scripts, seo_record, qa_report, history):
    """Validate every candidate before authenticating or uploading anything."""
    project = Path(project)
    stamp = scripts.get('timestamp')
    if not stamp or seo_record.get('script_timestamp') != stamp:
        raise ValueError('SEO is not tied to the current script. Run seo_agent.py again.')
    if qa_report.get('passed') is not True or qa_report.get('script_timestamp') != stamp:
        raise ValueError('Current script has no matching successful QA report. Run qa_agent.py.')
    seo = seo_record['seo']
    index = seo.get('recommended_title_index', 0)
    if isinstance(index, bool) or not isinstance(index, int) or not 0 <= index < len(seo['title_variants']):
        raise ValueError('Invalid SEO title selection.')
    title = seo['title_variants'][index]['title'].strip()
    if not normalize_title(title):
        raise ValueError('SEO title is empty.')
    plan = []
    for fmt in active_formats(scripts):
        path = project / 'assets' / 'final_videos' / f'final_{fmt}.mp4'
        if not path.is_file() or not path.stat().st_size:
            raise ValueError(f'Missing or empty {fmt} video.')
        digest = file_sha256(path)
        if qa_report.get('video_sha256', {}).get(fmt) != digest:
            raise ValueError(f'{fmt} video changed since QA or was not checked.')
        final_title = short_title(title) if fmt == 'short' else re.sub(r'#shorts\b', '', title, flags=re.I).strip()[:100]
        for previous in history:
            if previous.get('video_sha256') == digest:
                raise ValueError(f'{fmt} video bytes were already uploaded as {previous.get("video_id")}.')
            if previous.get('format') == fmt and normalize_title(previous.get('title', '')) == normalize_title(final_title):
                raise ValueError(f'{fmt} title already uploaded as {previous.get("video_id")}. Choose a new topic/title.')
        plan.append({'format': fmt, 'path': str(path), 'title': final_title, 'video_sha256': digest})
    return plan
