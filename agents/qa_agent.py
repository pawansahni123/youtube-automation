"""
QA Agent
--------
Purpose: Run right after the Editor Agent and inspect the final videos
(assets/final_videos/) for signs that something went wrong during editing --
before Upload Agent ever gets a chance to publish a broken video.

Two layers of checks:
1. TECHNICAL checks (fast, free, via ffprobe/ffmpeg):
   - Does the file have both a video and an audio stream?
   - Is the resolution correct for its format (1080x1920 short / 1920x1080 long)?
   - Is the duration reasonable (not near-zero, roughly matches the voiceover)?
   - Is too much of the video a solid black screen (a sign media/clips failed
     to download and Editor Agent silently fell back to black filler)?

2. AI VISUAL SPOT-CHECK (Gemini Vision, optional -- only runs if a Gemini key
   is configured): samples a few frames and asks Gemini whether they look like
   normal, intentional video content or broken/blank frames.

If ANY check fails, this agent raises an error with a clear, specific message
-- which (via safe_run) stops the pipeline before Thumbnail/SEO/Upload run,
so a bad edit never reaches YouTube. All results are logged to
database/qa_reports.json regardless of pass/fail, so you can review history.

Run standalone for testing:
    python agents/qa_agent.py
"""

import os
from _pipeline_utils import safe_run, GEMINI_API_KEYS, GEMINI_MODEL_NAME
from _content_guard import active_formats, file_sha256, load_history, write_json_atomic
import re
import json
import base64
import subprocess
import datetime
import tempfile
import requests

PROJECT_ROOT = os.path.join(os.path.dirname(__file__), "..")
FINAL_VIDEOS_DIR = os.path.join(PROJECT_ROOT, "assets", "final_videos")
AUDIO_DIR = os.path.join(PROJECT_ROOT, "assets", "audio")
QA_DB_PATH = os.path.join(PROJECT_ROOT, "database", "qa_reports.json")
SCRIPTS_DB_PATH = os.path.join(PROJECT_ROOT, "database", "scripts.json")

EXPECTED_RESOLUTIONS = {
    "short": (1080, 1920),
    "long": (1920, 1080),
}

# If more than this fraction of the video is solid black, treat it as a
# likely media-download failure rather than intentional content.
MAX_BLACK_RATIO = 0.35

# How much the final video's duration is allowed to differ from the
# voiceover's duration before we flag it as a mismatch (seconds).
MAX_DURATION_DRIFT_SECONDS = 3.0

# How many evenly-spaced frames to sample for the optional AI visual check.
AI_SAMPLE_FRAME_COUNT = 3


