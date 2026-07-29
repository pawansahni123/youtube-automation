"""
Strategy Agent
--------------
Purpose: The "brain" that looks at ALL performance history (database/analytics.json,
database/uploads.json) over time and decides how the channel should adjust:
  - How to split effort between Short vs Long-form (a focus ratio)
  - Whether the current niche is working, or it's time to test a new angle
  - Specific topic-pattern recommendations based on what has actually performed

When enough data + confidence exists, this agent AUTOMATICALLY updates
NICHE_KEYWORDS in .env so future runs pivot without you manually editing
anything. Every change is logged to database/strategy_changelog.json so you
always have a full history and can revert if needed.

Run standalone for testing:
    python agents/strategy_agent.py
"""

import os
import re
import json
import time
import datetime
import requests
from dotenv import load_dotenv

load_dotenv()

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
MODEL_NAME = "gemini-flash-latest"

PROJECT_ROOT = os.path.join(os.path.dirname(__file__), "..")
ENV_PATH = os.path.join(PROJECT_ROOT, ".env")
UPLOADS_DB_PATH = os.path.join(PROJECT_ROOT, "database", "uploads.json")
ANALYTICS_DB_PATH = os.path.join(PROJECT_ROOT, "database", "analytics.json")
STRATEGY_DB_PATH = os.path.join(PROJECT_ROOT, "database", "strategy.json")
CHANGELOG_PATH = os.path.join(PROJECT_ROOT, "database", "strategy_changelog.json")
STRATEGY_DIR = os.path.join(PROJECT_ROOT, "assets", "strategy")

# Minimum videos before we trust the data enough to auto-apply a niche pivot.
NICHE_PIVOT_MIN_VIDEOS = 20
# Confidence levels allowed to trigger an automatic .env change.
AUTO_APPLY_CONFIDENCE_LEVELS = {"medium", "high"}


def load_json(path, default=None):
    if not os.path.exists(path):
        return default if default is not None else []
    with open(path, "r") as f:
        try:
            return json.load(f)
        except json.JSONDecodeError:
            return default if default is not None else []


def call_gemini(prompt, max_retries=4):
    if not GEMINI_API_KEY:
        raise ValueError("GEMINI_API_KEY missing. Set it in your .env file.")

    url = f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL_NAME}:generateContent"
    body = {"contents": [{"parts": [{"text": prompt}]}]}

    wait_seconds = 15
    for attempt in range(1, max_retries + 1):
        response = requests.post(url, params={"key": GEMINI_API_KEY}, json=body, timeout=60)
        if response.status_code == 429 and attempt < max_retries:
            print(f"[Strategy Agent] Rate limited. Waiting {wait_seconds}s...")
            time.sleep(wait_seconds)
            wait_seconds *= 2
            continue
        if not response.ok:
            print(f"[Gemini API Error {response.status_code}]: {response.text}")
        response.raise_for_status()
        return response.json()

    raise RuntimeError("Gemini API failed after all retries.")


def build_prompt(uploads, latest_analytics, total_video_count):
    confidence_note = (
        f"Only {total_video_count} videos uploaded so far -- this is EARLY DATA. "
        f"Do not recommend a full niche pivot until at least {NICHE_PIVOT_MIN_VIDEOS} videos "
        f"have been published. Keep recommendations lightweight and clearly flag low confidence."
        if total_video_count < NICHE_PIVOT_MIN_VIDEOS else
        f"{total_video_count} videos uploaded -- enough data to make a more confident call "
        f"on format focus and whether a niche pivot is warranted."
    )

    return f"""You are a YouTube channel strategist for a faceless automation channel.

Total videos published so far: {total_video_count}
{confidence_note}

Upload history (topics + formats):
{json.dumps(uploads, indent=2)}

Most recent performance analysis from the Analytics Agent:
{json.dumps(latest_analytics, indent=2) if latest_analytics else "No analytics data available yet."}

Based on this, decide the channel's strategy going forward.

Respond ONLY in valid JSON, no markdown, no preamble, in this exact shape:
{{
  "confidence_level": "low" or "medium" or "high",
  "format_focus": {{
    "short_weight": 0.0 to 1.0,
    "long_weight": 0.0 to 1.0,
    "reasoning": "why this split"
  }},
  "niche_status": "keep_current" or "adjust_angle" or "consider_pivot",
  "niche_reasoning": "why this status",
  "suggested_niche_keywords": ["only fill this in if niche_status is not 'keep_current' -- a comma-style keyword list ready to paste into NICHE_KEYWORDS in .env"],
  "next_topic_patterns": ["2-4 concrete topic/title patterns to try next, based on what's worked or on strong genre bets if data is too thin"],
  "action_summary": "1-2 sentence plain-English summary of what to actually do next"
}}"""


def generate_strategy(uploads, analytics_history):
    latest_analytics = analytics_history[-1] if analytics_history else None
    total_video_count = len(uploads)

    prompt = build_prompt(uploads, latest_analytics, total_video_count)
    result = call_gemini(prompt)
    text = result["candidates"][0]["content"]["parts"][0]["text"]
    clean_text = text.replace("```json", "").replace("```", "").strip()
    return json.loads(clean_text)


