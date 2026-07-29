"""
Editor Agent
------------
Purpose: Take the voiceover audio (assets/audio/) and matched media clips
(assets/media/) and assemble them into a final, ready-to-upload .mp4 video
for both the Short and Long-form versions, using FFmpeg.

How it works:
1. Load the latest script + media manifest.
2. Measure the voiceover's total duration.
3. Split that duration across sections, proportional to each section's word
   count (a good proxy for how long the narrator spends on it).
4. For each section, turn its media file (video clip or still photo) into a
   normalized clip of the right duration, resolution or orientation.
5. Concatenate all section clips into one silent video track.
6. Mux that video track with the voiceover audio into the final .mp4.

Requires FFmpeg installed and available on PATH.
    Windows: https://www.gyan.dev/ffmpeg/builds/ (download "essentials" build,
    unzip, add the "bin" folder to your PATH, then restart PowerShell).
    Verify with: ffmpeg -version

Run standalone for testing:
    python agents/editor_agent.py
"""

import os
import json
import shutil
import subprocess
import tempfile

SCRIPTS_DB_PATH = os.path.join(os.path.dirname(__file__), "..", "database", "scripts.json")
MEDIA_DB_PATH = os.path.join(os.path.dirname(__file__), "..", "database", "media.json")
AUDIO_DIR = os.path.join(os.path.dirname(__file__), "..", "assets", "audio")
OUTPUT_DIR = os.path.join(os.path.dirname(__file__), "..", "assets", "final_videos")

# (width, height) per format
RESOLUTIONS = {
    "short": (1080, 1920),  # vertical 9:16
    "long": (1920, 1080),   # horizontal 16:9
}


def check_ffmpeg():
    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        raise EnvironmentError(
            "FFmpeg not found on PATH. Install it and make sure 'ffmpeg' and "
            "'ffprobe' work from a new terminal window before running this agent."
        )


def load_latest(path, label):
    if not os.path.exists(path):
        raise FileNotFoundError(f"No {os.path.basename(path)} found. Run the earlier agents first.")
    with open(path, "r") as f:
        history = json.load(f)
    if not history:
        raise ValueError(f"{os.path.basename(path)} is empty.")
    return history[-1]


def get_audio_duration(path):
    result = subprocess.run(
        ["ffprobe", "-v", "quiet", "-show_entries", "format=duration", "-of", "csv=p=0", path],
        capture_output=True, text=True, check=True,
    )
    return float(result.stdout.strip())


def compute_section_durations(sections, total_duration):
    """Split total_duration across sections proportional to word count."""
    word_counts = [max(len(s["narration"].split()), 1) for s in sections]
    total_words = sum(word_counts)
    durations = [total_duration * (wc / total_words) for wc in word_counts]
    return durations


def format_srt_time(seconds):
    seconds = max(seconds, 0)
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    ms = int(round((seconds - int(seconds)) * 1000))
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def chunk_words(narration, max_words=5):
    """Break narration into short caption-sized word groups (readable on screen at once)."""
    words = narration.split()
    return [" ".join(words[i:i + max_words]) for i in range(0, len(words), max_words)]


def build_srt_captions(sections, durations):
    """Build an SRT subtitle file's contents, evenly spreading each section's
    caption chunks across that section's allotted duration."""
    lines = []
    index = 1
    current_time = 0.0

    for section, duration in zip(sections, durations):
        chunks = chunk_words(section["narration"])
        if not chunks:
            current_time += duration
            continue

        per_chunk_duration = duration / len(chunks)
        for chunk in chunks:
            start = current_time
            end = current_time + per_chunk_duration
            lines.append(f"{index}")
            lines.append(f"{format_srt_time(start)} --> {format_srt_time(end)}")
            lines.append(chunk)
            lines.append("")  # blank line separates SRT entries
            index += 1
            current_time = end

    return "\n".join(lines)


def build_section_clip(media_item, duration, resolution, temp_dir, index):
    """Turn one media file (video or photo) into a normalized silent clip of `duration` seconds."""
    width, height = resolution
    output_path = os.path.join(temp_dir, f"clip_{index:02d}.mp4")

    scale_filter = f"scale={width}:{height}:force_original_aspect_ratio=increase,crop={width}:{height}"

    if media_item["type"] == "video" and media_item["file"]:
        cmd = [
            "ffmpeg", "-y",
            "-stream_loop", "-1", "-i", media_item["file"],
            "-t", str(duration),
            "-vf", f"{scale_filter},fps=30",
            "-an", "-pix_fmt", "yuv420p",
            output_path,
        ]
    elif media_item["type"] == "photo" and media_item["file"]:
        cmd = [
            "ffmpeg", "-y",
            "-loop", "1", "-i", media_item["file"],
            "-t", str(duration),
            "-vf", f"{scale_filter},fps=30",
            "-an", "-pix_fmt", "yuv420p",
            output_path,
        ]
    else:
        # No media found for this section -- generate a plain black clip so the
        # video still assembles correctly instead of crashing.
        cmd = [
            "ffmpeg", "-y",
            "-f", "lavfi", "-i", f"color=c=black:s={width}x{height}:d={duration}",
            "-vf", "fps=30",
            "-pix_fmt", "yuv420p",
            output_path,
        ]

    subprocess.run(cmd, capture_output=True, check=True)
    return output_path


