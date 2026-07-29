"""
Media Agent
-----------
Purpose: Take the latest script (database/scripts.json) and, for each section,
find a matching stock video clip (or photo as fallback) using the Pexels API --
completely free, no billing, just a free API key.

Downloads media files into assets/media/<short|long>/section_N.<ext> and saves
a manifest (database/media.json) mapping each section to its downloaded file,
so the Editor Agent can later stitch everything together with FFmpeg.

Run standalone for testing:
    python agents/media_agent.py
"""

import os
import re
import json
import time
import datetime
import requests
from dotenv import load_dotenv

load_dotenv()

PEXELS_API_KEY = os.getenv("PEXELS_API_KEY")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
MODEL_NAME = "gemini-flash-latest"

SCRIPTS_DB_PATH = os.path.join(os.path.dirname(__file__), "..", "database", "scripts.json")
MEDIA_DB_PATH = os.path.join(os.path.dirname(__file__), "..", "database", "media.json")
MEDIA_DIR = os.path.join(os.path.dirname(__file__), "..", "assets", "media")


def load_latest_scripts():
    if not os.path.exists(SCRIPTS_DB_PATH):
        raise FileNotFoundError("No scripts.json found. Run script_agent.py first.")

    with open(SCRIPTS_DB_PATH, "r") as f:
        history = json.load(f)

    if not history:
        raise ValueError("scripts.json is empty. Run script_agent.py first.")

    return history[-1]


def call_gemini(prompt, max_retries=4):
    if not GEMINI_API_KEY:
        raise ValueError("GEMINI_API_KEY missing. Set it in your .env file.")

    url = f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL_NAME}:generateContent"
    body = {"contents": [{"parts": [{"text": prompt}]}]}

    wait_seconds = 15
    for attempt in range(1, max_retries + 1):
        response = requests.post(url, params={"key": GEMINI_API_KEY}, json=body, timeout=60)
        if response.status_code == 429 and attempt < max_retries:
            print(f"[Media Agent] Rate limited. Waiting {wait_seconds}s...")
            time.sleep(wait_seconds)
            wait_seconds *= 2
            continue
        if not response.ok:
            print(f"[Gemini API Error {response.status_code}]: {response.text}")
        response.raise_for_status()
        return response.json()

    raise RuntimeError("Gemini API failed after all retries.")


def get_anchor_keywords(topic_info):
    """Ask Gemini for 2-3 concrete, filmable subject keywords (e.g. 'bobcat', 'kitten')
    that we force into EVERY section's search query, so stock footage stays on-topic
    even when a section's visual_suggestion is vague or abstract."""
    prompt = f"""Topic: {topic_info.get("chosen_topic")}
Title: {topic_info.get("suggested_title")}

List 2-3 concrete, filmable nouns that best represent this video's main subject
(e.g. a specific animal, object, or setting -- not abstract ideas). These will be
used as stock-footage search keywords, so they must be visual and literal.

Respond ONLY with a JSON array of strings, no markdown, e.g. ["bobcat", "kitten", "veterinarian"]"""

    try:
        result = call_gemini(prompt)
        text = result["candidates"][0]["content"]["parts"][0]["text"]
        clean_text = text.replace("```json", "").replace("```", "").strip()
        keywords = json.loads(clean_text)
        if isinstance(keywords, list) and keywords:
            return [str(k) for k in keywords[:3]]
    except Exception as e:
        print(f"[Media Agent] WARNING: Could not extract anchor keywords ({e}). "
              f"Falling back to visual_suggestion text only.")

    return []


def clean_query(visual_suggestion, narration, anchor_keywords=None):
    """Turn a visual suggestion into a short, effective search query, always
    grounded with the topic's anchor keywords so results stay on-subject."""
    text = visual_suggestion or narration
    text = re.sub(r"[^A-Za-z0-9 ]", " ", text)
    words = text.split()

    anchor_keywords = anchor_keywords or []
    # Keep the anchor keywords first (guarantees relevance), then fill remaining
    # slots with words from the visual suggestion, capped at 6 words total.
    remaining_slots = max(6 - len(anchor_keywords), 2)
    section_words = [w for w in words if w.lower() not in [a.lower() for a in anchor_keywords]]
    query_words = anchor_keywords + section_words[:remaining_slots]
    return " ".join(query_words)


