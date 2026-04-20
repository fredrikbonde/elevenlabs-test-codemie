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

### Bearer token authentication (Keycloak)
Auth uses Keycloak ROPC (Resource Owner Password Credentials) flow with a service account (`CODEMIE_USERNAME` / `CODEMIE_PASSWORD`). This replaced the previous browser cookie approach.

- On startup, `TokenCache._authenticate()` fetches an access token and refresh token from Keycloak
- `TokenCache.get_token()` is called before every CodemIE request; it returns the cached token if more than 1 hour remains, otherwise refreshes proactively
- If the refresh token has also expired, it falls back to full re-authentication
- All CodemIE requests use `Authorization: Bearer <token>` header via `get_auth_headers()`
- `/health` exposes `token_valid` and `token_expires_in_seconds` for monitoring

### Conversation creation
Before sending a message to CodemIE, a conversation must be created via:
```
POST https://codemie.lab.epam.com/code-assistant-api/v1/conversations
{"initial_assistant_id": "<CODEMIE_ASSISTANT_ID>", "folder": "<CODEMIE_ASSISTANT_FOLDER>", "is_workflow": false}
```
Response contains `conversation_id` (also duplicated as `id`). This ID is passed as `conversationId` in every subsequent message request.

The conversations base URL is derived from `CODEMIE_ENDPOINT` by replacing `/assistants` with `/conversations`.

ElevenLabs includes a `traceparent` header on every request. The trace ID (second `-`-separated segment) is stable across all turns in a session. `get_elevenlabs_id()` extracts this and `get_or_create_conversation()` uses it to look up or create a CodemIE conversation ID, stored in `_conversation_store` (in-memory dict). Replace with DynamoDB for production.

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
| `CODEMIE_ASSISTANT_ID` | CodemIE assistant UUID | No |
| `CODEMIE_ASSISTANT_FOLDER` | Human-readable name of the assistant (used when creating conversations) | No |
| `CODEMIE_LLM_MODEL` | LLM model name | No |
| `KEYCLOAK_URL` | Keycloak token endpoint (has a default value) | No |
| `KEYCLOAK_CLIENT` | Keycloak client ID (default: `codemie-sdk`) | No |
| `CODEMIE_USERNAME` | Service account username for Keycloak | Yes |
| `CODEMIE_PASSWORD` | Service account password for Keycloak | Yes |

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
