"""
ElevenLabs <-> CodemIE Bridge
Exposes an OpenAI-compatible /chat/completions endpoint.
ElevenLabs agent calls this as its custom LLM.
Translates to CodemIE's internal API format using cookie auth.
"""

import os
import json
import logging
import uuid
from datetime import datetime, timezone
from typing import AsyncGenerator

import httpx
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import StreamingResponse
from dotenv import load_dotenv

load_dotenv()

# ── Logging ────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%SZ",
)
logger = logging.getLogger(__name__)

# ── Config ─────────────────────────────────────────────────────────────────────
app = FastAPI(title="ElevenLabs-CodemIE Bridge")

CODEMIE_ENDPOINT     = os.getenv("CODEMIE_ENDPOINT", "https://codemie.lab.epam.com/code-assistant-api/v1/assistants")
CODEMIE_ASSISTANT_ID = os.getenv("CODEMIE_ASSISTANT_ID")
CODEMIE_LLM_MODEL    = os.getenv("CODEMIE_LLM_MODEL", "claude-haiku-4-5-20251001")
OAUTH_PROXY_0        = os.getenv("CODEMIE_OAUTH_PROXY_0")
OAUTH_PROXY_1        = os.getenv("CODEMIE_OAUTH_PROXY_1")

if not CODEMIE_ASSISTANT_ID:
    raise RuntimeError("CODEMIE_ASSISTANT_ID is not set")
if not OAUTH_PROXY_0 or not OAUTH_PROXY_1:
    raise RuntimeError("CODEMIE_OAUTH_PROXY_0 and CODEMIE_OAUTH_PROXY_1 must be set")


def build_codemie_request(messages: list, conversation_id: str) -> dict:
    """
    Convert OpenAI-style messages into CodemIE request format.
    The last user message becomes 'text', prior turns become 'history'.
    """
    now = datetime.now(timezone.utc).isoformat()
    history = []
    last_user_text = ""

    for msg in messages:
        role = msg.get("role", "")
        content = msg.get("content", "")

        if role == "system":
            continue  # CodemIE uses systemPrompt field, handled separately
        elif role == "user":
            last_user_text = content
            history.append({
                "role": "User",
                "message": content,
                "createdAt": now,
            })
        elif role == "assistant":
            history.append({
                "role": "Assistant",
                "message": content,
                "createdAt": now,
                "assistantId": CODEMIE_ASSISTANT_ID,
            })

    # Remove the last user message from history (it becomes 'text')
    if history and history[-1]["role"] == "User":
        history = history[:-1]

    # Extract system prompt if present
    system_prompt = next(
        (m.get("content", "") for m in messages if m.get("role") == "system"),
        ""
    )

    return {
        "conversationId": conversation_id,
        "text": last_user_text,
        "contentRaw": f"<p>{last_user_text}</p>",
        "file_names": [],
        "llmModel": CODEMIE_LLM_MODEL,
        "history": history,
        "historyIndex": len(history),
        "mcpServerSingleUsage": False,
        "workflowExecutionId": None,
        "stream": True,
        "topK": 10,
        "systemPrompt": system_prompt,
        "backgroundTask": False,
        "metadata": None,
        "toolsConfig": [],
        "outputSchema": None,
    }


async def stream_codemie_response(
    messages: list,
    conversation_id: str,
) -> AsyncGenerator[str, None]:
    """
    Call CodemIE, parse the streaming JSON chunks,
    and re-emit as OpenAI SSE format for ElevenLabs.
    """
    url = f"{CODEMIE_ENDPOINT}/{CODEMIE_ASSISTANT_ID}/model"
    payload = build_codemie_request(messages, conversation_id)

    cookies = {
        "_oauth2_proxy_0": OAUTH_PROXY_0,
        "_oauth2_proxy_1": OAUTH_PROXY_1,
    }

    headers = {
        "Content-Type": "application/json",
        "Accept": "*/*",
        "Origin": "https://codemie.lab.epam.com",
        "Referer": "https://codemie.lab.epam.com/",
        "User-Agent": "Mozilla/5.0 (ElevenLabs-Bridge/1.0)",
    }

    full_response = []

    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            async with client.stream(
                "POST", url,
                json=payload,
                headers=headers,
                cookies=cookies,
            ) as response:

                if response.status_code != 200:
                    body = await response.aread()
                    logger.error(
                        "[%s] CodemIE returned %d: %s",
                        conversation_id, response.status_code, body.decode()
                    )
                    raise HTTPException(
                        status_code=502,
                        detail=f"CodemIE returned {response.status_code}"
                    )

                buffer = ""
                async for raw_chunk in response.aiter_text():
                    buffer += raw_chunk

                    # CodemIE streams raw JSON objects back-to-back, not newline delimited
                    # We parse them out greedily
                    while buffer:
                        try:
                            obj, idx = json.JSONDecoder().raw_decode(buffer)
                            buffer = buffer[idx:].lstrip()
                        except json.JSONDecodeError:
                            break  # Incomplete chunk, wait for more data

                        thought = obj.get("thought")
                        is_last = obj.get("last", False)

                        if is_last:
                            # Final chunk — use the complete 'generated' field
                            generated = obj.get("generated", "")
                            if generated:
                                full_response = [generated]
                                chunk = {
                                    "object": "chat.completion.chunk",
                                    "choices": [{
                                        "delta": {"content": generated},
                                        "index": 0,
                                        "finish_reason": None,
                                    }],
                                }
                                yield f"data: {json.dumps(chunk)}\n\n"
                        elif thought and thought.get("in_progress") and thought.get("message"):
                            # Intermediate thought chunk
                            text = thought["message"]
                            full_response.append(text)
                            chunk = {
                                "object": "chat.completion.chunk",
                                "choices": [{
                                    "delta": {"content": text},
                                    "index": 0,
                                    "finish_reason": None,
                                }],
                            }
                            yield f"data: {json.dumps(chunk)}\n\n"

        logger.info("[%s] ASSISTANT: %s", conversation_id, "".join(full_response))

    except HTTPException:
        raise
    except Exception as e:
        logger.error("[%s] Error calling CodemIE: %s", conversation_id, str(e))
        raise HTTPException(status_code=502, detail=str(e))

    # OpenAI SSE terminator
    done_chunk = {
        "object": "chat.completion.chunk",
        "choices": [{"delta": {}, "index": 0, "finish_reason": "stop"}],
    }
    yield f"data: {json.dumps(done_chunk)}\n\n"
    yield "data: [DONE]\n\n"


@app.post("/chat/completions")
@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    conversation_id = str(uuid.uuid4())

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    messages = body.get("messages", [])
    if not messages:
        raise HTTPException(status_code=400, detail="No messages provided")

    # Log incoming user message
    user_messages = [m for m in messages if m.get("role") == "user"]
    if user_messages:
        logger.info("[%s] USER: %s", conversation_id, user_messages[-1]["content"])

    return StreamingResponse(
        stream_codemie_response(messages, conversation_id),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "backend": "CodemIE",
        "model": CODEMIE_LLM_MODEL,
        "assistant_id": CODEMIE_ASSISTANT_ID,
    }