def search_pexels_video(query, orientation="portrait", max_retries=3):
    """Search Pexels for a matching video clip. Returns a direct .mp4 URL or None."""
    if not PEXELS_API_KEY:
        raise ValueError("PEXELS_API_KEY missing. Set it in your .env file.")

    url = "https://api.pexels.com/videos/search"
    headers = {"Authorization": PEXELS_API_KEY}
    params = {"query": query, "orientation": orientation, "per_page": 1}

    wait_seconds = 10
    for attempt in range(1, max_retries + 1):
        response = requests.get(url, headers=headers, params=params, timeout=30)
        if response.status_code == 429 and attempt < max_retries:
            print(f"[Media Agent] Rate limited. Waiting {wait_seconds}s...")
            time.sleep(wait_seconds)
            wait_seconds *= 2
            continue
        if not response.ok:
            print(f"[Pexels API Error {response.status_code}]: {response.text}")
            return None
        data = response.json()
        videos = data.get("videos", [])
        if not videos:
            return None

        # Pick a reasonably sized mp4 file (avoid huge 4K downloads)
        video_files = videos[0].get("video_files", [])
        candidates = [v for v in video_files if v.get("file_type") == "video/mp4"]
        candidates.sort(key=lambda v: v.get("width", 0))
        for v in candidates:
            if v.get("width", 0) >= 720:
                return v["link"]
        return candidates[-1]["link"] if candidates else None

    return None


def search_pexels_photo(query, orientation="portrait", max_retries=3):
    """Fallback: search Pexels for a matching photo. Returns a direct image URL or None."""
    if not PEXELS_API_KEY:
        raise ValueError("PEXELS_API_KEY missing. Set it in your .env file.")

    url = "https://api.pexels.com/v1/search"
    headers = {"Authorization": PEXELS_API_KEY}
    params = {"query": query, "orientation": orientation, "per_page": 1}

    wait_seconds = 10
    for attempt in range(1, max_retries + 1):
        response = requests.get(url, headers=headers, params=params, timeout=30)
        if response.status_code == 429 and attempt < max_retries:
            print(f"[Media Agent] Rate limited. Waiting {wait_seconds}s...")
            time.sleep(wait_seconds)
            wait_seconds *= 2
            continue
        if not response.ok:
            print(f"[Pexels API Error {response.status_code}]: {response.text}")
            return None
        data = response.json()
        photos = data.get("photos", [])
        if not photos:
            return None
        return photos[0]["src"]["large2x"]

    return None


def download_file(url, output_path, max_retries=4):
    """Download with retries -- network drops mid-download are common on flaky
    connections, especially for larger video files."""
    wait_seconds = 5
    last_error = None

    for attempt in range(1, max_retries + 1):
        try:
            response = requests.get(url, stream=True, timeout=60)
            response.raise_for_status()
            with open(output_path, "wb") as f:
                for chunk in response.iter_content(chunk_size=8192):
                    f.write(chunk)
            return  # success
        except requests.exceptions.RequestException as e:
            last_error = e
            if attempt < max_retries:
                print(f"[Media Agent] Download hiccup ({e}). Retrying in {wait_seconds}s "
                      f"({attempt}/{max_retries})...")
                time.sleep(wait_seconds)
                wait_seconds *= 2
            else:
                print(f"[Media Agent] Download failed after {max_retries} attempts: {e}")

    raise last_error


