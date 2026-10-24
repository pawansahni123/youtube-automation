"""
Upload Agent
------------
Purpose: Take the final videos (assets/final_videos/), the SEO metadata
(database/seo.json), and the thumbnail (assets/thumbnails/) and upload them
to YouTube using OAuth (not a plain API key, since this acts on your behalf).

First run: opens a browser window asking you to log in and grant permission.
After that, a token file is saved so future runs are fully automatic (no
browser popup) until the token expires or is revoked.

SAFETY DEFAULT: videos are uploaded as "private" by default (see
UPLOAD_PRIVACY_STATUS in .env) so nothing goes live without you reviewing it
first in YouTube Studio. Change to "public" once you trust the pipeline.

Requires: google-auth-oauthlib, google-api-python-client (already installed),
and client_secret.json in the project root (see setup instructions).

Run standalone for testing:
    python agents/upload_agent.py
"""

import os
import json
import time
import pickle
import datetime
from _pipeline_utils import safe_run
from _content_guard import build_upload_plan, load_history, write_json_atomic
from dotenv import load_dotenv
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

load_dotenv()

PROJECT_ROOT = os.path.join(os.path.dirname(__file__), "..")
CLIENT_SECRETS_FILE = os.path.join(PROJECT_ROOT, "client_secret.json")
TOKEN_FILE = os.path.join(PROJECT_ROOT, "token.pickle")

SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]

SCRIPTS_DB_PATH = os.path.join(PROJECT_ROOT, "database", "scripts.json")
SEO_DB_PATH = os.path.join(PROJECT_ROOT, "database", "seo.json")
UPLOADS_DB_PATH = os.path.join(PROJECT_ROOT, "database", "uploads.json")
QA_DB_PATH = os.path.join(PROJECT_ROOT, "database", "qa_reports.json")
FINAL_VIDEOS_DIR = os.path.join(PROJECT_ROOT, "assets", "final_videos")
THUMBNAIL_PATH = os.path.join(PROJECT_ROOT, "assets", "thumbnails", "latest_thumbnail.jpg")

PRIVACY_STATUS = os.getenv("UPLOAD_PRIVACY_STATUS", "private")  # private / unlisted / public
CATEGORY_ID = os.getenv("UPLOAD_CATEGORY_ID", "24")  # 24 = Entertainment
MADE_FOR_KIDS = os.getenv("UPLOAD_MADE_FOR_KIDS", "false").lower() == "true"


def load_latest(path):
    if not os.path.exists(path):
        raise FileNotFoundError(f"No {os.path.basename(path)} found. Run the earlier agents first.")
    with open(path, "r") as f:
        history = json.load(f)
    if not history:
        raise ValueError(f"{os.path.basename(path)} is empty.")
    return history[-1]


def get_authenticated_service():
    if not os.path.exists(CLIENT_SECRETS_FILE):
        raise FileNotFoundError(
            f"{CLIENT_SECRETS_FILE} not found. Follow the OAuth setup steps to create it first."
        )

    creds = None
    if os.path.exists(TOKEN_FILE):
        with open(TOKEN_FILE, "rb") as token:
            creds = pickle.load(token)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            print("[Upload Agent] No saved login found -- opening browser for one-time authorization...")
            flow = InstalledAppFlow.from_client_secrets_file(CLIENT_SECRETS_FILE, SCOPES)
            creds = flow.run_local_server(port=0)

        with open(TOKEN_FILE, "wb") as token:
            pickle.dump(creds, token)

    return build("youtube", "v3", credentials=creds)


