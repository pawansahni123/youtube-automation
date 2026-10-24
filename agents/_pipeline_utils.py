"""Gemini-only calls, JSON parsing, and pipeline errors.

Only the existing primary GEMINI_API_KEY is used. Persistent failures or
quota exhaustion stop the run. No other AI provider or alternate key is
used. Google billing/free-tier settings remain controlled in Google Cloud.
"""
import datetime
import json
import os
import re
import sys
import time
import requests
from dotenv import load_dotenv
from _content_guard import load_history, write_json_atomic

load_dotenv()
PROJECT_ROOT = os.path.join(os.path.dirname(__file__), '..')
ERRORS_DB_PATH = os.path.join(PROJECT_ROOT, 'database', 'errors.json')
GEMINI_MODEL_NAME = 'gemini-flash-latest'
GEMINI_API_KEYS = [os.environ['GEMINI_API_KEY']] if os.getenv('GEMINI_API_KEY') else []


def _is_daily_quota_error(response_text):
    text = response_text.lower()
    return 'perday' in text.replace(' ', '') or 'requests per day' in text


def _call_gemini(prompt, max_retries=4):
    if not GEMINI_API_KEYS:
        raise ValueError('GEMINI_API_KEY is missing. Only the existing Gemini key is supported.')
    if max_retries < 1:
        raise ValueError('max_retries must be at least 1.')
    url = f'https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL_NAME}:generateContent'
    body = {'contents': [{'parts': [{'text': prompt}]}]}
    for attempt in range(1, max_retries + 1):
        delay = min(15 * (2 ** (attempt - 1)), 60)
        try:
            response = requests.post(url, params={'key': GEMINI_API_KEYS[0]}, json=body, timeout=60)
        except requests.RequestException as error:
            if attempt < max_retries:
                print(f'[Gemini] {type(error).__name__}; retry {attempt}/{max_retries} in {delay}s.')
                time.sleep(delay)
                continue
            raise RuntimeError(f'Gemini connection failed after {max_retries} attempts ({type(error).__name__}). No provider fallback.') from None
        if response.status_code == 429 and _is_daily_quota_error(response.text):
            raise RuntimeError('Gemini daily quota exhausted. Run stopped; no provider fallback.')
        if response.status_code in (429, 500, 502, 503, 504) and attempt < max_retries:
            print(f'[Gemini] Temporary HTTP {response.status_code}; retry {attempt}/{max_retries} in {delay}s.')
            time.sleep(delay)
            continue
        if not response.ok:
            raise RuntimeError(f'Gemini HTTP {response.status_code}. Run stopped; no provider fallback.')
        try:
            content = response.json()['candidates'][0]['content']['parts'][0]['text']
        except (KeyError, IndexError, TypeError, ValueError):
            content = None
        if isinstance(content, str) and content.strip():
            return content
        if attempt < max_retries:
            print(f'[Gemini] Empty or unusable response; retry {attempt}/{max_retries}.')
            time.sleep(min(5 * attempt, 15))
            continue
        raise RuntimeError('Gemini returned no usable text. Run stopped; no provider fallback.')


def call_ai(prompt, max_retries=4, response_parser=None):
    """Call only Gemini; retry malformed JSON once on the same key."""
    for validation_attempt in range(2):
        response_text = _call_gemini(prompt, max_retries=max_retries)
        if response_parser is None:
            return response_text
        try:
            return response_parser(response_text)
        except ValueError:
            if validation_attempt:
                raise
            print('[Gemini] Invalid structured output; retrying once on the same key.')


call_gemini = call_ai


def parse_json_response(text, expected_type=dict):
    if not isinstance(text, str) or not text.strip():
        raise ValueError('Gemini returned no structured content.')
    cleaned = re.sub(r'<think>.*?</think>', '', text, flags=re.S).strip()
    if cleaned.startswith('```'):
        cleaned = re.sub(r'^```(?:json)?\s*', '', cleaned, flags=re.I)
        cleaned = re.sub(r'\s*```\s*$', '', cleaned)
    starts = [index for index, character in enumerate(cleaned) if character in '{[']
    if not starts:
        raise ValueError('Gemini response did not contain a JSON object or array.')
    try:
        result, _ = json.JSONDecoder().raw_decode(cleaned[starts[0]:])
    except ValueError:
        raise ValueError('Gemini response contained malformed JSON.') from None
    if not isinstance(result, expected_type):
        raise ValueError(f'Gemini JSON must be {expected_type.__name__}.')
    return result


def call_json(prompt, max_retries=4, expected_type=dict):
    return call_ai(prompt, max_retries=max_retries,
                   response_parser=lambda value: parse_json_response(value, expected_type))


def _redact(message):
    text = str(message)
    for key in GEMINI_API_KEYS:
        text = text.replace(key, '[REDACTED]')
    return re.sub(r'(?i)(key=)[^&\s]+', r'\1[REDACTED]', text)


def log_error(agent_name, exc):
    history = load_history(ERRORS_DB_PATH)
    history.append({'timestamp': datetime.datetime.now(datetime.timezone.utc).isoformat(),
                    'agent': agent_name, 'error_type': type(exc).__name__,
                    'error_message': _redact(exc)})
    write_json_atomic(ERRORS_DB_PATH, history)


def safe_run(run_func, agent_name):
    try:
        run_func()
    except Exception as exc:
        print(f'\n[{agent_name}] FAILED: {type(exc).__name__}: {_redact(exc)}')
        try:
            log_error(agent_name, exc)
            print(f'[{agent_name}] Error logged to database/errors.json')
        except Exception as log_error_exception:
            print(f'[{agent_name}] Could not write error log: {_redact(log_error_exception)}')
        sys.exit(1)