def strategy_to_plain_text(strategy, total_video_count):
    lines = [
        f"=== STRATEGY (based on {total_video_count} videos, confidence: {strategy['confidence_level']}) ===",
        "",
        strategy["action_summary"],
        "",
        "=== FORMAT FOCUS ===",
        f"Short: {strategy['format_focus']['short_weight'] * 100:.0f}%  |  "
        f"Long: {strategy['format_focus']['long_weight'] * 100:.0f}%",
        f"Why: {strategy['format_focus']['reasoning']}",
        "",
        f"=== NICHE STATUS: {strategy['niche_status'].upper()} ===",
        strategy["niche_reasoning"],
    ]

    if strategy.get("suggested_niche_keywords"):
        lines.append("")
        lines.append("Suggested new NICHE_KEYWORDS for .env:")
        lines.append(", ".join(strategy["suggested_niche_keywords"]))

    lines.append("")
    lines.append("=== TOPIC PATTERNS TO TRY NEXT ===")
    for pattern in strategy["next_topic_patterns"]:
        lines.append(f"- {pattern}")

    return "\n".join(lines)


def get_current_niche_keywords():
    if not os.path.exists(ENV_PATH):
        return None
    with open(ENV_PATH, "r", encoding="utf-8") as f:
        for line in f:
            if line.strip().startswith("NICHE_KEYWORDS="):
                return line.strip().split("=", 1)[1]
    return None


def update_niche_keywords_in_env(new_keywords_line):
    """Rewrite the NICHE_KEYWORDS= line in .env, leaving everything else untouched."""
    with open(ENV_PATH, "r", encoding="utf-8") as f:
        lines = f.readlines()

    updated = False
    for i, line in enumerate(lines):
        if line.strip().startswith("NICHE_KEYWORDS="):
            lines[i] = f"NICHE_KEYWORDS={new_keywords_line}\n"
            updated = True
            break

    if not updated:
        lines.append(f"NICHE_KEYWORDS={new_keywords_line}\n")

    with open(ENV_PATH, "w", encoding="utf-8") as f:
        f.writelines(lines)


def log_changelog_entry(entry):
    history = load_json(CHANGELOG_PATH, [])
    history.append(entry)
    with open(CHANGELOG_PATH, "w", encoding="utf-8") as f:
        json.dump(history, f, indent=2)


def maybe_auto_apply_niche_change(strategy, total_video_count):
    """Automatically updates NICHE_KEYWORDS in .env when the Strategy Agent has
    enough data and confidence to justify it. Every change is logged so nothing
    happens silently -- check database/strategy_changelog.json for full history."""
    niche_status = strategy.get("niche_status")
    confidence = strategy.get("confidence_level")
    suggested_keywords = strategy.get("suggested_niche_keywords")

    if niche_status == "keep_current":
        print("[Strategy Agent] Niche status: keep_current -- no changes applied.")
        return False

    if total_video_count < NICHE_PIVOT_MIN_VIDEOS:
        print(f"[Strategy Agent] Only {total_video_count} videos so far (need "
              f"{NICHE_PIVOT_MIN_VIDEOS}+) -- recommendation saved but NOT auto-applied yet.")
        return False

    if confidence not in AUTO_APPLY_CONFIDENCE_LEVELS:
        print(f"[Strategy Agent] Confidence is '{confidence}' -- too low to auto-apply. "
              f"Recommendation saved for your review instead.")
        return False

    if not suggested_keywords:
        print("[Strategy Agent] No concrete keyword suggestion provided -- nothing to apply.")
        return False

    old_keywords = get_current_niche_keywords()
    new_keywords_line = ", ".join(suggested_keywords)

    print(f"[Strategy Agent] AUTO-APPLYING niche change:")
    print(f"  OLD: {old_keywords}")
    print(f"  NEW: {new_keywords_line}")
    update_niche_keywords_in_env(new_keywords_line)

    log_changelog_entry({
        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "action": "niche_keywords_updated",
        "old_value": old_keywords,
        "new_value": new_keywords_line,
        "reasoning": strategy.get("niche_reasoning"),
        "confidence_level": confidence,
        "total_video_count": total_video_count,
    })

    print("[Strategy Agent] .env updated. Change logged to database/strategy_changelog.json.")
    return True


def save_result(strategy, total_video_count):
    os.makedirs(os.path.dirname(STRATEGY_DB_PATH), exist_ok=True)
    os.makedirs(STRATEGY_DIR, exist_ok=True)

    record = {
        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "total_video_count": total_video_count,
        "strategy": strategy,
    }

    history = load_json(STRATEGY_DB_PATH, [])
    history.append(record)
    with open(STRATEGY_DB_PATH, "w") as f:
        json.dump(history, f, indent=2)

    with open(os.path.join(STRATEGY_DIR, "latest_strategy.txt"), "w", encoding="utf-8") as f:
        f.write(strategy_to_plain_text(strategy, total_video_count))


def run():
    print("[Strategy Agent] Loading upload history + analytics history...")
    uploads = load_json(UPLOADS_DB_PATH, [])
    analytics_history = load_json(ANALYTICS_DB_PATH, [])

    if not uploads:
        print("[Strategy Agent] No uploads yet -- run upload_agent.py first to have something to analyze.")
        return None

    print(f"[Strategy Agent] Analyzing {len(uploads)} uploaded video(s)...")
    strategy = generate_strategy(uploads, analytics_history)

    save_result(strategy, len(uploads))
    maybe_auto_apply_niche_change(strategy, len(uploads))

    print("\n[Strategy Agent] Done. Strategy:")
    print(strategy_to_plain_text(strategy, len(uploads)))
    return strategy


if __name__ == "__main__":
    run()