def upload_video(youtube, video_path, title, description, tags, thumbnail_path=None):
    if not os.path.exists(video_path):
        raise FileNotFoundError(f"Video file not found: {video_path}")

    body = {
        "snippet": {
            "title": title[:100],
            "description": description[:5000],
            "tags": tags,
            "categoryId": CATEGORY_ID,
        },
        "status": {
            "privacyStatus": PRIVACY_STATUS,
            "selfDeclaredMadeForKids": MADE_FOR_KIDS,
        },
    }

    # Upload in 5MB chunks (not the whole file in one shot) so a network drop
    # only loses one chunk -- the resumable upload picks back up from there.
    media = MediaFileUpload(video_path, chunksize=5 * 1024 * 1024, resumable=True, mimetype="video/mp4")

    print(f"[Upload Agent] Uploading \"{title}\" ({os.path.basename(video_path)})...")
    request = youtube.videos().insert(part="snippet,status", body=body, media_body=media)

    response = None
    consecutive_errors = 0
    max_consecutive_errors = 8

    while response is None:
        try:
            status, response = request.next_chunk(num_retries=5)
            consecutive_errors = 0
            if status:
                print(f"[Upload Agent]   {int(status.progress() * 100)}% uploaded...")
        except Exception as e:
            consecutive_errors += 1
            if consecutive_errors > max_consecutive_errors:
                raise RuntimeError(
                    f"Upload failed after {max_consecutive_errors} consecutive chunk retries: {e}"
                ) from e
            wait_seconds = min(5 * consecutive_errors, 60)
            print(f"[Upload Agent] Network hiccup on chunk upload ({e}). "
                  f"Retrying in {wait_seconds}s ({consecutive_errors}/{max_consecutive_errors})...")
            time.sleep(wait_seconds)

    video_id = response["id"]
    video_url = f"https://www.youtube.com/watch?v={video_id}"
    print(f"[Upload Agent] Upload complete: {video_url} (status: {PRIVACY_STATUS})")

    if thumbnail_path and os.path.exists(thumbnail_path):
        print("[Upload Agent] Setting custom thumbnail...")
        try:
            youtube.thumbnails().set(videoId=video_id, media_body=MediaFileUpload(thumbnail_path)).execute()
        except Exception as e:
            print(f"[Upload Agent] WARNING: Could not set thumbnail ({e}). "
                  f"You can set it manually in YouTube Studio. Continuing...")

    return video_id, video_url


def log_upload(video_id, video_url, video_format, title, topic_info, video_sha256, script_timestamp):
    os.makedirs(os.path.dirname(UPLOADS_DB_PATH), exist_ok=True)
    record = {
        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "video_id": video_id,
        "video_url": video_url,
        "format": video_format,
        "title": title,
        "topic": topic_info.get("chosen_topic"),
        "unique_angle": topic_info.get("unique_angle"),
        "video_sha256": video_sha256,
        "script_timestamp": script_timestamp,
    }

    history = load_history(UPLOADS_DB_PATH)
    history.append(record)
    write_json_atomic(UPLOADS_DB_PATH, history)


def run():
    print("[Upload Agent] Loading latest SEO metadata + scripts...")
    seo_record = load_latest(SEO_DB_PATH)
    script_record = load_latest(SCRIPTS_DB_PATH)
    topic_info = script_record["topic"]

    seo = seo_record["seo"]
    description = seo["description"]
    tags = seo["tags"]

    history = load_history(UPLOADS_DB_PATH)
    qa_history = load_history(QA_DB_PATH)
    plan = build_upload_plan(PROJECT_ROOT, script_record, seo_record,
                             qa_history[-1] if qa_history else {}, history)

    print("[Upload Agent] Authenticating with YouTube (browser may open on first run)...")
    youtube = get_authenticated_service()

    results = {}
    for item in plan:
        video_format = item['format']
        upload_description = description
        if video_format == 'short' and '#shorts' not in description.lower():
            upload_description += '\n\n#Shorts'
        video_id, video_url = upload_video(
            youtube, item['path'], item['title'], upload_description, tags,
            thumbnail_path=THUMBNAIL_PATH if video_format == 'long' else None,
        )
        log_upload(video_id, video_url, video_format, item['title'], topic_info,
                   item['video_sha256'], script_record['timestamp'])
        results[video_format] = video_url

    print("\n[Upload Agent] Done! Uploaded videos (check YouTube Studio to review/publish):")
    for kind, url in results.items():
        print(f"  - {kind}: {url}")

    return results


if __name__ == "__main__":
    safe_run(run, "Upload Agent")
