"""
Script Agent
------------
Purpose: Take the research notes (database/research.json, latest entry) and write
two full, word-for-word, ready-to-narrate video scripts:
  1. A Short (under 60 seconds, ~140-160 words)
  2. A Long-form video (8-10 minutes, ~1200-1500 words)

Both scripts are broken into labeled sections (Hook, Body beats, CTA) so the
Voice Agent and Editor Agent can later sync narration with visuals/timing.

Run standalone for testing:
    python agents/script_agent.py
"""

import os
from _pipeline_utils import safe_run, call_gemini
import sys
import json
import time
import datetime
import requests
from dotenv import load_dotenv

load_dotenv()


RESEARCH_DB_PATH = os.path.join(os.path.dirname(__file__), "..", "database", "research.json")
SCRIPTS_DB_PATH = os.path.join(os.path.dirname(__file__), "..", "database", "scripts.json")
ASSETS_DIR = os.path.join(os.path.dirname(__file__), "..", "assets")


def load_latest_research():
    """Read the most recent research produced by research_agent.py."""
    if not os.path.exists(RESEARCH_DB_PATH):
        raise FileNotFoundError(
            "No research.json found. Run research_agent.py first."
        )

    with open(RESEARCH_DB_PATH, "r") as f:
        history = json.load(f)

    if not history:
        raise ValueError("research.json is empty. Run research_agent.py first.")

    return history[-1]  # most recent run (has "topic" + "research" keys)



def build_prompt(topic_info, research, script_type):
    if script_type == "short":
        length_instruction = (
            "Write a Short script for a 45-60 second YouTube Short. "
            "Target 140-160 spoken words total. Extremely punchy, no filler words, "
            "every sentence must earn its place."
        )
    else:
        length_instruction = (
            "Write a Long-form script for an 8-10 minute YouTube video. "
            "Target 1200-1500 spoken words total. Include natural pacing, "
            "transitions between facts, and moments that re-hook the viewer "
            "(pattern interrupts) every 45-60 seconds to maintain retention."
        )

    return f"""You are a professional YouTube scriptwriter for a faceless channel (US audience).

Topic: {topic_info.get("chosen_topic")}
Working title: {topic_info.get("suggested_title")}
Unique angle: {topic_info.get("unique_angle")}

Research to use (do not invent new facts beyond this):
Hook ideas: {json.dumps(research.get("hook_ideas"))}
Key facts: {json.dumps(research.get("key_facts"))}
Supporting points: {json.dumps(research.get("supporting_points"))}
Suggested structure: {json.dumps(research.get("suggested_structure"))}
CTA suggestion: {research.get("cta_suggestion")}

{length_instruction}

Write it as pure narration -- exactly what the voiceover artist will read aloud, in a
confident, conversational tone. No stage directions inside the spoken lines.

Respond ONLY in valid JSON, no markdown, no preamble, in this exact shape:
{{
  "title": "final video title (can refine the working title)",
  "estimated_word_count": 123,
  "sections": [
    {{"label": "Hook", "narration": "...", "visual_suggestion": "short note on what to show on screen"}},
    {{"label": "Fact 1", "narration": "...", "visual_suggestion": "..."}}
  ]
}}

Use as many section objects as the structure needs (Hook, then one section per fact/beat, then CTA as the last section)."""


def generate_script(topic_info, research, script_type):
    prompt = build_prompt(topic_info, research, script_type)
    text = call_gemini(prompt)
    clean_text = text.replace("```json", "").replace("```", "").strip()
    return json.loads(clean_text)


def script_to_plain_text(script):
    lines = [f"TITLE: {script['title']}", f"Estimated words: {script.get('estimated_word_count', '?')}", ""]
    for section in script["sections"]:
        lines.append(f"[{section['label']}]")
        lines.append(section["narration"])
        if section.get("visual_suggestion"):
            lines.append(f"(Visual: {section['visual_suggestion']})")
        lines.append("")
    return "\n".join(lines)


def save_result(topic_info, research, short_script, long_script):
    os.makedirs(os.path.dirname(SCRIPTS_DB_PATH), exist_ok=True)
    os.makedirs(ASSETS_DIR, exist_ok=True)

    record = {
        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "topic": topic_info,
        "short_script": short_script,
        "long_script": long_script,
    }

    history = []
    if os.path.exists(SCRIPTS_DB_PATH):
        with open(SCRIPTS_DB_PATH, "r") as f:
            try:
                history = json.load(f)
            except json.JSONDecodeError:
                history = []

    history.append(record)
    with open(SCRIPTS_DB_PATH, "w") as f:
        json.dump(history, f, indent=2)

    # Also save human-readable .txt versions for quick review (only for formats we made)
    if short_script:
        with open(os.path.join(ASSETS_DIR, "latest_script_short.txt"), "w", encoding="utf-8") as f:
            f.write(script_to_plain_text(short_script))
    if long_script:
        with open(os.path.join(ASSETS_DIR, "latest_script_long.txt"), "w", encoding="utf-8") as f:
            f.write(script_to_plain_text(long_script))

    return record


def run():
    # Optional command-line arg controls which format(s) to produce:
    #   python agents/script_agent.py         -> both (default, backward compatible)
    #   python agents/script_agent.py short    -> only the Short
    #   python agents/script_agent.py long     -> only the Long-form
    format_mode = sys.argv[1].lower() if len(sys.argv) > 1 else "both"
    if format_mode not in ("both", "short", "long"):
        print(f"[Script Agent] Unknown format arg '{format_mode}', defaulting to 'both'.")
        format_mode = "both"

    print("[Script Agent] Loading latest research...")
    record = load_latest_research()
    topic_info = record["topic"]
    research = record["research"]

    short_script = None
    long_script = None

    if format_mode in ("both", "short"):
        print(f"[Script Agent] Writing SHORT script for: {topic_info.get('chosen_topic')}")
        short_script = generate_script(topic_info, research, "short")

    if format_mode in ("both", "long"):
        print("[Script Agent] Writing LONG-FORM script...")
        long_script = generate_script(topic_info, research, "long")

    saved = save_result(topic_info, research, short_script, long_script)

    print(f"\n[Script Agent] Done (mode: {format_mode}). Scripts saved to database/scripts.json")
    if short_script:
        print("--- SHORT SCRIPT PREVIEW ---")
        print(script_to_plain_text(short_script))
    return saved


if __name__ == "__main__":
    safe_run(run, "Script Agent")