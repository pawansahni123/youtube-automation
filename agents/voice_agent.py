"""
Voice Agent
-----------
Purpose: Take the latest script (database/scripts.json) and convert the narration
into an actual voiceover audio file (.mp3) for both the Short and Long-form scripts.

Uses edge-tts (Microsoft Edge's neural text-to-speech) by default -- completely
free, no API key, no billing, and noticeably more natural-sounding than gTTS.

Upgrade path (optional, even better voice quality/control): set ELEVENLABS_API_KEY
in .env and this script will automatically use ElevenLabs instead.

Run standalone for testing:
    python agents/voice_agent.py
"""

import os
from _pipeline_utils import safe_run
import json
import time
import asyncio
import requests
from dotenv import load_dotenv
import edge_tts

load_dotenv()

ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY")  # optional upgrade
ELEVENLABS_VOICE_ID = os.getenv("ELEVENLABS_VOICE_ID", "21m00Tcm4TlvDq8ikWAM")  # default: "Rachel"

# Natural-sounding US English neural voice. Browse more with: edge-tts --list-voices
EDGE_TTS_VOICE = os.getenv("EDGE_TTS_VOICE", "en-US-GuyNeural")

SCRIPTS_DB_PATH = os.path.join(os.path.dirname(__file__), "..", "database", "scripts.json")
AUDIO_DIR = os.path.join(os.path.dirname(__file__), "..", "assets", "audio")


def load_latest_scripts():
    if not os.path.exists(SCRIPTS_DB_PATH):
        raise FileNotFoundError("No scripts.json found. Run script_agent.py first.")

    with open(SCRIPTS_DB_PATH, "r") as f:
        history = json.load(f)

    if not history:
        raise ValueError("scripts.json is empty. Run script_agent.py first.")

    return history[-1]  # most recent run


def script_to_narration_text(script):
    """Join every section's narration into one continuous voiceover script."""
    return " ".join(section["narration"] for section in script["sections"])


async def _edge_tts_save(text, output_path, voice):
    communicate = edge_tts.Communicate(text, voice)
    await communicate.save(output_path)


def generate_audio_edge_tts(text, output_path):
    asyncio.run(_edge_tts_save(text, output_path, EDGE_TTS_VOICE))


def generate_audio_elevenlabs(text, output_path, max_retries=3):
    url = f"https://api.elevenlabs.io/v1/text-to-speech/{ELEVENLABS_VOICE_ID}"
    headers = {
        "xi-api-key": ELEVENLABS_API_KEY,
        "Content-Type": "application/json",
    }
    body = {
        "text": text,
        "model_id": "eleven_multilingual_v2",
        "voice_settings": {"stability": 0.5, "similarity_boost": 0.75},
    }

    wait_seconds = 10
    for attempt in range(1, max_retries + 1):
        response = requests.post(url, headers=headers, json=body, timeout=60)
        if response.status_code == 429 and attempt < max_retries:
            print(f"[Voice Agent] ElevenLabs rate limited. Waiting {wait_seconds}s...")
            time.sleep(wait_seconds)
            wait_seconds *= 2
            continue
        if not response.ok:
            print(f"[ElevenLabs Error {response.status_code}]: {response.text}")
        response.raise_for_status()
        with open(output_path, "wb") as f:
            f.write(response.content)
        return

    raise RuntimeError("ElevenLabs API failed after all retries.")


def generate_voiceover(text, output_path):
    if ELEVENLABS_API_KEY:
        print(f"[Voice Agent] Using ElevenLabs for {os.path.basename(output_path)}...")
        generate_audio_elevenlabs(text, output_path)
    else:
        print(f"[Voice Agent] Using free edge-tts ({EDGE_TTS_VOICE}) for {os.path.basename(output_path)}...")
        generate_audio_edge_tts(text, output_path)


def run():
    os.makedirs(AUDIO_DIR, exist_ok=True)

    print("[Voice Agent] Loading latest scripts...")
    record = load_latest_scripts()

    short_path = os.path.join(AUDIO_DIR, "latest_voiceover_short.mp3")
    long_path = os.path.join(AUDIO_DIR, "latest_voiceover_long.mp3")

    if record.get("short_script"):
        short_text = script_to_narration_text(record["short_script"])
        generate_voiceover(short_text, short_path)
    else:
        print("[Voice Agent] No short_script in latest record -- skipping Short audio.")

    if record.get("long_script"):
        long_text = script_to_narration_text(record["long_script"])
        generate_voiceover(long_text, long_path)
    else:
        print("[Voice Agent] No long_script in latest record -- skipping Long audio.")

    print("\n[Voice Agent] Done. Audio files saved:")
    if record.get("short_script"):
        print(f"  - {short_path}")
    if record.get("long_script"):
        print(f"  - {long_path}")


if __name__ == "__main__":
    safe_run(run, "Voice Agent")