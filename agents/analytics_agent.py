"""
Analytics Agent
----------------
Purpose: Pull real performance data (views, watch time, likes, retention) for
every video we've uploaded (database/uploads.json) from the YouTube Analytics
API, then ask Gemini to turn those numbers into plain-English insights and
concrete recommendations for what topic/format/style to try next.

Uses a SEPARATE OAuth token from the Upload Agent because it needs a
different, read-only scope (yt-analytics.readonly) rather than the upload
scope. First run will open a browser once for this new permission.

Saves results to database/analytics.json and a readable summary to
assets/analytics/latest_insights.txt.

Run standalone for testing:
    python agents/analytics_agent.py
"""

import os
from _pipeline_utils import safe_run, call_gemini
import json
import time
import pickle
import datetime
import requests
from dotenv import load_dotenv
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

load_dotenv()

PROJECT_ROOT = os.path.join(os.path.dirname(__file__), "..")
CLIENT_SECRETS_FILE = os.path.join(PROJECT_ROOT, "client_secret.json")
ANALYTICS_TOKEN_FILE = os.path.join(PROJECT_ROOT, "token_analytics.pickle")

SCOPES = ["https://www.googleapis.com/auth/yt-analytics.readonly"]

UPLOADS_DB_PATH = os.path.join(PROJECT_ROOT, "database", "uploads.json")
ANALYTICS_DB_PATH = os.path.join(PROJECT_ROOT, "database", "analytics.json")
ANALYTICS_DIR = os.path.join(PROJECT_ROOT, "assets", "analytics")


def load_uploads():
    if not os.path.exists(UPLOADS_DB_PATH):
        raise FileNotFoundError("No uploads.json found. Run upload_agent.py first to publish a video.")
    with open(UPLOADS_DB_PATH, "r") as f:
        uploads = json.load(f)
    if not uploads:
        raise ValueError("uploads.json is empty. Run upload_agent.py first.")
    return uploads


def get_authenticated_analytics_service():
    if not os.path.exists(CLIENT_SECRETS_FILE):
        raise FileNotFoundError(f"{CLIENT_SECRETS_FILE} not found. Complete the OAuth setup first.")

    creds = None
    if os.path.exists(ANALYTICS_TOKEN_FILE):
        with open(ANALYTICS_TOKEN_FILE, "rb") as token:
            creds = pickle.load(token)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            print("[Analytics Agent] No saved analytics login found -- opening browser "
                  "for one-time authorization (read-only analytics access)...")
            flow = InstalledAppFlow.from_client_secrets_file(CLIENT_SECRETS_FILE, SCOPES)
            creds = flow.run_local_server(port=0)

        with open(ANALYTICS_TOKEN_FILE, "wb") as token:
            pickle.dump(creds, token)

    return build("youtubeAnalytics", "v2", credentials=creds)


def fetch_video_stats(analytics, video_id, upload_timestamp):
    """Pull views/watch time/likes/etc for one video from its upload date to today."""
    start_date = upload_timestamp[:10]
    end_date = datetime.date.today().isoformat()

    try:
        response = analytics.reports().query(
            ids="channel==MINE",
            startDate=start_date,
            endDate=end_date,
            metrics="views,estimatedMinutesWatched,averageViewDuration,"
                    "averageViewPercentage,likes,comments,subscribersGained",
            filters=f"video=={video_id}",
        ).execute()
    except Exception as e:
        print(f"[Analytics Agent] WARNING: Could not fetch stats for {video_id}: {e}")
        return None

    rows = response.get("rows")
    if not rows:
        return None

    values = rows[0]
    column_headers = [c["name"] for c in response.get("columnHeaders", [])]
    return dict(zip(column_headers, values))


def generate_insights(performance_data):
    prompt = f"""You are a YouTube growth strategist analyzing performance data for a
faceless automation channel.

Here is the performance data for every video uploaded so far (topic, format, and stats):
{json.dumps(performance_data, indent=2)}

Note: some videos may have very low or zero views if they are new or still private --
that's expected and not necessarily a bad sign, just note it as "too early to tell".

Respond ONLY in valid JSON, no markdown, no preamble, in this exact shape:
{{
  "summary": "2-3 sentence plain-English summary of how things are going so far",
  "best_performing": "which video/topic is doing best and a guess at why (or 'not enough data yet')",
  "worst_performing": "which video/topic is doing worst and a guess at why (or 'not enough data yet')",
  "patterns_noticed": ["short bullet observations about format, topic type, retention, etc."],
  "recommendations_for_next_topic": ["3-5 concrete, actionable suggestions for what niche_keywords or angle to try next"]
}}"""

    text = call_gemini(prompt)
    clean_text = text.replace("```json", "").replace("```", "").strip()
    return json.loads(clean_text)


def insights_to_plain_text(insights, performance_data):
    lines = ["=== PERFORMANCE SUMMARY ===", insights["summary"], ""]
    lines.append(f"Best performing: {insights['best_performing']}")
    lines.append(f"Worst performing: {insights['worst_performing']}")
    lines.append("")
    lines.append("=== PATTERNS NOTICED ===")
    for p in insights["patterns_noticed"]:
        lines.append(f"- {p}")
    lines.append("")
    lines.append("=== RECOMMENDATIONS FOR NEXT VIDEO ===")
    for r in insights["recommendations_for_next_topic"]:
        lines.append(f"- {r}")
    lines.append("")
    lines.append("=== RAW STATS PER VIDEO ===")
    for entry in performance_data:
        lines.append(f"- {entry['title']} ({entry['format']}): {entry.get('stats')}")
    return "\n".join(lines)


def save_result(insights, performance_data):
    os.makedirs(os.path.dirname(ANALYTICS_DB_PATH), exist_ok=True)
    os.makedirs(ANALYTICS_DIR, exist_ok=True)

    record = {
        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "performance_data": performance_data,
        "insights": insights,
    }

    history = []
    if os.path.exists(ANALYTICS_DB_PATH):
        with open(ANALYTICS_DB_PATH, "r") as f:
            try:
                history = json.load(f)
            except json.JSONDecodeError:
                history = []

    history.append(record)
    with open(ANALYTICS_DB_PATH, "w") as f:
        json.dump(history, f, indent=2)

    with open(os.path.join(ANALYTICS_DIR, "latest_insights.txt"), "w", encoding="utf-8") as f:
        f.write(insights_to_plain_text(insights, performance_data))


def run():
    print("[Analytics Agent] Loading upload history...")
    uploads = load_uploads()

    print("[Analytics Agent] Authenticating with YouTube Analytics "
          "(browser may open on first run)...")
    analytics = get_authenticated_analytics_service()

    performance_data = []
    for upload in uploads:
        print(f"[Analytics Agent] Fetching stats for: {upload['title']} ({upload['format']})...")
        stats = fetch_video_stats(analytics, upload["video_id"], upload["timestamp"])
        performance_data.append({
            "title": upload["title"],
            "topic": upload.get("topic"),
            "format": upload["format"],
            "video_url": upload["video_url"],
            "stats": stats or {"note": "no data yet (too new, or still private)"},
        })

    print("[Analytics Agent] Asking Gemini to generate insights + recommendations...")
    insights = generate_insights(performance_data)

    save_result(insights, performance_data)

    print("\n[Analytics Agent] Done. Insights:")
    print(insights_to_plain_text(insights, performance_data))
    return insights


if __name__ == "__main__":
    safe_run(run, "Analytics Agent")