"""
ElevenLabs <-> CodemIE Bridge
Exposes an OpenAI-compatible /chat/completions endpoint.
ElevenLabs agent calls this as its custom LLM.
Translates to CodemIE's internal API format using Keycloak bearer token auth.

Auth: Uses Keycloak ROPC flow to obtain a bearer token on startup.
Token is cached and refreshed proactively when less than 1 hour remaining.

Conversation mapping: ElevenLabs traceparent trace IDs are mapped to
CodemIE conversation IDs in an in-memory dict. Sufficient for demo —
replace with DynamoDB for production.
"""

import os
import json
import logging
import uuid
import time
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

KEYCLOAK_URL    = os.getenv("KEYCLOAK_URL", "https://auth.codemie.lab.epam.com/realms/codemie-prod/protocol/openid-connect/token")
KEYCLOAK_CLIENT = os.getenv("KEYCLOAK_CLIENT", "codemie-sdk")
CODEMIE_USERNAME = os.getenv("CODEMIE_USERNAME")
CODEMIE_PASSWORD = os.getenv("CODEMIE_PASSWORD")

if not CODEMIE_ASSISTANT_ID:
    raise RuntimeError("CODEMIE_ASSISTANT_ID is not set")
if not CODEMIE_USERNAME or not CODEMIE_PASSWORD:
    raise RuntimeError("CODEMIE_USERNAME and CODEMIE_PASSWORD must be set")


# ── Token cache ────────────────────────────────────────────────────────────────

