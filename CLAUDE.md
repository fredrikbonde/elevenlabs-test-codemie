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
- **`build_cookie_header()`** — constructs `oauth2_proxy` auth cookies from three env vars (`CODEMIE_OAUTH_PROXY_0_A`, `CODEMIE_OAUTH_PROXY_0_B`, `CODEMIE_OAUTH_PROXY_1`); `_oauth2_proxy_0` appears twice to split large tokens

## Configuration

Required environment variables (in `.env` locally, AWS Secrets Manager in prod):

| Variable | Purpose |
|---|---|
| `CODEMIE_ENDPOINT` | CodemIE API base URL |
| `CODEMIE_ASSISTANT_ID` | Which CodemIE assistant to use |
| `CODEMIE_LLM_MODEL` | Model name (e.g. `claude-haiku-4-5-20251001`) |
| `CODEMIE_OAUTH_PROXY_0_A` | First half of `_oauth2_proxy_0` cookie |
| `CODEMIE_OAUTH_PROXY_0_B` | Second half of `_oauth2_proxy_0` cookie |
| `CODEMIE_OAUTH_PROXY_1` | `_oauth2_proxy_1` cookie |

OAuth cookies expire and must be manually refreshed — this is a known limitation.

## Known issues

- Conversation ID is hardcoded (line ~217 in `main.py`) — should be unique per conversation in production
- No test suite exists
- Deployed to AWS App Runner via `apprunner.yaml`
