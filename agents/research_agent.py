"""
Research Agent
---------------
Purpose: Take the topic chosen by Topic Agent (database/topics.json, latest entry)
and gather well-organized research: key facts, a strong hook, supporting points,
and a suggested narrative structure -- everything the Script Agent will need to
write the actual video script.

Uses Gemini with Google Search grounding (when available) so facts are checked
against real, current web sources instead of relying purely on the model's memory.

Run standalone for testing:
    python agents/research_agent.py
"""

import os
import json
import time
import datetime
import requests
from dotenv import load_dotenv

load_dotenv()

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
MODEL_NAME = "gemini-flash-latest"  # auto-updating alias, avoids breaking when Google retires old models

TOPICS_DB_PATH = os.path.join(os.path.dirname(__file__), "..", "database", "topics.json")
RESEARCH_DB_PATH = os.path.join(os.path.dirname(__file__), "..", "database", "research.json")


def load_latest_topic():
    """Read the most recent topic chosen by topic_agent.py."""
    if not os.path.exists(TOPICS_DB_PATH):
        raise FileNotFoundError(
            "No topics.json found. Run topic_agent.py first to generate a topic."
        )

    with open(TOPICS_DB_PATH, "r") as f:
        history = json.load(f)

    if not history:
        raise ValueError("topics.json is empty. Run topic_agent.py first.")

    return history[-1]["chosen"]  # most recent run


def call_gemini(prompt, use_grounding=False, max_retries=4):
    """Call Gemini with retry-on-429 backoff. Tries Google Search grounding first,
    falls back to a plain call if the grounded request errors out."""
    if not GEMINI_API_KEY:
        raise ValueError("GEMINI_API_KEY missing. Set it in your .env file.")

    url = f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL_NAME}:generateContent"
    body = {"contents": [{"parts": [{"text": prompt}]}]}
    if use_grounding:
        body["tools"] = [{"google_search": {}}]

    wait_seconds = 15
    for attempt in range(1, max_retries + 1):
        response = requests.post(url, params={"key": GEMINI_API_KEY}, json=body, timeout=60)

        if response.status_code == 429 and attempt < max_retries:
            print(f"[Research Agent] Rate limited (429). Waiting {wait_seconds}s "
                  f"before retry ({attempt}/{max_retries})...")
            time.sleep(wait_seconds)
            wait_seconds *= 2
            continue

        if response.status_code == 400 and use_grounding:
            # Grounding tool may not be supported for this key/model - retry without it.
            print("[Research Agent] Grounded search request failed, retrying without grounding...")
            return call_gemini(prompt, use_grounding=False, max_retries=max_retries)

        if not response.ok:
            print(f"[Gemini API Error {response.status_code}]: {response.text}")
        response.raise_for_status()
        return response.json()

    raise RuntimeError("Gemini API failed after all retries.")


def research_topic(topic_info):
    """Ask Gemini to produce structured research notes for the chosen topic."""
    prompt = f"""You are a research assistant for a faceless YouTube channel (US audience).

Chosen topic: {topic_info.get("chosen_topic")}
Suggested title: {topic_info.get("suggested_title")}
Unique angle: {topic_info.get("unique_angle")}
Format: {topic_info.get("format")}

Research this topic thoroughly and return well-organized notes the scriptwriter can use directly.

Respond ONLY in valid JSON, no markdown, no preamble, in this exact shape:
{{
  "hook_ideas": ["3 strong opening lines/hooks under 15 words each"],
  "key_facts": [
    {{"fact": "a specific, accurate, interesting fact", "why_it_matters": "why this grabs attention"}}
  ],
  "supporting_points": ["3-6 additional points or examples that build the narrative"],
  "suggested_structure": ["ordered list of narrative beats, e.g. Hook -> Fact 1 -> Twist -> Fact 2 -> CTA"],
  "cta_suggestion": "a natural call-to-action line for the end of the video"
}}

Include at least 5 key_facts. Keep facts accurate -- do not invent statistics or studies."""

    result = call_gemini(prompt)
    text = result["candidates"][0]["content"]["parts"][0]["text"]
    clean_text = text.replace("```json", "").replace("```", "").strip()
    return json.loads(clean_text)


def save_result(topic_info, research):
    os.makedirs(os.path.dirname(RESEARCH_DB_PATH), exist_ok=True)
    record = {
        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "topic": topic_info,
        "research": research,
    }

    history = []
    if os.path.exists(RESEARCH_DB_PATH):
        with open(RESEARCH_DB_PATH, "r") as f:
            try:
                history = json.load(f)
            except json.JSONDecodeError:
                history = []

    history.append(record)
    with open(RESEARCH_DB_PATH, "w") as f:
        json.dump(history, f, indent=2)

    return record


def run():
    print("[Research Agent] Loading latest topic from topics.json...")
    topic_info = load_latest_topic()
    print(f"[Research Agent] Researching: {topic_info.get('chosen_topic')}")

    research = research_topic(topic_info)
    record = save_result(topic_info, research)

    print("[Research Agent] Done. Research notes:")
    print(json.dumps(research, indent=2))
    return record


if __name__ == "__main__":
    run()