class TokenCache:
    """
    Caches the Keycloak bearer token and handles proactive refresh.
    Refreshes when less than 1 hour (3600s) remaining on the access token.
    Falls back to full re-auth if refresh token has also expired.
    """
    def __init__(self):
        self.access_token: str | None = None
        self.refresh_token: str | None = None
        self.access_expires_at: float = 0
        self.refresh_expires_at: float = 0

    def is_access_token_valid(self) -> bool:
        return self.access_token is not None and time.time() < (self.access_expires_at - 3600)

    def is_refresh_token_valid(self) -> bool:
        return self.refresh_token is not None and time.time() < (self.refresh_expires_at - 60)

    async def get_token(self) -> str:
        if self.is_access_token_valid():
            return self.access_token

        if self.is_refresh_token_valid():
            await self._refresh()
        else:
            await self._authenticate()

        return self.access_token

    async def _authenticate(self):
        logger.info("Authenticating with Keycloak (full auth)...")
        data = {
            "grant_type": "password",
            "client_id": KEYCLOAK_CLIENT,
            "username": CODEMIE_USERNAME,
            "password": CODEMIE_PASSWORD,
        }
        await self._fetch_token(data)
        logger.info("Keycloak authentication successful, token valid for %ds", 
                   int(self.access_expires_at - time.time()))

    async def _refresh(self):
        logger.info("Refreshing Keycloak token...")
        data = {
            "grant_type": "refresh_token",
            "client_id": KEYCLOAK_CLIENT,
            "refresh_token": self.refresh_token,
        }
        try:
            await self._fetch_token(data)
            logger.info("Token refreshed successfully")
        except Exception as e:
            logger.warning("Token refresh failed, falling back to full auth: %s", e)
            await self._authenticate()

    async def _fetch_token(self, data: dict):
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.post(
                KEYCLOAK_URL,
                data=data,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            if response.status_code != 200:
                raise HTTPException(
                    status_code=502,
                    detail=f"Keycloak returned {response.status_code}: {response.text}"
                )
            token_data = response.json()
            now = time.time()
            self.access_token = token_data["access_token"]
            self.refresh_token = token_data.get("refresh_token")
            self.access_expires_at = now + token_data.get("expires_in", 518400)
            self.refresh_expires_at = now + token_data.get("refresh_expires_in", 604800)


token_cache = TokenCache()


# ── Conversation store ─────────────────────────────────────────────────────────
# Maps ElevenLabs traceparent trace ID -> CodemIE conversation ID.
# In-memory for demo. Replace with DynamoDB for production.
_conversation_store: dict[str, str] = {}


async def get_or_create_conversation(elevenlabs_id: str) -> str:
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
    Extract stable session ID from ElevenLabs traceparent header.
    The trace ID (second segment) is constant across all requests in a session.
    """
    traceparent = request.headers.get("traceparent", "")
    parts = traceparent.split("-")
    if len(parts) >= 4:
        return parts[1]
    return str(uuid.uuid4())


# ── CodemIE helpers ────────────────────────────────────────────────────────────

async def get_auth_headers() -> dict:
    token = await token_cache.get_token()
    return {
        "Content-Type": "application/json",
        "Accept": "*/*",
        "Authorization": f"Bearer {token}",
        "Origin": "https://codemie.lab.epam.com",
        "Referer": "https://codemie.lab.epam.com/",
        "User-Agent": "Mozilla/5.0 (ElevenLabs-Bridge/1.0)",
    }


async def create_conversation() -> str:
    payload = {
        "initial_assistant_id": CODEMIE_ASSISTANT_ID,
        "folder": CODEMIE_ASSISTANT_FOLDER,
        "is_workflow": False,
    }
    headers = await get_auth_headers()
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
            history.append({"role": "User", "message": content, "createdAt": now})
        elif role == "assistant":
            history.append({
                "role": "Assistant",
                "message": content,
                "createdAt": now,
                "assistantId": CODEMIE_ASSISTANT_ID,
            })

    if history and history[-1]["role"] == "User":
        history = history[:-1]

    system_prompt = next(
        (m.get("content", "") for m in messages if m.get("role") == "system"), ""
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
    url = f"{CODEMIE_ENDPOINT}/{CODEMIE_ASSISTANT_ID}/model"
    payload = build_codemie_request(messages, conversation_id)
    headers = await get_auth_headers()
    full_response = []

    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            async with client.stream("POST", url, json=payload, headers=headers) as response:
                if response.status_code != 200:
                    body = await response.aread()
                    logger.error("[%s] CodemIE returned %d: %s",
                                 elevenlabs_id, response.status_code, body.decode())
                    raise HTTPException(status_code=502,
                                        detail=f"CodemIE returned {response.status_code}")

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
                            generated = obj.get("generated", "")
                            if generated:
                                full_response = [generated]
                        elif thought and thought.get("in_progress") and thought.get("message"):
                            text = thought["message"]
                            full_response.append(text)
                            chunk = {
                                "object": "chat.completion.chunk",
                                "choices": [{"delta": {"content": text}, "index": 0, "finish_reason": None}],
                            }
                            yield f"data: {json.dumps(chunk)}\n\n"

        logger.info("[%s] ASSISTANT: %s", elevenlabs_id, "".join(full_response))

    except HTTPException:
        raise
    except Exception as e:
        logger.error("[%s] Error calling CodemIE: %s", elevenlabs_id, str(e))
        raise HTTPException(status_code=502, detail=str(e))

    yield f"data: {json.dumps({'object': 'chat.completion.chunk', 'choices': [{'delta': {}, 'index': 0, 'finish_reason': 'stop'}]})}\n\n"
    yield "data: [DONE]\n\n"


# ── Startup ────────────────────────────────────────────────────────────────────

@app.on_event("startup")
async def startup():
    """Authenticate with Keycloak on startup so the first request is fast."""
    await token_cache._authenticate()


# ── Routes ─────────────────────────────────────────────────────────────────────

@app.post("/chat/completions")
@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    elevenlabs_id = get_elevenlabs_id(request)

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    messages = body.get("messages", [])
    if not messages:
        raise HTTPException(status_code=400, detail="No messages provided")

    codemie_conversation_id = await get_or_create_conversation(elevenlabs_id)

    user_messages = [m for m in messages if m.get("role") == "user"]
    if user_messages:
        logger.info("[%s] USER: %s", elevenlabs_id, user_messages[-1]["content"])

    return StreamingResponse(
        stream_codemie_response(messages, codemie_conversation_id, elevenlabs_id),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "backend": "CodemIE",
        "model": CODEMIE_LLM_MODEL,
        "assistant_id": CODEMIE_ASSISTANT_ID,
        "active_conversations": len(_conversation_store),
        "token_valid": token_cache.is_access_token_valid(),
        "token_expires_in_seconds": max(0, int(token_cache.access_expires_at - time.time())),
    }


@app.get("/ping")
async def ping():
    """Sends a test message to CodemIE. Use to verify auth and connectivity."""
    messages = [{"role": "user", "content": "Hello! Please respond with a short greeting."}]
    ping_id = "ping-" + str(uuid.uuid4())[:8]
    conversation_id = await create_conversation()
    url = f"{CODEMIE_ENDPOINT}/{CODEMIE_ASSISTANT_ID}/model"
    payload = build_codemie_request(messages, conversation_id)
    headers = await get_auth_headers()

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            async with client.stream("POST", url, json=payload, headers=headers) as response:
                if response.status_code != 200:
                    body = await response.aread()
                    return {"status": "error", "http_status": response.status_code, "detail": body.decode()}

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
                            logger.info("[%s] PING response: %s", ping_id, generated)
                            return {"status": "ok", "response": generated}

        return {"status": "error", "detail": "No response received from CodemIE"}

    except Exception as e:
        logger.error("[%s] Ping failed: %s", ping_id, str(e))
        return {"status": "error", "detail": str(e)}
