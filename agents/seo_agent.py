"""
SEO Agent
---------
Purpose: Take the latest script + research (database/scripts.json,
database/research.json) and generate YouTube-optimized metadata:
  - 3 title variants (with the strongest one marked as recommended)
  - A keyword-rich description (hook + summary + CTA + hashtags)
  - 15-20 tags for the YouTube tags field
  - 3-5 hashtags to include in the title/description

Saves to database/seo.json and a readable copy to assets/seo/latest_seo.txt.

Run standalone for testing:
    python agents/seo_agent.py
"""

import os
from _pipeline_utils import safe_run, call_gemini
import json
import time
import datetime
import requests
from dotenv import load_dotenv

load_dotenv()


SCRIPTS_DB_PATH = os.path.join(os.path.dirname(__file__), "..", "database", "scripts.json")
RESEARCH_DB_PATH = os.path.join(os.path.dirname(__file__), "..", "database", "research.json")
SEO_DB_PATH = os.path.join(os.path.dirname(__file__), "..", "database", "seo.json")
SEO_DIR = os.path.join(os.path.dirname(__file__), "..", "assets", "seo")


def load_latest(path):
    if not os.path.exists(path):
        raise FileNotFoundError(f"No {os.path.basename(path)} found. Run the earlier agents first.")
    with open(path, "r") as f:
        history = json.load(f)
    if not history:
        raise ValueError(f"{os.path.basename(path)} is empty.")
    return history[-1]



def build_prompt(script_record, research_record):
    short_script = script_record["short_script"]
    topic_info = script_record["topic"]
    research = research_record.get("research", {})

    narration_summary = " ".join(s["narration"] for s in short_script["sections"])

    return f"""You are a YouTube SEO expert optimizing metadata for a video targeting a US audience.

Working title: {short_script.get("title")}
Topic: {topic_info.get("chosen_topic")}
Unique angle: {topic_info.get("unique_angle")}
Full narration (for context): {narration_summary}
Key facts used: {json.dumps(research.get("key_facts", []))}

Generate YouTube-optimized metadata that maximizes click-through rate and searchability,
while staying accurate to the video's actual content (no clickbait that misrepresents it).

Respond ONLY in valid JSON, no markdown, no preamble, in this exact shape:
{{
  "title_variants": [
    {{"title": "...", "why": "short reason this could work"}},
    {{"title": "...", "why": "..."}},
    {{"title": "...", "why": "..."}}
  ],
  "recommended_title_index": 0,
  "description": "a 3-4 paragraph YouTube description: hook line, brief summary with natural keywords, a call to subscribe, ending with 5-8 relevant hashtags",
  "tags": ["15 to 20 comma-ready YouTube tags, mix of broad and specific keywords"],
  "hashtags": ["3 to 5 hashtags for the title bar, each starting with #"]
}}"""


def generate_seo(script_record, research_record):
    prompt = build_prompt(script_record, research_record)
    text = call_gemini(prompt)
    clean_text = text.replace("```json", "").replace("```", "").strip()
    return json.loads(clean_text)


def seo_to_plain_text(seo):
    lines = ["=== TITLE OPTIONS ==="]
    for i, variant in enumerate(seo["title_variants"]):
        marker = " <-- RECOMMENDED" if i == seo.get("recommended_title_index", 0) else ""
        lines.append(f"{i + 1}. {variant['title']}{marker}")
        lines.append(f"   ({variant['why']})")
    lines.append("")
    lines.append("=== DESCRIPTION ===")
    lines.append(seo["description"])
    lines.append("")
    lines.append("=== TAGS ===")
    lines.append(", ".join(seo["tags"]))
    lines.append("")
    lines.append("=== HASHTAGS ===")
    lines.append(" ".join(seo["hashtags"]))
    return "\n".join(lines)


def save_result(topic_info, seo):
    os.makedirs(os.path.dirname(SEO_DB_PATH), exist_ok=True)
    os.makedirs(SEO_DIR, exist_ok=True)

    record = {
        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "topic": topic_info,
        "seo": seo,
    }

    history = []
    if os.path.exists(SEO_DB_PATH):
        with open(SEO_DB_PATH, "r") as f:
            try:
                history = json.load(f)
            except json.JSONDecodeError:
                history = []

    history.append(record)
    with open(SEO_DB_PATH, "w") as f:
        json.dump(history, f, indent=2)

    with open(os.path.join(SEO_DIR, "latest_seo.txt"), "w", encoding="utf-8") as f:
        f.write(seo_to_plain_text(seo))

    return record


def run():
    print("[SEO Agent] Loading latest script + research...")
    script_record = load_latest(SCRIPTS_DB_PATH)
    research_record = load_latest(RESEARCH_DB_PATH)

    print(f"[SEO Agent] Generating SEO metadata for: {script_record['topic'].get('chosen_topic')}")
    seo = generate_seo(script_record, research_record)

    save_result(script_record["topic"], seo)

    print("\n[SEO Agent] Done. SEO metadata:")
    print(seo_to_plain_text(seo))
    return seo


if __name__ == "__main__":
    safe_run(run, "SEO Agent")