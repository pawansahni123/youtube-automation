"""
Pipeline Utils
--------------
Shared helpers used by every agent:

1. safe_run() -- wraps an agent's run() call so a failure prints a clean
   banner (no raw traceback as the final word), logs it to
   database/errors.json, and exits with a controlled non-zero code instead
   of crashing messily.

2. call_ai() -- a MULTI-PROVIDER AI caller with automatic fallback:
       Gemini  ->  OpenRouter  ->  Claude (Anthropic)
   If Gemini hits its daily free-tier quota (or fails for any reason), the
   call automatically retries on OpenRouter; if that also fails, it retries
   on Claude. Only providers with an API key configured in .env are tried.
   Returns the plain text response (already extracted from whichever
   provider answered), so every agent's calling code stays provider-agnostic.

   Configure any/all of these in .env:
     GEMINI_API_KEY        (+ optional GEMINI_API_KEY_2 / _3 for extra Gemini fallback)
     OPENROUTER_API_KEY     (https://openrouter.ai/keys)
     ANTHROPIC_API_KEY      (https://console.anthropic.com/settings/keys)
"""

import os
import sys
import json
import time
import datetime
import traceback
import requests
from dotenv import load_dotenv

# Load .env ourselves, right here, at import time -- this file is imported by
# each agent BEFORE that agent calls load_dotenv() in its own code, so if we
# don't load it here too, GEMINI_API_KEYS etc. below would be computed before
# any .env values exist in the environment. (load_dotenv() is safe to call
# more than once.)
load_dotenv()

PROJECT_ROOT = os.path.join(os.path.dirname(__file__), "..")
ERRORS_DB_PATH = os.path.join(PROJECT_ROOT, "database", "errors.json")

# ---------------------------------------------------------------------------
# Provider configuration
# ---------------------------------------------------------------------------

GEMINI_MODEL_NAME = "gemini-flash-latest"
GEMINI_API_KEYS = [
    key for key in [
        os.getenv("GEMINI_API_KEY"),
        os.getenv("GEMINI_API_KEY_2"),
        os.getenv("GEMINI_API_KEY_3"),
    ]
    if key
]

OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
# "openrouter/free" is OpenRouter's own auto-router -- it picks whichever free
# model is currently available instead of us hardcoding one specific model ID
# that can get retired/paywalled without notice (this rotates often).
OPENROUTER_MODEL = os.getenv("OPENROUTER_MODEL", "openrouter/free")

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-3-5-haiku-20241022")


def _is_daily_quota_error(response_text):
    """Distinguish 'wait a few seconds' (per-minute rate limit, worth
    retrying on the SAME key) from 'you're done for today' (daily quota
    exhausted, worth moving on instead of waiting)."""
    text = response_text.lower()
    return "perday" in text.replace(" ", "") or "requests per day" in text


# ---------------------------------------------------------------------------
# Provider #1: Gemini (with multi-key fallback within itself)
# ---------------------------------------------------------------------------

def _call_gemini(prompt, max_retries=4):
    if not GEMINI_API_KEYS:
        raise ValueError("No Gemini API key configured.")

    url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL_NAME}:generateContent"
    body = {"contents": [{"parts": [{"text": prompt}]}]}
    last_error = None

    for key_index, api_key in enumerate(GEMINI_API_KEYS, start=1):
        wait_seconds = 15
        for attempt in range(1, max_retries + 1):
            response = requests.post(url, params={"key": api_key}, json=body, timeout=60)

            if response.status_code == 429:
                if _is_daily_quota_error(response.text):
                    print(f"[Gemini] Key #{key_index} has hit its DAILY quota. "
                          f"Trying the next Gemini key (if any)...")
                    last_error = RuntimeError(f"Gemini key #{key_index} daily quota exhausted")
                    break
                elif attempt < max_retries:
                    print(f"[Gemini] Key #{key_index} rate limited (429). Waiting {wait_seconds}s "
                          f"before retry ({attempt}/{max_retries})...")
                    time.sleep(wait_seconds)
                    wait_seconds *= 2
                    continue
                else:
                    last_error = RuntimeError(f"Gemini key #{key_index} rate limited after {max_retries} retries.")
                    break

            if not response.ok:
                last_error = RuntimeError(f"Gemini key #{key_index} HTTP {response.status_code}: {response.text}")
                break

            try:
                data = response.json()
                text = data["candidates"][0]["content"]["parts"][0]["text"]
            except (KeyError, IndexError, ValueError) as e:
                last_error = RuntimeError(f"Gemini key #{key_index} returned an unparseable response: {e}")
                break

            if not text or not text.strip():
                last_error = RuntimeError(f"Gemini key #{key_index} returned an empty response.")
                break

            return text

    raise last_error or RuntimeError("All configured Gemini API keys failed.")


# ---------------------------------------------------------------------------
# Provider #2: OpenRouter (OpenAI-compatible chat completions API)
# ---------------------------------------------------------------------------

