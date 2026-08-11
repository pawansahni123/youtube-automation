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

DIVERSITY: Also loads the last several chosen topics from topics.json and
tells Gemini to avoid repeating the same subject/niche category too often,
so the channel doesn't end up publishing the same type of video every time
just because it has the highest velocity on a given day.

Run standalone for testing:
    python agents/topic_agent.py
"""

import os
from _pipeline_utils import safe_run, call_gemini
import json
import time
import datetime
import requests
from dotenv import load_dotenv
from googleapiclient.discovery import build

load_dotenv()

YOUTUBE_API_KEY = os.getenv("YOUTUBE_API_KEY")
REGION_CODE = os.getenv("REGION_CODE", "US")
NICHE_KEYWORDS = [k.strip() for k in os.getenv("NICHE_KEYWORDS", "facts").split(",")]

DB_PATH = os.path.join(os.path.dirname(__file__), "..", "database", "topics.json")

# How many of the most recent chosen topics to show Gemini, so it can
# actively avoid repeating the same subject/category too often.
RECENT_TOPICS_TO_AVOID = 8


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
                "matched_keyword": None,  # filled in by caller
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
        for s in stats:
            s["matched_keyword"] = keyword
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


def load_recent_chosen_topics():
    """Read the last few chosen topics so Gemini can avoid repeating the same
    subject/category too often."""
    if not os.path.exists(DB_PATH):
        return []
    try:
        with open(DB_PATH, "r") as f:
            history = json.load(f)
    except (json.JSONDecodeError, FileNotFoundError):
        return []

    recent = history[-RECENT_TOPICS_TO_AVOID:]
    return [
        {
            "chosen_topic": r["chosen"].get("chosen_topic"),
            "matched_keyword": r["chosen"].get("matched_keyword"),
        }
        for r in recent if "chosen" in r
    ]


def ask_gemini_to_pick_best_topic(candidates):
    """Send the top trending videos to Gemini and get back one recommended topic + angle."""
    titles_block = "\n".join(
        f"- \"{c['title']}\" (channel: {c['channel']}, velocity: {c['velocity_score']} views/hr, "
        f"matched niche keyword: \"{c['matched_keyword']}\")"
        for c in candidates
    )

    recent_topics = load_recent_chosen_topics()
    recent_block = (
        "\n".join(f"- {t['chosen_topic']} (niche: {t['matched_keyword']})" for t in recent_topics)
        if recent_topics else "(no recent history yet)"
    )

    prompt = f"""You are a YouTube content strategist for a faceless channel targeting a US audience,
covering these niche keywords: {", ".join(NICHE_KEYWORDS)}.

Here are currently trending videos (by views-per-hour velocity, not just total views),
each tagged with which niche keyword it matched:
{titles_block}

IMPORTANT -- channel diversity: here are our most recently published topics
(most recent last). Do NOT pick another topic in the same subject/category as
these recent ones unless nothing else in the candidate list is even remotely
viable -- our channel needs to rotate across ALL the niche keywords above over
time, not just repeatedly chase whichever single category has the highest
velocity today:
{recent_block}

Task:
1. Pick the ONE topic pattern with strong potential for our channel, favoring one
   from a DIFFERENT niche keyword than our recent picks above, as long as its
   velocity is reasonably competitive (doesn't have to be the single highest).
2. Suggest a fresh, non-duplicate video title + a one-line unique angle to differentiate us.
3. Say whether this fits better as a Short (under 60s) or a Long-form video, and why.

Respond ONLY in valid JSON, no markdown, no preamble, in this exact shape:
{{
  "chosen_topic": "...",
  "suggested_title": "...",
  "unique_angle": "...",
  "format": "short" or "long",
  "matched_keyword": "the niche keyword this topic belongs to",
  "reasoning": "..."
}}"""

    text = call_gemini(prompt)
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

    print(f"[Topic Agent] Found {len(candidates)} candidates. Asking Gemini to choose the best one "
          f"(with diversity in mind)...")
    chosen = ask_gemini_to_pick_best_topic(candidates)

    record = save_result(candidates, chosen)
    print("[Topic Agent] Done. Chosen topic:")
    print(json.dumps(chosen, indent=2))
    return record


if __name__ == "__main__":
    safe_run(run, "Topic Agent")