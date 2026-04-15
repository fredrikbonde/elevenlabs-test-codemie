"""
ElevenLabs <-> Claude Bridge
Exposes an OpenAI-compatible /chat/completions endpoint.
ElevenLabs agent calls this as its custom LLM.
"""

import os
import json
import logging
import uuid
from datetime import datetime, timezone
from typing import AsyncGenerator

import anthropic
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import StreamingResponse
from dotenv import load_dotenv

load_dotenv()

# ── Logging setup ──────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%SZ",
)
logger = logging.getLogger(__name__)

# ── Config ─────────────────────────────────────────────────────────────────────
app = FastAPI(title="ElevenLabs-Claude Bridge")

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "claude-sonnet-4-20250514")
MAX_TOKENS = int(os.getenv("MAX_TOKENS", "1024"))

if not ANTHROPIC_API_KEY:
    raise RuntimeError("ANTHROPIC_API_KEY is not set in .env")

client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)


def convert_messages(messages: list) -> tuple[str | None, list]:
    """
    Split OpenAI-style messages into a system prompt + user/assistant turns
    that Anthropic's API expects.
    """
    system_prompt = None
    converted = []

    for msg in messages:
        role = msg.get("role")
        content = msg.get("content", "")

        if role == "system":
            system_prompt = content
        elif role in ("user", "assistant"):
            converted.append({"role": role, "content": content})

    return system_prompt, converted


async def stream_claude_response(
    system_prompt: str | None,
    messages: list,
    conversation_id: str,
) -> AsyncGenerator[str, None]:
    """
    Stream a Claude response as Server-Sent Events in OpenAI format
    so ElevenLabs can consume it.
    """
    kwargs = {
        "model": CLAUDE_MODEL,
        "max_tokens": MAX_TOKENS,
        "messages": messages,
    }
    if system_prompt:
        kwargs["system"] = system_prompt

    full_response = []

    try:
        with client.messages.stream(**kwargs) as stream:
            for text_chunk in stream.text_stream:
                full_response.append(text_chunk)
                chunk = {
                    "object": "chat.completion.chunk",
                    "choices": [
                        {
                            "delta": {"content": text_chunk},
                            "index": 0,
                            "finish_reason": None,
                        }
                    ],
                }
                yield f"data: {json.dumps(chunk)}\n\n"

        # Log the full assistant response once streaming is complete
        logger.info(
            "[%s] ASSISTANT: %s",
            conversation_id,
            "".join(full_response),
        )

    except Exception as e:
        logger.error("[%s] Error streaming Claude response: %s", conversation_id, str(e))
        raise

    # Send the final [DONE] marker
    done_chunk = {
        "object": "chat.completion.chunk",
        "choices": [
            {
                "delta": {},
                "index": 0,
                "finish_reason": "stop",
            }
        ],
    }
    yield f"data: {json.dumps(done_chunk)}\n\n"
    yield "data: [DONE]\n\n"


@app.post("/chat/completions")
@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    conversation_id = str(uuid.uuid4())[:8]

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    messages = body.get("messages", [])
    if not messages:
        raise HTTPException(status_code=400, detail="No messages provided")

    system_prompt, converted_messages = convert_messages(messages)

    # Log incoming system prompt
    if system_prompt:
        logger.info("[%s] SYSTEM: %s", conversation_id, system_prompt)

    # Log the latest user message
    user_messages = [m for m in converted_messages if m["role"] == "user"]
    if user_messages:
        logger.info("[%s] USER: %s", conversation_id, user_messages[-1]["content"])

    logger.info(
        "[%s] Request: model=%s, messages=%d, stream=%s",
        conversation_id,
        CLAUDE_MODEL,
        len(converted_messages),
        body.get("stream", True),
    )

    stream = body.get("stream", True)

    if stream:
        return StreamingResponse(
            stream_claude_response(system_prompt, converted_messages, conversation_id),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )
    else:
        try:
            response = client.messages.create(
                model=CLAUDE_MODEL,
                max_tokens=MAX_TOKENS,
                messages=converted_messages,
                **({"system": system_prompt} if system_prompt else {}),
            )
            text = response.content[0].text
            logger.info("[%s] ASSISTANT: %s", conversation_id, text)
            return {
                "object": "chat.completion",
                "choices": [
                    {
                        "message": {"role": "assistant", "content": text},
                        "index": 0,
                        "finish_reason": "stop",
                    }
                ],
            }
        except Exception as e:
            logger.error("[%s] Error calling Claude: %s", conversation_id, str(e))
            raise HTTPException(status_code=500, detail=str(e))


@app.get("/health")
async def health():
    return {"status": "ok", "model": CLAUDE_MODEL}
