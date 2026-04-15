# ElevenLabs ↔ Claude Bridge

A small FastAPI app that lets your ElevenLabs agent use Claude as its LLM brain.

## How it works

```
You (voice) ↔ ElevenLabs Agent (WebSocket) ↔ this app (HTTP) ↔ Claude API
```

This app exposes an OpenAI-compatible `/v1/chat/completions` endpoint.
ElevenLabs is configured to call it as a custom LLM.

---

## Setup

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

### 2. Configure your API key

Copy `.env.template` to `.env` and fill in your Anthropic API key:

```bash
cp .env.template .env
```

Edit `.env`:
```
ANTHROPIC_API_KEY=sk-ant-...
```

### 3. Run the app

```bash
uvicorn main:app --host 0.0.0.0 --port 8000
```

Check it's working:
```
http://localhost:8000/health
```

---

## Expose publicly with ngrok

ElevenLabs needs a public URL to reach your local app.

### Install ngrok
Download from https://ngrok.com/download and sign up for a free account.

### Run ngrok
In a separate terminal:
```bash
ngrok http 8000
```

You'll see something like:
```
Forwarding  https://abc123.ngrok-free.app -> http://localhost:8000
```

Copy that `https://...ngrok-free.app` URL — you'll need it in the next step.

---

## Configure your ElevenLabs Agent

1. Go to https://elevenlabs.io/app/conversational-ai
2. Open your agent
3. Go to **"LLM"** settings
4. Change LLM to **"Custom LLM"**
5. Set the URL to:
   ```
   https://your-ngrok-url.ngrok-free.app/v1/chat/completions
   ```
6. Save the agent

That's it! Your agent will now use Claude for all responses.

---

## Notes

- The ngrok URL changes every time you restart ngrok (free tier).
  Remember to update the ElevenLabs agent config each time.
- The system prompt you set in ElevenLabs (e.g. your Vodafone sales prompt)
  is passed through to Claude automatically.
- To change Claude model, edit `CLAUDE_MODEL` in `.env`.
