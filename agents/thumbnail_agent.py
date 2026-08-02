"""
Thumbnail Agent
----------------
Purpose: Automatically generate a clickable, YouTube-ready thumbnail (1280x720)
using the most eye-catching frame from the Short video's Hook section, with a
bold, high-contrast text overlay -- no paid image-generation API needed.

How it works:
1. Load the latest script (for the title) and media manifest (for the Hook
   section's image/video source).
2. Get a source frame: if the Hook media is a video, extract a frame from it
   with FFmpeg; if it's a photo, use it directly.
3. Overlay a short, punchy version of the title in bold text with a heavy
   outline (classic high-CTR thumbnail style).
4. Save as assets/thumbnails/latest_thumbnail.jpg at 1280x720.

Requires: Pillow (pip install pillow) and FFmpeg (already installed for the
Editor Agent).

Run standalone for testing:
    python agents/thumbnail_agent.py
"""

import os
import json
import shutil
import subprocess
import textwrap
from PIL import Image, ImageDraw, ImageFont
from _pipeline_utils import safe_run

SCRIPTS_DB_PATH = os.path.join(os.path.dirname(__file__), "..", "database", "scripts.json")
MEDIA_DB_PATH = os.path.join(os.path.dirname(__file__), "..", "database", "media.json")
THUMBNAIL_DIR = os.path.join(os.path.dirname(__file__), "..", "assets", "thumbnails")

THUMBNAIL_SIZE = (1280, 720)

# Common bold font locations to try, in order (Windows first, then Linux/Mac fallbacks)
FONT_CANDIDATES = [
    r"C:\Windows\Fonts\impact.ttf",
    r"C:\Windows\Fonts\arialbd.ttf",
    r"C:\Windows\Fonts\Arial Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
]


def load_latest(path):
    if not os.path.exists(path):
        raise FileNotFoundError(f"No {os.path.basename(path)} found. Run the earlier agents first.")
    with open(path, "r") as f:
        history = json.load(f)
    if not history:
        raise ValueError(f"{os.path.basename(path)} is empty.")
    return history[-1]


def find_font(size):
    for path in FONT_CANDIDATES:
        if os.path.exists(path):
            return ImageFont.truetype(path, size)
    return ImageFont.load_default()


def get_source_frame(media_item, output_path):
    """Get a still image to use as the thumbnail base -- extract a video frame
    or use the photo directly."""
    if media_item["type"] == "video" and media_item["file"] and os.path.exists(media_item["file"]):
        cmd = [
            "ffmpeg", "-y",
            "-ss", "1.0",
            "-i", media_item["file"],
            "-frames:v", "1",
            output_path,
        ]
        subprocess.run(cmd, capture_output=True, check=True)
        return output_path
    elif media_item["type"] == "photo" and media_item["file"] and os.path.exists(media_item["file"]):
        shutil.copy(media_item["file"], output_path)
        return output_path
    else:
        return None


def shorten_for_thumbnail(title, max_words=6):
    """Thumbnails need very few words -- trim the title down to a punchy fragment."""
    words = title.replace("?", "").replace("!", "").split()
    return " ".join(words[:max_words]).upper()


def draw_text_with_outline(draw, position, text, font, fill, outline_fill, outline_width):
    x, y = position
    for dx in range(-outline_width, outline_width + 1):
        for dy in range(-outline_width, outline_width + 1):
            if dx != 0 or dy != 0:
                draw.text((x + dx, y + dy), text, font=font, fill=outline_fill)
    draw.text((x, y), text, font=font, fill=fill)


def build_thumbnail(source_image_path, title_text, output_path):
    img = Image.open(source_image_path).convert("RGB")

    target_ratio = THUMBNAIL_SIZE[0] / THUMBNAIL_SIZE[1]
    img_ratio = img.width / img.height
    if img_ratio > target_ratio:
        new_width = int(img.height * target_ratio)
        left = (img.width - new_width) // 2
        img = img.crop((left, 0, left + new_width, img.height))
    else:
        new_height = int(img.width / target_ratio)
        top = (img.height - new_height) // 2
        img = img.crop((0, top, img.width, top + new_height))
    img = img.resize(THUMBNAIL_SIZE, Image.LANCZOS)

    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    overlay_draw = ImageDraw.Draw(overlay)
    band_top = int(THUMBNAIL_SIZE[1] * 0.62)
    overlay_draw.rectangle([0, band_top, THUMBNAIL_SIZE[0], THUMBNAIL_SIZE[1]], fill=(0, 0, 0, 120))
    img = Image.alpha_composite(img.convert("RGBA"), overlay).convert("RGB")

    draw = ImageDraw.Draw(img)
    font_size = 90

    short_text = shorten_for_thumbnail(title_text)
    wrapped = textwrap.wrap(short_text, width=14)[:3]

    while font_size > 40:
        font = find_font(font_size)
        max_line_width = max(draw.textlength(line, font=font) for line in wrapped)
        if max_line_width <= THUMBNAIL_SIZE[0] - 80:
            break
        font_size -= 8
        wrapped = textwrap.wrap(short_text, width=14)[:3]

    line_height = int(font_size * 1.15)
    total_text_height = line_height * len(wrapped)
    y = THUMBNAIL_SIZE[1] - total_text_height - 40

    for line in wrapped:
        line_width = draw.textlength(line, font=font)
        x = (THUMBNAIL_SIZE[0] - line_width) // 2
        draw_text_with_outline(draw, (x, y), line, font, fill="white", outline_fill="black", outline_width=6)
        y += line_height

    img.save(output_path, quality=92)
    return output_path


def run():
    print("[Thumbnail Agent] Loading latest script + media manifest...")
    script_record = load_latest(SCRIPTS_DB_PATH)
    media_record = load_latest(MEDIA_DB_PATH)

    title = script_record["short_script"]["title"]
    hook_media = next((m for m in media_record["short_media"] if m["section"] == 1), None)

    if hook_media is None:
        raise ValueError("No Hook section media found in media.json. Run media_agent.py first.")

    os.makedirs(THUMBNAIL_DIR, exist_ok=True)
    raw_frame_path = os.path.join(THUMBNAIL_DIR, "_raw_frame.jpg")

    print("[Thumbnail Agent] Extracting source frame from Hook section media...")
    frame_path = get_source_frame(hook_media, raw_frame_path)

    if frame_path is None:
        raise RuntimeError("Could not get a usable image from the Hook section's media file.")

    output_path = os.path.join(THUMBNAIL_DIR, "latest_thumbnail.jpg")
    print(f"[Thumbnail Agent] Building thumbnail with title text: \"{title}\"...")
    build_thumbnail(frame_path, title, output_path)

    os.remove(raw_frame_path)

    print(f"\n[Thumbnail Agent] Done. Thumbnail saved to: {output_path}")
    return output_path


if __name__ == "__main__":
    safe_run(run, "Topic Agent")