def run_ffprobe_json(path):
    result = subprocess.run(
        ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", "-show_streams", path],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"ffprobe could not read {path}: {result.stderr.strip()}")
    return json.loads(result.stdout)


def check_streams_and_resolution(path, format_name, issues):
    info = run_ffprobe_json(path)
    streams = info.get("streams", [])
    video_streams = [s for s in streams if s.get("codec_type") == "video"]
    audio_streams = [s for s in streams if s.get("codec_type") == "audio"]

    if not video_streams:
        issues.append(f"[{format_name}] No video stream found in the file at all.")
        return None

    if not audio_streams:
        issues.append(f"[{format_name}] No audio stream found -- voiceover audio is missing.")

    width = video_streams[0].get("width")
    height = video_streams[0].get("height")
    expected_w, expected_h = EXPECTED_RESOLUTIONS[format_name]
    if (width, height) != (expected_w, expected_h):
        issues.append(
            f"[{format_name}] Resolution is {width}x{height}, expected {expected_w}x{expected_h}."
        )

    duration = float(info.get("format", {}).get("duration", 0))
    return duration


def check_duration_matches_audio(final_duration, format_name, issues):
    if final_duration < 2.0:
        issues.append(f"[{format_name}] Final video is suspiciously short ({final_duration:.1f}s).")
    audio_path = os.path.join(AUDIO_DIR, f"latest_voiceover_{format_name}.mp3")
    if not os.path.exists(audio_path):
        issues.append(f"[{format_name}] Voiceover file is missing; duration cannot be verified.")
        return

    try:
        audio_info = run_ffprobe_json(audio_path)
        audio_duration = float(audio_info.get("format", {}).get("duration", 0))
    except Exception:
        issues.append(f"[{format_name}] Voiceover cannot be read; duration cannot be verified.")
        return

    drift = abs(final_duration - audio_duration)
    if drift > MAX_DURATION_DRIFT_SECONDS:
        issues.append(
            f"[{format_name}] Final video duration ({final_duration:.1f}s) doesn't match "
            f"the voiceover duration ({audio_duration:.1f}s) -- drift of {drift:.1f}s. "
            f"Audio and visuals may be out of sync."
        )

def check_black_screen_ratio(path, format_name, total_duration, issues):
    """Uses FFmpeg's blackdetect filter to measure how much of the video is
    solid black -- a strong signal that Media Agent couldn't find clips and
    Editor Agent silently filled sections with black."""
    if total_duration <= 0:
        return

    result = subprocess.run(
        ["ffmpeg", "-i", path, "-vf", "blackdetect=d=0.5:pic_th=0.98", "-an", "-f", "null", "-"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        issues.append(f"[{format_name}] FFmpeg black-screen analysis failed.")
        return
    black_durations = re.findall(r"black_duration:([\d.]+)", result.stderr)
    total_black = sum(float(d) for d in black_durations)
    ratio = total_black / total_duration

    if ratio > MAX_BLACK_RATIO:
        issues.append(
            f"[{format_name}] {ratio * 100:.0f}% of the video is solid black "
            f"(threshold is {MAX_BLACK_RATIO * 100:.0f}%) -- Media Agent likely failed to find "
            f"clips for multiple sections. Check database/media.json for sections with "
            f"\"type\": null."
        )


def extract_sample_frames(path, total_duration, count=AI_SAMPLE_FRAME_COUNT):
    """Extract `count` evenly-spaced frames as base64 JPEG strings."""
    frames = []
    if total_duration <= 0:
        return frames

    with tempfile.TemporaryDirectory() as temp_dir:
        for i in range(count):
            timestamp = total_duration * (i + 1) / (count + 1)
            frame_path = os.path.join(temp_dir, f"frame_{i}.jpg")
            subprocess.run(
                ["ffmpeg", "-y", "-ss", str(timestamp), "-i", path, "-frames:v", "1", frame_path],
                capture_output=True,
            )
            if os.path.exists(frame_path):
                with open(frame_path, "rb") as f:
                    frames.append(base64.b64encode(f.read()).decode("utf-8"))

    return frames


def ai_visual_spot_check(path, format_name, total_duration, issues):
    """Optional: ask Gemini Vision whether sampled frames look like normal,
    intentional content. Skips silently if no Gemini key is configured or
    the vision call fails for any reason -- this check is a bonus, not a
    hard requirement, since the technical checks already catch the main
    failure modes."""
    if not GEMINI_API_KEYS:
        return

    try:
        frames_b64 = extract_sample_frames(path, total_duration)
        if not frames_b64:
            return

        parts = [{
            "text": (
                "These are sample frames from a YouTube video, evenly spaced through its "
                "duration. For EACH frame, judge whether it looks like normal, intentional "
                "video content (any real photo/video footage, with or without text captions) "
                "versus a broken frame (solid black, solid color, heavy visual glitches/corruption, "
                "or completely blank). Minor captions overlapping the edge of the frame is fine "
                "and should NOT count as broken.\n\n"
                "Respond ONLY in valid JSON, no markdown, no preamble, in this exact shape:\n"
                '{"all_frames_look_normal": true or false, "notes": "brief explanation, '
                'especially if any frame looks broken"}'
            )
        }]
        for b64 in frames_b64:
            parts.append({"inline_data": {"mime_type": "image/jpeg", "data": b64}})

        url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL_NAME}:generateContent"
        body = {"contents": [{"parts": parts}]}

        response = requests.post(url, params={"key": GEMINI_API_KEYS[0]}, json=body, timeout=60)
        if not response.ok:
            print(f"[QA Agent] ({format_name}) AI visual check skipped (Gemini error {response.status_code}).")
            return

        data = response.json()
        text = data["candidates"][0]["content"]["parts"][0]["text"]
        clean_text = text.replace("```json", "").replace("```", "").strip()
        result = json.loads(clean_text)

        if not result.get("all_frames_look_normal", True):
            issues.append(f"[{format_name}] AI visual check flagged a problem: {result.get('notes')}")

    except Exception as e:
        print(f"[QA Agent] ({format_name}) AI visual check skipped due to an error ({e}). "
              f"Relying on technical checks only.")


def check_video(format_name):
    """Run all checks for one format (short/long). Returns (issues, checked: bool)."""
    path = os.path.join(FINAL_VIDEOS_DIR, f"final_{format_name}.mp4")
    if not os.path.exists(path):
        return [], False  # nothing to check, this format wasn't generated this run

    issues = []
    print(f"[QA Agent] Checking {format_name} video: streams, resolution, duration...")
    duration = check_streams_and_resolution(path, format_name, issues)

    if duration is not None:
        check_duration_matches_audio(duration, format_name, issues)
        print(f"[QA Agent] Checking {format_name} video: black-screen ratio...")
        check_black_screen_ratio(path, format_name, duration, issues)
        print(f"[QA Agent] Checking {format_name} video: AI visual spot-check...")
        ai_visual_spot_check(path, format_name, duration, issues)

    return issues, True


def save_report(all_issues, checked_formats, script_record, hashes):
    os.makedirs(os.path.dirname(QA_DB_PATH), exist_ok=True)
    record = {
        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "checked_formats": checked_formats,
        "passed": len(all_issues) == 0,
        "issues": all_issues,
        "script_timestamp": script_record['timestamp'],
        "video_sha256": hashes,
    }

    history = load_history(QA_DB_PATH)
    history.append(record)
    write_json_atomic(QA_DB_PATH, history)


def run():
    print("[QA Agent] Reviewing Editor Agent's output before it goes any further...")

    all_issues = []
    checked_formats = []
    scripts = load_history(SCRIPTS_DB_PATH)
    if not scripts:
        raise ValueError('No scripts found to validate against.')
    script_record = scripts[-1]
    hashes = {}

    for format_name in active_formats(script_record):
        issues, was_checked = check_video(format_name)
        if was_checked:
            checked_formats.append(format_name)
            path = os.path.join(FINAL_VIDEOS_DIR, f'final_{format_name}.mp4')
            hashes[format_name] = file_sha256(path)
            script_time = datetime.datetime.fromisoformat(script_record['timestamp'])
            if script_time.tzinfo is None:
                raise ValueError('Script timestamp must include a timezone.')
            for artifact in (path, os.path.join(AUDIO_DIR, f'latest_voiceover_{format_name}.mp3')):
                if os.path.exists(artifact) and os.path.getmtime(artifact) + 2 < script_time.timestamp():
                    issues.append(f'[{format_name}] Output predates the current script; regenerate voice and video.')
        else:
            issues.append(f'[{format_name}] Expected video is missing.')
        all_issues.extend(issues)

    save_report(all_issues, checked_formats, script_record, hashes)
    if not checked_formats:
        raise FileNotFoundError("No final videos found to check. Run editor_agent.py first.")

    if all_issues:
        print("\n[QA Agent] FOUND PROBLEMS WITH THE EDIT:")
        for issue in all_issues:
            print(f"  - {issue}")
        raise RuntimeError(
            f"QA check failed with {len(all_issues)} issue(s) -- see above. "
            f"Stopping before Thumbnail/SEO/Upload so nothing broken gets published. "
            f"Full report saved to database/qa_reports.json."
        )

    print(f"\n[QA Agent] All checks passed for: {', '.join(checked_formats)}. Editor Agent did its job correctly.")


if __name__ == "__main__":
    safe_run(run, "QA Agent")