def concat_clips(clip_paths, temp_dir, output_path):
    list_file = os.path.join(temp_dir, "concat_list.txt")
    with open(list_file, "w") as f:
        for clip in clip_paths:
            # FFmpeg concat demuxer needs forward slashes / escaped paths
            f.write(f"file '{os.path.abspath(clip).replace(os.sep, '/')}'\n")

    cmd = [
        "ffmpeg", "-y",
        "-f", "concat", "-safe", "0", "-i", list_file,
        "-c", "copy",
        output_path,
    ]
    subprocess.run(cmd, capture_output=True, check=True)


# Caption styling per format (bigger/lower on vertical Shorts, smaller on horizontal long-form)
CAPTION_STYLES = {
    "short": "FontName=Arial,FontSize=20,Bold=1,PrimaryColour=&H00FFFFFF,"
             "OutlineColour=&H00000000,BorderStyle=1,Outline=3,Shadow=0,"
             "Alignment=2,MarginV=180",
    "long": "FontName=Arial,FontSize=14,Bold=1,PrimaryColour=&H00FFFFFF,"
            "OutlineColour=&H00000000,BorderStyle=1,Outline=2,Shadow=0,"
            "Alignment=2,MarginV=80",
}


def mux_audio_video_with_captions(video_filename, audio_path, captions_filename, output_path, script_type, cwd):
    """Burns in captions.srt onto the video and muxes with the voiceover audio.
    Runs with cwd=temp_dir and uses relative filenames for video/captions so we
    never have to escape colons/backslashes in Windows paths for the subtitles filter."""
    style = CAPTION_STYLES[script_type]
    cmd = [
        "ffmpeg", "-y",
        "-i", video_filename,
        "-i", audio_path,
        "-filter_complex", f"[0:v]subtitles={captions_filename}:force_style='{style}'[v]",
        "-map", "[v]", "-map", "1:a:0",
        "-c:v", "libx264", "-preset", "medium", "-crf", "20",
        "-c:a", "aac", "-b:a", "192k", "-ac", "2",
        "-shortest",
        output_path,
    ]
    subprocess.run(cmd, capture_output=True, check=True, cwd=cwd)


def assemble_video(script, media_manifest, audio_path, script_type):
    resolution = RESOLUTIONS[script_type]
    sections = script["sections"]

    print(f"[Editor Agent] ({script_type}) Measuring voiceover duration...")
    total_duration = get_audio_duration(audio_path)
    durations = compute_section_durations(sections, total_duration)

    media_by_section = {m["section"]: m for m in media_manifest}

    with tempfile.TemporaryDirectory() as temp_dir:
        clip_paths = []
        for i, (section, duration) in enumerate(zip(sections, durations), start=1):
            media_item = media_by_section.get(i, {"type": None, "file": None})
            print(f"[Editor Agent] ({script_type}) Building clip {i}/{len(sections)} "
                  f"[{section['label']}] -- {duration:.1f}s...")
            clip_path = build_section_clip(media_item, duration, resolution, temp_dir, i)
            clip_paths.append(clip_path)

        print(f"[Editor Agent] ({script_type}) Concatenating clips...")
        silent_video_path = os.path.join(temp_dir, "silent_video.mp4")
        concat_clips(clip_paths, temp_dir, silent_video_path)

        print(f"[Editor Agent] ({script_type}) Generating captions...")
        captions_content = build_srt_captions(sections, durations)
        captions_path = os.path.join(temp_dir, "captions.srt")
        with open(captions_path, "w", encoding="utf-8") as f:
            f.write(captions_content)

        os.makedirs(OUTPUT_DIR, exist_ok=True)
        final_path = os.path.abspath(os.path.join(OUTPUT_DIR, f"final_{script_type}.mp4"))
        print(f"[Editor Agent] ({script_type}) Burning in captions + adding voiceover audio "
              f"(this step re-encodes, may take a bit longer)...")
        mux_audio_video_with_captions(
            "silent_video.mp4", os.path.abspath(audio_path), "captions.srt",
            final_path, script_type, cwd=temp_dir,
        )

    return final_path


def run():
    check_ffmpeg()

    print("[Editor Agent] Loading latest scripts + media manifest...")
    script_record = load_latest(SCRIPTS_DB_PATH, "scripts")
    media_record = load_latest(MEDIA_DB_PATH, "media")

    short_audio = os.path.join(AUDIO_DIR, "latest_voiceover_short.mp3")
    long_audio = os.path.join(AUDIO_DIR, "latest_voiceover_long.mp3")

    results = {}

    if script_record.get("short_script") and os.path.exists(short_audio):
        results["short"] = assemble_video(
            script_record["short_script"], media_record.get("short_media", []), short_audio, "short"
        )
    else:
        print("[Editor Agent] No short_script/audio -- skipping Short video.")

    if script_record.get("long_script") and os.path.exists(long_audio):
        results["long"] = assemble_video(
            script_record["long_script"], media_record.get("long_media", []), long_audio, "long"
        )
    else:
        print("[Editor Agent] No long_script/audio -- skipping Long video.")

    if not results:
        raise FileNotFoundError("Nothing to assemble -- no script+audio pair found for either format.")

    print("\n[Editor Agent] Done! Final videos:")
    for kind, path in results.items():
        print(f"  - {kind}: {path}")


if __name__ == "__main__":
    run()