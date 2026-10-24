"""Refresh public channel metrics and an income-planning report without AI spend."""
import argparse
from collections import Counter
import datetime as dt
import json
import math
from pathlib import Path
import statistics
from _content_guard import normalize_title, write_json_atomic

ROOT = Path(__file__).resolve().parents[1]


def fetch_snapshot(credentials_dir, handle):
    import requests
    from dotenv import dotenv_values
    import os
    key = os.getenv('YOUTUBE_API_KEY') or dotenv_values(Path(credentials_dir) / '.env').get('YOUTUBE_API_KEY')
    if not key:
        raise ValueError('YOUTUBE_API_KEY is not configured.')
    session = requests.Session()

    def get(resource, **params):
        try:
            response = session.get('https://www.googleapis.com/youtube/v3/' + resource,
                                   params={**params, 'key': key}, timeout=30)
            data = response.json()
        except (requests.RequestException, ValueError) as exc:
            raise RuntimeError('YouTube request failed: ' + type(exc).__name__) from None
        if not response.ok:
            reasons = [e.get('reason') for e in data.get('error', {}).get('errors', [])]
            raise RuntimeError(f'YouTube HTTP {response.status_code}; reasons={reasons}')
        return data

    channels = get('channels', part='snippet,statistics,contentDetails', forHandle=handle).get('items', [])
    if len(channels) != 1:
        raise ValueError('Expected one matching channel.')
    channel = channels[0]
    ids, page = [], None
    while True:
        data = get('playlistItems', part='contentDetails', maxResults=50,
                   playlistId=channel['contentDetails']['relatedPlaylists']['uploads'],
                   **({'pageToken': page} if page else {}))
        ids.extend(item['contentDetails']['videoId'] for item in data.get('items', []))
        page = data.get('nextPageToken')
        if not page:
            break
    videos = []
    for offset in range(0, len(ids), 50):
        videos.extend(get('videos', part='snippet,statistics,contentDetails,status',
                          id=','.join(ids[offset:offset + 50])).get('items', []))
    return compact_snapshot({'timestamp': dt.datetime.now(dt.timezone.utc).isoformat(),
                             'source': 'YouTube Data API v3 (public data)', 'channel': channel, 'videos': videos})


def compact_snapshot(snapshot):
    """Retain report inputs rather than duplicating descriptions and thumbnail URLs."""
    return {**snapshot,
            'channel': {'id': snapshot['channel']['id'], 'statistics': snapshot['channel']['statistics']},
            'videos': [{'id': video['id'],
                        'snippet': {key: video['snippet'][key] for key in ('title', 'publishedAt')},
                        'statistics': video.get('statistics', {}),
                        'contentDetails': {'duration': video['contentDetails']['duration']}}
                       for video in snapshot['videos']]}


def required_views(target, rpm, monthly_cost=0):
    if rpm <= 0 or target < 0 or monthly_cost < 0:
        raise ValueError('RPM must be positive; income and costs cannot be negative.')
    return math.ceil((target + monthly_cost) * 1000 / rpm)


