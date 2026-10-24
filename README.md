# YouTube Automation System (Phase 1: Topic Agent)

## Current setup work — 24 October 2026

See [the measured channel growth report](reports/channel_growth.md) for the
public baseline and next production experiments. Owner notes, budgets and
income targets are stored locally and excluded from this public repository.

The full pipeline now includes topic, research, script, voice, media, editing,
QA, thumbnail, SEO and upload. Both scheduled formats require fresh, matching
SEO/QA records and reject duplicate video files or titles before upload.
Technical QA does not replace factual or editorial review.

```powershell
python -m unittest discover -s tests -v
python agents/growth_agent.py --refresh
```

The growth report uses the existing YouTube Data API key without an AI call.
The optional `--include-income` flag adds local `config/business_goals.json`
to an ignored private report at `reports/growth_report.md`.
Private Analytics needs its login renewed before retention and watch-time
analysis is available. Cloud execution changes only after deployment to GitHub.

The original topic-agent setup notes below remain useful for API-key setup.

### Gemini-only operation

All AI calls use the existing primary `GEMINI_API_KEY`. There is no OpenRouter,
Claude, alternate-key or other-provider fallback. Gemini quota exhaustion or
persistent failures stop the run. Technical FFmpeg QA, Edge TTS and Pexels
continue as before. Use a Google project configured for the free tier: choosing
Gemini in this code does not change the project's Google billing settings.

## अभी क्या तैयार है
`agents/topic_agent.py` — YouTube पर real trending videos ढूंढता है, हर video का
`velocity_score` (views ÷ hours since published) निकालता है, और Gemini से पूछता है
कि हमारी niche के लिए कौन सा topic + angle सबसे बेहतर रहेगा। Result
`database/topics.json` में save हो जाता है (history के साथ)।

## Setup (एक बार करना है)

### 1. Python packages install करो
```bash
pip install -r requirements.txt
```

### 2. API Keys लो

**YouTube Data API Key:**
1. https://console.cloud.google.com पर जाओ → नया project बनाओ
2. "APIs & Services" → "Library" → "YouTube Data API v3" search करके Enable करो
3. "Credentials" → "Create Credentials" → "API Key"

**Gemini API Key:**
1. https://aistudio.google.com/apikey पर जाओ
2. "Create API Key" पर click करो

### 3. `.env` file बनाओ
```bash
cp .env.example .env
```
फिर `.env` खोलकर अपनी असली keys डालो और `NICHE_KEYWORDS` को अपनी niche के हिसाब से बदलो।

## चलाना (Test Run)
```bash
python agents/topic_agent.py
```

Terminal में तुम्हें दिखेगा:
1. कौन से trending videos मिले (views/hour के साथ)
2. Gemini का चुना हुआ final topic + title + angle + format (short/long)

Result `database/topics.json` में भी save हो जाएगा — हर run history में जुड़ती जाएगी।

## अगला Step
जब ये चल जाए और result ठीक लगे, तो हम **Research Agent** बनाएंगे — जो इस चुने
हुए topic पर facts/data इकट्ठा करके अगले agent (Script Agent) को देगा।
