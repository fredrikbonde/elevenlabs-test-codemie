# Project Context for Claude Code

This document captures the full context of this project for continuity. Read this before making any changes.

---

## What this project is

A FastAPI bridge app that connects an **ElevenLabs voice agent** to **CodemIE** (EPAM's internal LLM platform). It allows a voice conversation via ElevenLabs to be powered by CodemIE's LLM backend instead of ElevenLabs' default LLM.

The app was originally built to use the **Anthropic Claude API** directly, but was switched to CodemIE because EPAM's "greater powers" require use of the internal LLM platform. The Claude version of `main.py` still exists in git history if needed.

---

## Architecture

```
User (voice)
    ↕  WebSocket
ElevenLabs Agent
    ↕  HTTP POST (OpenAI-compatible)
This FastAPI app (AWS App Runner, eu-central-1)
    ↕  HTTP POST (CodemIE internal format)
CodemIE (https://codemie.lab.epam.com)
    ↕
Underlying LLM (claude-haiku-4-5-20251001 configured)
```

---

## Key technical decisions and why

### OpenAI-compatible endpoint
ElevenLabs custom LLM integration expects an OpenAI-compatible `/chat/completions` endpoint. We expose both `/chat/completions` and `/v1/chat/completions` since ElevenLabs strips the `/v1` prefix.

### Streaming
Both legs stream — we use `httpx` async streaming to forward CodemIE chunks to ElevenLabs in real time. This is critical for voice latency so ElevenLabs can start TTS before the full response is ready.

### CodemIE response format
CodemIE does NOT use standard SSE (`data: {...}\n\n`). It streams raw JSON objects concatenated back-to-back:
```json
{"thought": {"in_progress": true, "message": "Hello", ...}, "last": false, ...}
{"thought": {"in_progress": true, "message": " world", ...}, "last": false, ...}
{"generated": "Hello world", "last": true, ...}
```
- Intermediate chunks: text is in `thought.message`
- Final chunk: `last: true`, full text in `generated` (we skip re-sending this to avoid duplication)
- We parse these using `json.JSONDecoder().raw_decode()` in a buffer loop

### Cookie authentication
CodemIE has no API key mechanism for external/programmatic access (this is a PoC — proper credentials to be requested later). Authentication uses browser session cookies stolen via Firefox DevTools.

`oauth2_proxy` splits large tokens across multiple cookies with the **same name**:
- `_oauth2_proxy_0` appears **twice** (stored as `CODEMIE_OAUTH_PROXY_0_A` and `CODEMIE_OAUTH_PROXY_0_B`)
- `_oauth2_proxy_1` appears once

We send these as a **raw Cookie header string** (not a dict) to preserve duplicates. All `_ga_*` and `__cf_bm` cookies are analytics/CDN and not needed.

⚠️ Cookies expire periodically. When you get 401 errors, refresh them from Firefox DevTools.

### Conversation history
We rely entirely on ElevenLabs to maintain conversation history. ElevenLabs sends the full message history on every request (standard OpenAI chat completions behaviour). We convert this to CodemIE's `history` array format.

---

## CodemIE API format

### Request
```
POST https://codemie.lab.epam.com/code-assistant-api/v1/assistants/{assistantId}/model
```

```json
{
  "conversationId": "uuid",
  "text": "latest user message",
  "contentRaw": "<p>latest user message</p>",
  "file_names": [],
  "llmModel": "claude-haiku-4-5-20251001",
  "history": [
    {"role": "User", "message": "...", "createdAt": "ISO timestamp"},
    {"role": "Assistant", "message": "...", "createdAt": "ISO timestamp", "assistantId": "..."}
  ],
  "historyIndex": 2,
  "mcpServerSingleUsage": false,
  "workflowExecutionId": null,
  "stream": true,
  "topK": 10,
  "systemPrompt": "",
  "backgroundTask": false,
  "metadata": null,
  "toolsConfig": [],
  "outputSchema": null
}
```

### Response (streaming)
Raw JSON objects concatenated, no SSE wrapper:
```json
{"thought": {"id": "...", "in_progress": true, "message": "chunk", "author_type": "Tool", ...}, "last": false, ...}
{"time_elapsed": 4.9, "generated": "full response text", "last": true, ...}
```

---

## Environment variables

| Variable | Description | Secret? |
|---|---|---|
| `CODEMIE_ENDPOINT` | Base URL for CodemIE API | No |
| `CODEMIE_ASSISTANT_ID` | ElevenLabs/CodemIE assistant UUID | No |
| `CODEMIE_ASSISTANT_FOLDER` | Human-readable name of the assistant (used when creating conversations) | No |
| `CODEMIE_LLM_MODEL` | LLM model name | No |
| `CODEMIE_OAUTH_PROXY_0_A` | First `_oauth2_proxy_0` cookie | Yes (SSM) |
| `CODEMIE_OAUTH_PROXY_0_B` | Second `_oauth2_proxy_0` cookie | Yes (SSM) |
| `CODEMIE_OAUTH_PROXY_1` | `_oauth2_proxy_1` cookie | Yes (SSM) |

⚠️ SSM SecureString has a 4096 character limit. Cookie values may exceed this — use **AWS Secrets Manager** if needed (65536 char limit).

---

## AWS App Runner deployment

- **Region**: `eu-central-1` (Frankfurt)
- **Source**: GitHub repo, auto-deploy on push
- **Config**: `apprunner.yaml` in repo root
- **Instance role**: `Apprunner-FastApi-Bridge_Role` with `AmazonSSMReadOnlyAccess`
- **Port**: 8080

### Important apprunner.yaml quirk
App Runner's managed Python runtime has a split build/run environment — packages installed in `build` don't persist to `run`. The fix is to reinstall in `pre-run`:

```yaml
build:
  commands:
    build:
      - pip3 install -r requirements.txt
run:
  pre-run:
    - pip3 install -r requirements.txt  # Must repeat this!
  command: uvicorn main:app --host 0.0.0.0 --port 8080
```

---

## API endpoints

| Endpoint | Method | Description |
|---|---|---|
| `/health` | GET | Service status |
| `/ping` | GET | Sends test message to CodemIE, returns response — useful for verifying cookies |
| `/chat/completions` | POST | OpenAI-compatible endpoint (used by ElevenLabs) |
| `/v1/chat/completions` | POST | Same, alternate path |

---

## ElevenLabs agent configuration

- Agent is configured with a **Vodafone sales** system prompt (PoC demo)
- Custom LLM URL: `https://your-url.eu-central-1.awsapprunner.com` (no path — ElevenLabs appends `/chat/completions`)
- System prompt is passed through to CodemIE via the `systemPrompt` field

---

## Known issues / future work

1. **Cookie expiry** — manual process to refresh. Next step: get proper API credentials from CodemIE platform team
2. **Latency** — extra hop through App Runner adds some latency vs direct LLM call. Still acceptable for PoC
3. **ElevenLabs server region** — likely US by default, adding transatlantic latency. Enterprise plan allows EU data residency
4. **No monitoring** — CloudWatch logs exist but no alerting set up
5. **Response duplication fixed** — earlier bug where CodemIE response was sent twice (once via thought chunks, once via final `generated` field). Fixed by skipping the `generated` field re-send

---

## Dependencies

```
httpx>=0.27.0
fastapi>=0.111.0
uvicorn[standard]>=0.30.0
python-dotenv>=1.0.0
```

Note: `anthropic` package was removed when switching from Claude API to CodemIE.

---

## GitHub repo

`https://github.com/fredrikbonde/elevenlabs-test`
