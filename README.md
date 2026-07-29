# YouTube Automation System (Phase 1: Topic Agent)

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
