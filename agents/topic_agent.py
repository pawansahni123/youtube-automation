"""
Topic Agent
-----------
Purpose: Find currently trending video topics (US audience) for a given niche,
using real YouTube data (not guesses), then ask Gemini to pick the single best
topic + suggest an angle for our channel.

How it decides "trending":
    velocity_score = views / hours_since_published
This avoids the trap of an old viral video looking "hot" just because it has
a huge total view count.

Run standalone for testing:
    python agents/topic_agent.py
"""

import os
import json
import time
import datetime
import requests
from dotenv import load_dotenv
from googleapiclient.discovery import build

load_dotenv()

YOUTUBE_API_KEY = os.getenv("YOUTUBE_API_KEY")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
REGION_CODE = os.getenv("REGION_CODE", "US")
NICHE_KEYWORDS = [k.strip() for k in os.getenv("NICHE_KEYWORDS", "facts").split(",")]

DB_PATH = os.path.join(os.path.dirname(__file__), "..", "database", "topics.json")


def get_youtube_client():
    if not YOUTUBE_API_KEY:
        raise ValueError("YOUTUBE_API_KEY missing. Set it in your .env file.")
    return build("youtube", "v3", developerKey=YOUTUBE_API_KEY)


def search_recent_videos(youtube, keyword, hours_back=48, max_results=15):
    """Search YouTube for videos on `keyword` published in the last `hours_back` hours."""
    published_after = (
        datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=hours_back)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")

    search_response = youtube.search().list(
        q=keyword,
        part="id",
        type="video",
        order="viewCount",
        publishedAfter=published_after,
        regionCode=REGION_CODE,
        maxResults=max_results,
    ).execute()

    video_ids = [item["id"]["videoId"] for item in search_response.get("items", [])]
    return video_ids


def get_video_stats(youtube, video_ids):
    """Fetch view counts + publish times for a list of video IDs."""
    if not video_ids:
        return []

    stats_response = youtube.videos().list(
        part="snippet,statistics",
        id=",".join(video_ids),
    ).execute()

    results = []
    now = datetime.datetime.now(datetime.timezone.utc)

    for item in stats_response.get("items", []):
        try:
            views = int(item["statistics"].get("viewCount", 0))
            published_at = datetime.datetime.strptime(
                item["snippet"]["publishedAt"], "%Y-%m-%dT%H:%M:%SZ"
            ).replace(tzinfo=datetime.timezone.utc)
            hours_since = max((now - published_at).total_seconds() / 3600, 0.5)
            velocity = round(views / hours_since, 2)

            results.append({
                "title": item["snippet"]["title"],
                "channel": item["snippet"]["channelTitle"],
                "video_id": item["id"],
                "views": views,
                "hours_since_published": round(hours_since, 1),
                "velocity_score": velocity,
            })
        except (KeyError, ValueError):
            continue

    return results


def collect_trending_candidates(top_n=8):
    """Search every niche keyword, merge results, sort by velocity_score."""
    youtube = get_youtube_client()
    all_candidates = []

    for keyword in NICHE_KEYWORDS:
        video_ids = search_recent_videos(youtube, keyword)
        stats = get_video_stats(youtube, video_ids)
        all_candidates.extend(stats)

    # Remove duplicates (same video found via multiple keywords)
    seen = set()
    unique_candidates = []
    for c in all_candidates:
        if c["video_id"] not in seen:
            seen.add(c["video_id"])
            unique_candidates.append(c)

    unique_candidates.sort(key=lambda x: x["velocity_score"], reverse=True)
    return unique_candidates[:top_n]


def ask_gemini_to_pick_best_topic(candidates):
    """Send the top trending videos to Gemini and get back one recommended topic + angle."""
    if not GEMINI_API_KEY:
        raise ValueError("GEMINI_API_KEY missing. Set it in your .env file.")

    titles_block = "\n".join(
        f"- \"{c['title']}\" (channel: {c['channel']}, velocity: {c['velocity_score']} views/hr)"
        for c in candidates
    )

    prompt = f"""You are a YouTube content strategist for a faceless channel targeting a US audience,
in the niche: {", ".join(NICHE_KEYWORDS)}.

Here are currently trending videos (by views-per-hour velocity, not just total views):
{titles_block}

Task:
1. Pick the ONE topic pattern with the strongest, most repeatable potential for our channel.
2. Suggest a fresh, non-duplicate video title + a one-line unique angle to differentiate us.
3. Say whether this fits better as a Short (under 60s) or a Long-form video, and why.

Respond ONLY in valid JSON, no markdown, no preamble, in this exact shape:
{{
  "chosen_topic": "...",
  "suggested_title": "...",
  "unique_angle": "...",
  "format": "short" or "long",
  "reasoning": "..."
}}"""

    model_name = "gemini-flash-latest"  # auto-updating alias, avoids breaking when Google retires old models
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent"

    max_retries = 4
    wait_seconds = 15

    for attempt in range(1, max_retries + 1):
        response = requests.post(
            url,
            params={"key": GEMINI_API_KEY},
            json={"contents": [{"parts": [{"text": prompt}]}]},
            timeout=30,
        )
        if response.status_code == 429 and attempt < max_retries:
            print(f"[Gemini Agent] Rate limited (429). Waiting {wait_seconds}s before retry "
                  f"({attempt}/{max_retries})...")
            time.sleep(wait_seconds)
            wait_seconds *= 2  # exponential backoff
            continue
        if not response.ok:
            print(f"[Gemini API Error {response.status_code}]: {response.text}")
        response.raise_for_status()
        break

    data = response.json()

    text = data["candidates"][0]["content"]["parts"][0]["text"]
    clean_text = text.replace("```json", "").replace("```", "").strip()
    return json.loads(clean_text)


def save_result(candidates, chosen):
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    record = {
        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "candidates_considered": candidates,
        "chosen": chosen,
    }

    history = []
    if os.path.exists(DB_PATH):
        with open(DB_PATH, "r") as f:
            try:
                history = json.load(f)
            except json.JSONDecodeError:
                history = []

    history.append(record)
    with open(DB_PATH, "w") as f:
        json.dump(history, f, indent=2)

    return record


def run():
    print(f"[Topic Agent] Searching trending videos for niche: {NICHE_KEYWORDS} in {REGION_CODE}...")
    candidates = collect_trending_candidates()

    if not candidates:
        print("[Topic Agent] No candidates found. Try widening NICHE_KEYWORDS or hours_back.")
        return None

    print(f"[Topic Agent] Found {len(candidates)} candidates. Asking Gemini to choose the best one...")
    chosen = ask_gemini_to_pick_best_topic(candidates)

    record = save_result(candidates, chosen)
    print("[Topic Agent] Done. Chosen topic:")
    print(json.dumps(chosen, indent=2))
    return record


if __name__ == "__main__":
    run()