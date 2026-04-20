"""
ElevenLabs <-> CodemIE Bridge
Exposes an OpenAI-compatible /chat/completions endpoint.
ElevenLabs agent calls this as its custom LLM.
Translates to CodemIE's internal API format using cookie auth.

Cookie note: oauth2_proxy splits large tokens across multiple cookies
with the same name. We store them as CODEMIE_OAUTH_PROXY_0_A and
CODEMIE_OAUTH_PROXY_0_B (and _1) and send them as a raw Cookie header
so duplicates are preserved correctly.

Conversation mapping: ElevenLabs traceparent trace IDs are mapped to
CodemIE conversation IDs in an in-memory dict. This is sufficient for
a demo but should be replaced with DynamoDB for production.
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

CODEMIE_ENDPOINT          = os.getenv("CODEMIE_ENDPOINT", "https://codemie.lab.epam.com/code-assistant-api/v1/assistants")
CODEMIE_ASSISTANT_ID      = os.getenv("CODEMIE_ASSISTANT_ID")
CODEMIE_ASSISTANT_FOLDER  = os.getenv("CODEMIE_ASSISTANT_FOLDER", "")
CODEMIE_LLM_MODEL         = os.getenv("CODEMIE_LLM_MODEL", "claude-haiku-4-5-20251001")
CODEMIE_CONVERSATIONS_URL = CODEMIE_ENDPOINT.rsplit("/assistants", 1)[0] + "/conversations"

OAUTH_PROXY_0_A = os.getenv("CODEMIE_OAUTH_PROXY_0_A")
OAUTH_PROXY_0_B = os.getenv("CODEMIE_OAUTH_PROXY_0_B")
OAUTH_PROXY_1   = os.getenv("CODEMIE_OAUTH_PROXY_1")

if not CODEMIE_ASSISTANT_ID:
    raise RuntimeError("CODEMIE_ASSISTANT_ID is not set")
if not OAUTH_PROXY_0_A or not OAUTH_PROXY_1:
    raise RuntimeError("CODEMIE_OAUTH_PROXY_0_A and CODEMIE_OAUTH_PROXY_1 must be set")


# ── Conversation store ─────────────────────────────────────────────────────────
# Maps ElevenLabs traceparent trace ID -> CodemIE conversation ID.
# In-memory for demo purposes. Replace with DynamoDB for production.
_conversation_store: dict[str, str] = {}


async def get_or_create_conversation(elevenlabs_id: str) -> str:
    """
    Look up the CodemIE conversation ID for a given ElevenLabs trace ID.
    If none exists, create a new CodemIE conversation and store the mapping.
    """
    if elevenlabs_id in _conversation_store:
        codemie_id = _conversation_store[elevenlabs_id]
        logger.info("[%s] Reusing CodemIE conversation: %s", elevenlabs_id, codemie_id)
        return codemie_id

    codemie_id = await create_conversation()
    _conversation_store[elevenlabs_id] = codemie_id
    logger.info("[%s] Created new CodemIE conversation: %s", elevenlabs_id, codemie_id)
    return codemie_id


def get_elevenlabs_id(request: Request) -> str:
    """
    Extract a stable session ID from the ElevenLabs request.
    Uses the trace ID from the W3C traceparent header, which remains
    constant across all requests in the same ElevenLabs conversation.
    Falls back to a new UUID if the header is missing.
    """
    traceparent = request.headers.get("traceparent", "")
    parts = traceparent.split("-")
    if len(parts) >= 4:
        return parts[1]
    return str(uuid.uuid4())


# ── Codemie:
    """
    Build a raw Cookie header string that preserves duplicate cookie names.
    """
    parts = [f"_oauth2_proxy_0={OAUTH_PROXY_0_A}"]
    if OAUTH_PROXY_0_B:
        parts.append(f"_oauth2_proxy_0={OAUTH_PROXY_0_B}")
    parts.append(f"_oauth2_proxy_1={OAUTH_PROXY_1}")
    return "; ".join(parts)


async def create_conversation() -> str:
    """
    Create a new CodemIE conversation and return its conversation_id.
    """
    payload = {
        "initial_assistant_id": CODEMIE_ASSISTANT_ID,
        "folder": CODEMIE_ASSISTANT_FOLDER,
        "is_workflow": False,
    }
    headers = {
        "Content-Type": "application/json",
        "Accept": "*/*",
        "Origin": "https://codemie.lab.epam.com",
        "Referer": "https://codemie.lab.epam.com/",
        "User-Agent": "Mozilla/5.0 (ElevenLabs-Bridge/1.0)",
        "Cookie": build_cookie_header(),
    }
    async with httpx.AsyncClient(timeout=15.0) as client:
        response = await client.post(CODEMIE_CONVERSATIONS_URL, json=payload, headers=headers)
        if response.status_code not in (200, 201):
            raise HTTPException(
                status_code=502,
                detail=f"Failed to create conversation: CodemIE returned {response.status_code}"
            )
        data = response.json()
        conversation_id = data.get("conversation_id") or data.get("id")
        if not conversation_id:
            raise HTTPException(status_code=502, detail="CodemIE returned no conversation_id")
        return conversation_id


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
            continue
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
    elevenlabs_id: str,
) -> AsyncGenerator[str, None]:
    """
    Call CodemIE, parse the streaming JSON chunks,
    and re-emit as OpenAI SSE format for ElevenLabs.
    """
    url = f"{CODEMIE_ENDPOINT}/{CODEMIE_ASSISTANT_ID}/model"
    payload = build_codemie_request(messages, conversation_id)

    headers = {
        "Content-Type": "application/json",
        "Accept": "*/*",
        "Origin": "https://codemie.lab.epam.com",
        "Referer": "https://codemie.lab.epam.com/",
        "User-Agent": "Mozilla/5.0 (ElevenLabs-Bridge/1.0)",
        "Cookie": build_cookie_header(),
    }

    full_response = []

    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            async with client.stream(
                "POST", url,
                json=payload,
                headers=headers,
            ) as response:

                if response.status_code != 200:
                    body = await response.aread()
                    logger.error(
                        "[%s] CodemIE returned %d: %s",
                        elevenlabs_id, response.status_code, body.decode()
                    )
                    raise HTTPException(
                        status_code=502,
                        detail=f"CodemIE returned {response.status_code}"
                    )

                buffer = ""
                async for raw_chunk in response.aiter_text():
                    buffer += raw_chunk

                    while buffer:
                        try:
                            obj, idx = json.JSONDecoder().raw_decode(buffer)
                            buffer = buffer[idx:].lstrip()
                        except json.JSONDecodeError:
                            break

                        thought = obj.get("thought")
                        is_last = obj.get("last", False)

                        if is_last:
                            # Just capture for logging — don't re-send,
                            # content was already streamed via thought chunks
                            generated = obj.get("generated", "")
                            if generated:
                                full_response = [generated]
                        elif thought and thought.get("in_progress") and thought.get("message"):
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

        logger.info("[%s] ASSISTANT: %s", elevenlabs_id, "".join(full_response))

    except HTTPException:
        raise
    except Exception as e:
        logger.error("[%s] Error calling CodemIE: %s", elevenlabs_id, str(e))
        raise HTTPException(status_code=502, detail=str(e))

    done_chunk = {
        "object": "chat.completion.chunk",
        "choices": [{"delta": {}, "index": 0, "finish_reason": "stop"}],
    }
    yield f"data: {json.dumps(done_chunk)}\n\n"
    yield "data: [DONE]\n\n"


# ── Routes ─────────────────────────────────────────────────────────────────────

@app.post("/chat/completions")
@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    # Extract stable ElevenLabs session ID from traceparent header
    elevenlabs_id = get_elevenlabs_id(request)

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    messages = body.get("messages", [])
    if not messages:
        raise HTTPException(status_code=400, detail="No messages provided")

    # Get or create a CodemIE conversation for this ElevenLabs session
    codemie_conversation_id = await get_or_create_conversation(elevenlabs_id)

    user_messages = [m for m in messages if m.get("role") == "user"]
    if user_messages:
        logger.info("[%s] USER: %s", elevenlabs_id, user_messages[-1]["content"])

    return StreamingResponse(
        stream_codemie_response(messages, codemie_conversation_id, elevenlabs_id),
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
        "active_conversations": len(_conversation_store),
    }


@app.get("/ping")
async def ping():
    """
    Quick sanity check — sends a hello to CodemIE and returns the response.
    Hit this in a browser to verify cookies and connectivity are working.
    """
    messages = [{"role": "user", "content": "Hello! Please respond with a short greeting."}]
    ping_elevenlabs_id = "ping-" + str(uuid.uuid4())[:8]
    conversation_id = await create_conversation()

    url = f"{CODEMIE_ENDPOINT}/{CODEMIE_ASSISTANT_ID}/model"
    payload = build_codemie_request(messages, conversation_id)

    headers = {
        "Content-Type": "application/json",
        "Accept": "*/*",
        "Origin": "https://codemie.lab.epam.com",
        "Referer": "https://codemie.lab.epam.com/",
        "User-Agent": "Mozilla/5.0 (ElevenLabs-Bridge/1.0)",
        "Cookie": build_cookie_header(),
    }

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            async with client.stream("POST", url, json=payload, headers=headers) as response:
                if response.status_code != 200:
                    body = await response.aread()
                    return {
                        "status": "error",
                        "http_status": response.status_code,
                        "detail": body.decode(),
                    }

                buffer = ""
                async for raw_chunk in response.aiter_text():
                    buffer += raw_chunk
                    while buffer:
                        try:
                            obj, idx = json.JSONDecoder().raw_decode(buffer)
                            buffer = buffer[idx:].lstrip()
                        except json.JSONDecodeError:
                            break
                        if obj.get("last"):
                            generated = obj.get("generated", "")
                            logger.info("[%s] PING response: %s", ping_elevenlabs_id, generated)
                            return {"status": "ok", "response": generated}

        return {"status": "error", "detail": "No response received from CodemIE"}

    except Exception as e:
        logger.error("[%s] Ping failed: %s", ping_elevenlabs_id, str(e))
        return {"status": "error", "detail": str(e)}
