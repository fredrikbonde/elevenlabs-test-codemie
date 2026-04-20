# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this project does

A single-file FastAPI bridge that connects ElevenLabs voice agents to EPAM's internal CodemIE LLM platform. It exposes an OpenAI-compatible `/chat/completions` endpoint so ElevenLabs can talk to CodemIE as if it were OpenAI.

```
ElevenLabs voice agent → this app (OpenAI API format) → CodemIE (EPAM internal LLM)
```

## Commands

```bash
pip install -r requirements.txt
uvicorn main:app --host 0.0.0.0 --port 8080
```

Health checks:
- `GET /health` — service status + model info
- `GET /ping` — test CodemIE connectivity

## Architecture

Everything lives in `main.py`. Key components:

- **`/chat/completions` and `/v1/chat/completions`** — accept OpenAI-format requests, return OpenAI-format SSE streaming responses
- **`build_codemie_request()`** — translates OpenAI `messages[]` to CodemIE format (system prompt + history + current user message wrapped in `<p>` tags)
- **`stream_codemie_response()`** — async HTTP streaming call to CodemIE, converts JSON chunks to OpenAI SSE format
- **`TokenCache`** — fetches and caches a Keycloak bearer token (ROPC flow); proactively refreshes when <1 hour remains; authenticates on startup
- **`get_auth_headers()`** — returns headers with `Authorization: Bearer <token>` for all CodemIE calls
- **`get_elevenlabs_id()`** — extracts the stable trace ID from ElevenLabs' `traceparent` header to use as session key
- **`get_or_create_conversation()`** — maps ElevenLabs session ID → CodemIE conversation ID using an in-memory dict; creates a new conversation via `create_conversation()` on first turn
- **`create_conversation()`** — POSTs to CodemIE's `/v1/conversations` to obtain a `conversation_id`

## Configuration

Required environment variables (in `.env` locally, AWS Secrets Manager in prod):

| Variable | Purpose |
|---|---|
| `CODEMIE_ENDPOINT` | CodemIE API base URL |
| `CODEMIE_ASSISTANT_ID` | Which CodemIE assistant to use |
| `CODEMIE_ASSISTANT_FOLDER` | Human-readable name of the assistant (used when creating conversations) |
| `CODEMIE_LLM_MODEL` | Model name (e.g. `claude-haiku-4-5-20251001`) |
| `KEYCLOAK_URL` | Keycloak token endpoint (has a sensible default) |
| `KEYCLOAK_CLIENT` | Keycloak client ID (default: `codemie-sdk`) |
| `CODEMIE_USERNAME` | Service account username |
| `CODEMIE_PASSWORD` | Service account password |

## Known issues

- Conversation store is in-memory (`_conversation_store` dict) — sufficient for demo, replace with DynamoDB for production
- `/health` now exposes `token_valid` and `token_expires_in_seconds` for monitoring token state
- No test suite exists
- Deployed to AWS App Runner via `apprunner.yaml`