def build_report(snapshot, goals):
    videos = snapshot['videos']
    stats = snapshot['channel']['statistics']
    target = goals.get('monthly_income_target_inr')
    lines = [
        '# WildFactsDailyTV growth report', '',
        f"Snapshot: {snapshot['timestamp']}. Source: {snapshot['source']}.", '',
        *([f"Monthly income target: INR {target['minimum']:,}-{target['stretch']:,}."] if target else []),
        f"Subscribers: {stats.get('subscriberCount', 'hidden')}; public videos: {stats.get('videoCount', '?')}; "
        f"total channel views: {int(stats.get('viewCount', 0)):,}.", '',
        'These are lifetime public counts. They do not establish monthly revenue, audience country, '
        'retention, CTR, eligible watch hours, Shorts engaged views, or YPP approval.', '',
        '## Performance by title label', '',
        'The #Shorts label below is a heuristic, not an authoritative YouTube format classification. '
        'Counts are lifetime views per video, not views earned during a common measurement window.', '',
        '| Title label | Videos | Total video views | Median views/video |',
        '|---|---:|---:|---:|',
    ]
    for label, selected in (
        ('Contains #Shorts', [v for v in videos if '#shorts' in v['snippet']['title'].lower()]),
        ('Other titles', [v for v in videos if '#shorts' not in v['snippet']['title'].lower()]),
    ):
        values = [int(v.get('statistics', {}).get('viewCount', 0)) for v in selected]
        median = statistics.median(values) if values else 0
        lines.append(f'| {label} | {len(values)} | {sum(values):,} | {median:g} |')
    lines.extend(['', '## Top videos by lifetime views', '', '| Video | Published | Duration | Views |', '|---|---|---|---:|'])
    for v in sorted(videos, key=lambda v: int(v.get('statistics', {}).get('viewCount', 0)), reverse=True)[:15]:
        title = v['snippet']['title'].replace('|', '/').replace('[', '(').replace(']', ')')
        lines.append(f"| [{title}](https://www.youtube.com/watch?v={v['id']}) | "
                     f"{v['snippet']['publishedAt'][:10]} | {v['contentDetails']['duration']} | "
                     f"{int(v.get('statistics', {}).get('viewCount', 0)):,} |")
    duplicates = Counter(('shorts-tagged' if '#shorts' in v['snippet']['title'].lower() else 'other',
                          normalize_title(v['snippet']['title'])) for v in videos)
    repeated = [(fmt, title, count) for (fmt, title), count in duplicates.items() if count > 1]
    lines.extend(['', f'Repeated normalized titles within the same title label: {len(repeated)} groups.'])
    if target:
        lines.extend(['', '## Income planning', '', goals['rpm_scenario_note'], '',
                  'Assumes monetization is approved and all modelled views earn the selected RPM. '
                  'These are gross ad-revenue targets before expenses and taxes. '
                  'For take-home income, add actual costs to the target before dividing by RPM.', '',
                  f"| Assumed long-form RPM (INR/1,000 views) | Views/month for INR {target['minimum']:,} | Views/month for INR {target['stretch']:,} |",
                  '|---:|---:|---:|'])
        for rpm in goals['planning_rpm_scenarios_inr']:
            lines.append(f"| {rpm} | {required_views(target['minimum'], rpm):,} | {required_views(target['stretch'], rpm):,} |")
    lines.extend(['', '## Next decisions', '',
                  '1. Confirm YouTube Studio Earn status and obtain the last 28 days of CTR, retention, '
                  'watch time, returning viewers, country mix, and Shorts viewed-versus-swiped metrics.',
                  '2. Test fresh, source-backed animal-science topics inspired by the strongest public examples. '
                  'Compare at the same 48-hour and 7-day age; do not infer a winning niche from lifetime views alone.',
                  '3. Prepare two original long-form pilots and focused Shorts with distinct hooks. '
                  'Use the long-form experiments to test watch time and the Shorts to test audience discovery.',
                  '4. Record production time and cash cost per finished video. Increase output only after '
                  'quality and repeat viewing improve.',
                  '', 'Official eligibility and policy reference: https://support.google.com/youtube/answer/72851',
                  'Account-specific monetization status must be checked in YouTube Studio.', ''])
    return '\n'.join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--refresh', action='store_true', help='Read current public data using the existing API key.')
    parser.add_argument('--credentials-dir', type=Path, default=ROOT)
    parser.add_argument('--snapshot', type=Path, default=ROOT / 'database/channel_snapshot.json')
    parser.add_argument('--include-income', action='store_true', help='Include local business_goals.json in a private report.')
    args = parser.parse_args()
    goals = json.loads((ROOT / 'config/channel.json').read_text(encoding='utf-8'))
    if args.include_income:
        goals.update(json.loads((ROOT / 'config/business_goals.json').read_text(encoding='utf-8')))
    if args.refresh:
        snapshot = fetch_snapshot(args.credentials_dir, goals['channel_handle'])
        write_json_atomic(args.snapshot, snapshot)
    else:
        snapshot = json.loads(args.snapshot.read_text(encoding='utf-8'))
    report = build_report(snapshot, goals)
    output = ROOT / 'reports' / ('growth_report.md' if args.include_income else 'channel_growth.md')
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(report, encoding='utf-8')
    print(f'Growth report written to {output}')


if __name__ == '__main__':
    main()