def fetch_media_for_script(script, script_type, orientation, anchor_keywords):
    """For each section in the script, find + download a matching video (or photo fallback)."""
    out_dir = os.path.join(MEDIA_DIR, script_type)
    os.makedirs(out_dir, exist_ok=True)

    manifest = []
    for i, section in enumerate(script["sections"], start=1):
        query = clean_query(section.get("visual_suggestion", ""), section.get("narration", ""), anchor_keywords)
        print(f"[Media Agent] ({script_type}) Section {i} [{section['label']}] -> searching: \"{query}\"")

        video_url = search_pexels_video(query, orientation=orientation)
        photo_url = None if video_url else search_pexels_photo(query, orientation=orientation)

        # If the specific query found nothing, fall back to just the anchor
        # keywords alone (broader, but still on-topic) before giving up.
        if not video_url and not photo_url and anchor_keywords:
            broad_query = " ".join(anchor_keywords)
            print(f"[Media Agent] ({script_type}) Section {i}: no results, "
                  f"retrying with broader query: \"{broad_query}\"")
            video_url = search_pexels_video(broad_query, orientation=orientation)
            photo_url = None if video_url else search_pexels_photo(broad_query, orientation=orientation)
            query = broad_query

        if video_url:
            try:
                file_path = os.path.join(out_dir, f"section_{i:02d}.mp4")
                download_file(video_url, file_path)
                manifest.append({"section": i, "label": section["label"], "type": "video", "file": file_path, "query": query})
                continue
            except Exception as e:
                print(f"[Media Agent] WARNING: Video download ultimately failed for section {i} ({e}). "
                      f"Trying a photo instead...")
                photo_url = search_pexels_photo(query, orientation=orientation)

        if photo_url:
            try:
                file_path = os.path.join(out_dir, f"section_{i:02d}.jpg")
                download_file(photo_url, file_path)
                manifest.append({"section": i, "label": section["label"], "type": "photo", "file": file_path, "query": query})
                continue
            except Exception as e:
                print(f"[Media Agent] WARNING: Photo download also failed for section {i} ({e}).")

        print(f"[Media Agent] WARNING: No usable media for section {i} (\"{query}\"). "
              f"Editor Agent will use a black clip here instead.")
        manifest.append({"section": i, "label": section["label"], "type": None, "file": None, "query": query})

    return manifest


def save_result(topic_info, short_manifest, long_manifest):
    os.makedirs(os.path.dirname(MEDIA_DB_PATH), exist_ok=True)
    record = {
        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "topic": topic_info,
        "short_media": short_manifest,
        "long_media": long_manifest,
    }

    history = []
    if os.path.exists(MEDIA_DB_PATH):
        with open(MEDIA_DB_PATH, "r") as f:
            try:
                history = json.load(f)
            except json.JSONDecodeError:
                history = []

    history.append(record)
    with open(MEDIA_DB_PATH, "w") as f:
        json.dump(history, f, indent=2)

    return record


def run():
    print("[Media Agent] Loading latest scripts...")
    record = load_latest_scripts()
    topic_info = record["topic"]

    print("[Media Agent] Extracting anchor keywords to keep every clip on-topic...")
    anchor_keywords = get_anchor_keywords(topic_info)
    print(f"[Media Agent] Anchor keywords: {anchor_keywords or '(none -- using visual_suggestion only)'}")

    # Shorts are vertical (9:16), long-form is typically horizontal (16:9)
    short_manifest = []
    long_manifest = []

    if record.get("short_script"):
        print("[Media Agent] Fetching media for SHORT script (vertical)...")
        short_manifest = fetch_media_for_script(record["short_script"], "short", "portrait", anchor_keywords)
    else:
        print("[Media Agent] No short_script -- skipping Short media.")

    if record.get("long_script"):
        print("[Media Agent] Fetching media for LONG-FORM script (horizontal)...")
        long_manifest = fetch_media_for_script(record["long_script"], "long", "landscape", anchor_keywords)
    else:
        print("[Media Agent] No long_script -- skipping Long media.")

    saved = save_result(topic_info, short_manifest, long_manifest)

    print("\n[Media Agent] Done. Media saved under assets/media/short/ and assets/media/long/")
    print("Manifest saved to database/media.json")
    return saved


if __name__ == "__main__":
    run()