def _call_openrouter(prompt, max_retries=4):
    if not OPENROUTER_API_KEY:
        raise ValueError("No OPENROUTER_API_KEY configured.")

    url = "https://openrouter.ai/api/v1/chat/completions"
    headers = {"Authorization": f"Bearer {OPENROUTER_API_KEY}"}
    body = {"model": OPENROUTER_MODEL, "messages": [{"role": "user", "content": prompt}]}

    wait_seconds = 15
    for attempt in range(1, max_retries + 1):
        response = requests.post(url, headers=headers, json=body, timeout=60)

        if response.status_code == 429 and attempt < max_retries:
            print(f"[OpenRouter] Rate limited. Waiting {wait_seconds}s before retry "
                  f"({attempt}/{max_retries})...")
            time.sleep(wait_seconds)
            wait_seconds *= 2
            continue

        if not response.ok:
            raise RuntimeError(f"OpenRouter HTTP {response.status_code}: {response.text}")

        try:
            data = response.json()
            text = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, ValueError) as e:
            raise RuntimeError(f"OpenRouter returned an unparseable response: {e}")

        if not text or not text.strip():
            raise RuntimeError("OpenRouter returned an empty response (model likely overloaded/refused).")

        return text

    raise RuntimeError("OpenRouter failed after all retries.")


# ---------------------------------------------------------------------------
# Provider #3: Claude (Anthropic Messages API)
# ---------------------------------------------------------------------------

def _call_claude(prompt, max_retries=4):
    if not ANTHROPIC_API_KEY:
        raise ValueError("No ANTHROPIC_API_KEY configured.")

    url = "https://api.anthropic.com/v1/messages"
    headers = {
        "x-api-key": ANTHROPIC_API_KEY,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    body = {
        "model": ANTHROPIC_MODEL,
        "max_tokens": 4096,
        "messages": [{"role": "user", "content": prompt}],
    }

    wait_seconds = 15
    for attempt in range(1, max_retries + 1):
        response = requests.post(url, headers=headers, json=body, timeout=60)

        if response.status_code == 429 and attempt < max_retries:
            print(f"[Claude] Rate limited. Waiting {wait_seconds}s before retry "
                  f"({attempt}/{max_retries})...")
            time.sleep(wait_seconds)
            wait_seconds *= 2
            continue

        if not response.ok:
            raise RuntimeError(f"Claude HTTP {response.status_code}: {response.text}")

        try:
            data = response.json()
            text = data["content"][0]["text"]
        except (KeyError, IndexError, ValueError) as e:
            raise RuntimeError(f"Claude returned an unparseable response: {e}")

        if not text or not text.strip():
            raise RuntimeError("Claude returned an empty response.")

        return text

    raise RuntimeError("Claude failed after all retries.")


# ---------------------------------------------------------------------------
# Unified entry point -- tries each configured provider in order
# ---------------------------------------------------------------------------

def call_ai(prompt, max_retries=4):
    """Try Gemini, then OpenRouter, then Claude -- whichever are configured
    with an API key -- returning the first successful plain-text response.
    Raises only if every configured provider fails."""
    providers = []
    if GEMINI_API_KEYS:
        providers.append(("Gemini", _call_gemini))
    if OPENROUTER_API_KEY:
        providers.append(("OpenRouter", _call_openrouter))
    if ANTHROPIC_API_KEY:
        providers.append(("Claude", _call_claude))

    if not providers:
        raise ValueError(
            "No AI provider configured. Set at least one of GEMINI_API_KEY, "
            "OPENROUTER_API_KEY, or ANTHROPIC_API_KEY in your .env file."
        )

    last_error = None
    for name, func in providers:
        try:
            return func(prompt, max_retries=max_retries)
        except Exception as e:
            print(f"[AI] {name} failed ({e}). Trying next provider...")
            last_error = e

    raise last_error or RuntimeError("All configured AI providers failed.")


# Backward-compatible alias -- existing agent code calling call_gemini(prompt)
# still works, but now transparently falls back to OpenRouter/Claude too.
call_gemini = call_ai


# ---------------------------------------------------------------------------
# Error logging + graceful pipeline stop
# ---------------------------------------------------------------------------

def log_error(agent_name, exc):
    """Append a structured error record to database/errors.json."""
    os.makedirs(os.path.dirname(ERRORS_DB_PATH), exist_ok=True)

    record = {
        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "agent": agent_name,
        "error_type": type(exc).__name__,
        "error_message": str(exc),
    }

    history = []
    if os.path.exists(ERRORS_DB_PATH):
        with open(ERRORS_DB_PATH, "r") as f:
            try:
                history = json.load(f)
            except json.JSONDecodeError:
                history = []

    history.append(record)
    with open(ERRORS_DB_PATH, "w") as f:
        json.dump(history, f, indent=2)


def safe_run(run_func, agent_name):
    """Wrap an agent's run() call. On success, returns normally. On failure,
    prints a clean error banner (no raw traceback dumped as the final word),
    logs it, and exits 1 so the pipeline stops -- but the failure is recorded
    and readable instead of a wall of stack trace."""
    try:
        run_func()
    except Exception as e:
        print("\n" + "=" * 60)
        print(f"[{agent_name}] FAILED: {type(e).__name__}: {e}")
        print("=" * 60)
        traceback.print_exc()
        try:
            log_error(agent_name, e)
            print(f"[{agent_name}] Error logged to database/errors.json")
        except Exception as log_err:
            print(f"[{agent_name}] (Could not write error log: {log_err})")
        sys.exit(1)