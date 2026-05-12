import asyncio
import json
import os
import sys
import uuid
from concurrent import futures
from contextlib import asynccontextmanager
from typing import Optional

import grpc
import httpx
from fastapi import FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

# voice_pb2_grpc uses a flat `import voice_pb2`; add src/ to sys.path so it resolves
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import voice_pb2_grpc

from .db_manager import database
from .voice_session import _Session

FRAUD_DETECTION_API_URL = os.getenv("FRAUD_DETECTION_API_URL", "")

# Global registry: user_uuid → active _Session
active_sessions: dict[str, _Session] = {}
sessions_lock = asyncio.Lock()

_grpc_server: grpc.aio.Server | None = None


# ─── Auth ─────────────────────────────────────────────────────────────────────

async def validate_token(token: str) -> Optional[str]:
    """Returns user_uuid string if the token is valid, else None."""
    if not database.pool:
        return None
    async with database.pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT user_uuid FROM tokens
            WHERE token = $1 AND revoked_at IS NULL
              AND (expires_at IS NULL OR expires_at > NOW())
            """,
            token,
        )
        return str(row["user_uuid"]) if row else None


# ─── gRPC servicer ────────────────────────────────────────────────────────────

class VoiceServiceServicer(voice_pb2_grpc.VoiceServiceServicer):
    async def Session(self, request_iterator, context):
        metadata = dict(context.invocation_metadata())
        token = metadata.get("authorization", "")
        user_uuid = await validate_token(token)
        if not user_uuid:
            await context.abort(grpc.StatusCode.UNAUTHENTICATED, "Invalid or expired token")
            return

        print(f"[gRPC] Session authenticated: user={user_uuid}", flush=True)
        session = _Session(user_uuid, database.pool, database.redis_client)

        async with sessions_lock:
            active_sessions[user_uuid] = session
        try:
            async for response in session.run(request_iterator, context):
                yield response
        finally:
            async with sessions_lock:
                active_sessions.pop(user_uuid, None)
            print(f"[gRPC] Session closed: user={user_uuid}", flush=True)


# ─── FastAPI lifespan ─────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    global _grpc_server
    await database.connect_to_db()
    if database.redis_client:
        await database.redis_client.ping()

    _grpc_server = grpc.aio.server(futures.ThreadPoolExecutor(max_workers=10))
    voice_pb2_grpc.add_VoiceServiceServicer_to_server(VoiceServiceServicer(), _grpc_server)
    _grpc_server.add_insecure_port("[::]:60015")
    await _grpc_server.start()
    print("[gRPC] Server listening on [::]:60015", flush=True)

    yield

    await _grpc_server.stop(grace=5)
    await database.close_db_connection()
    await database.close_redis_connection()


# ─── FastAPI app ──────────────────────────────────────────────────────────────

app = FastAPI(
    title="Anti-Fraud Communication Service",
    description="GPT-powered service that answers calls on behalf of users via edge devices",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ─── Pydantic models ──────────────────────────────────────────────────────────

class IncomingCallRequest(BaseModel):
    phone_number: str


class CallEventRequest(BaseModel):
    callee_user_id: str


class NotificationPayload(BaseModel):
    title: str | None = None
    body: str | None = None
    data: dict | None = None
    silent: bool = False
    android_priority: str | None = None


# ─── Helpers ──────────────────────────────────────────────────────────────────


async def send_push(
    target_user_id: str,
    payload: NotificationPayload,
    app: str | None = None,
):
    event = {
        "target_user_id": target_user_id,
        "payload": json.dumps(payload.model_dump()),
        "app": app,
    }
    if database.redis_client:
        await database.redis_client.xadd("push_notification_stream", event)

async def format_conversation_for_detection(conversation_id: str) -> str:
    try:
        if database.pool:
            async with database.pool.acquire() as conn:
                rows = await conn.fetch(
                    """
                    SELECT role, content FROM messages
                    WHERE conversation_id = $1 AND role IN ('user', 'assistant')
                    ORDER BY created_at ASC
                    """,
                    uuid.UUID(conversation_id),
                )
                parts = []
                for row in rows:
                    label = "caller" if row["role"] == "user" else "receiver"
                    parts.append(f"{label}: {row['content'].strip()}")
                return " ".join(parts)
    except Exception as e:
        print(f"[ERROR] format_conversation: {e}", flush=True)
    return ""


async def call_fraud_detection_api(conversation_text: str) -> Optional[bool]:
    if not FRAUD_DETECTION_API_URL or not conversation_text:
        return None
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                FRAUD_DETECTION_API_URL,
                json={"text": conversation_text},
                headers={"Content-Type": "application/json"},
            )
            if resp.status_code == 200:
                text = resp.text.strip().lower()
                if text == "true":
                    return True
                if text == "false":
                    return False
    except Exception as e:
        print(f"[ERROR] fraud_detection_api: {e}", flush=True)
    return None


# ─── Endpoints ────────────────────────────────────────────────────────────────

@app.post("/api/fraud/incoming-call")
async def incoming_call(
    body: IncomingCallRequest,
    x_user_id: str | None = Header(None, alias="X-User-Id"),
    x_email: str | None = Header(None, alias="X-Email"),
    x_installation_id: str | None = Header("", alias="X-Installation-Id"),
):
    print(f"[HTTP] incoming_call: caller={body.phone_number}, callee={x_user_id}", flush=True)
    if not x_user_id:
        raise HTTPException(status_code=400, detail="Missing X-User-Id header")

    conversation_id = await database.create_conversation(x_user_id)
    caller_phone_number = body.phone_number
    
    is_fraud_detection_enabled = await database.is_fraud_detection_enabled(x_user_id)

    if is_fraud_detection_enabled:
        async with sessions_lock:
            session = active_sessions.get(x_user_id)
        if session:
            await session.on_incoming_call(conversation_id, body.phone_number)
        else:
            print(f"[HTTP] No active edge session for user {x_user_id}", flush=True)

        await send_push(
            target_user_id=x_user_id,
            payload=NotificationPayload(
                title="Incoming Call",
                body=f"Call from {caller_phone_number}",
                data={
                    "type": "incoming_call",
                    "detail": {
                        "phone_number": caller_phone_number,
                    }
                },
                silent=False,
            ),
            app="kebbi"
        )

        return {"status": "ok", "fraud_detection": "enabled"}
    
    else:
        call_token = await database.set_call_token(caller_phone_number, x_user_id)
        async with sessions_lock:
            session = active_sessions.get(x_user_id)
        if session:
            await session.on_direct_call(conversation_id, body.phone_number)
        else:
            print(f"[HTTP] No active edge session for user {x_user_id}", flush=True)

        await send_push(
            target_user_id=x_user_id,
            payload=NotificationPayload(
                title="Incoming Call",
                body=f"Call from {caller_phone_number}",
                data={
                    "type": "incoming_call",
                    "detail": {
                        "phone_number": caller_phone_number,
                    }
                },
                silent=True,
                android_priority="high",
            ),
            app="kebbi"
        )
        return {"status": "ok", "fraud_detection": "disabled", "call_token": call_token}


@app.post("/api/fraud/direct-call")
async def direct_call(
    body: CallEventRequest,
    x_user_id: str | None = Header(None, alias="X-User-Id"),
):
    """Callee manually answered the call; edge should stop handling it."""
    async with sessions_lock:
        session = active_sessions.get(body.callee_user_id)
    if session:
        await session.on_call_end("direct_call")
    return {"status": "ok"}


@app.post("/api/fraud/call-end")
async def call_end(
    body: CallEventRequest,
    x_user_id: str | None = Header(None, alias="X-User-Id"),
):
    """Call has ended; edge should stop."""
    async with sessions_lock:
        session = active_sessions.get(body.callee_user_id)
    if session:
        await session.on_call_end("call_end")
    send_push(
        target_user_id=x_user_id,
        payload=NotificationPayload(
            data={
                "type": "call_event",
                "action": "hangup"
            },
            silent=True,
            android_priority="high",
        ),
        app="host_mobile"
    )
    return {"status": "ok"}


@app.get("/health")
async def health_check():
    redis_ok = False
    if database.redis_client:
        try:
            await database.redis_client.ping()
            redis_ok = True
        except Exception:
            pass
    return {
        "status": "healthy",
        "service": "anti-fraud",
        "components": {
            "redis": "healthy" if redis_ok else "disconnected",
            "grpc": "listening on :60015",
            "active_sessions": len(active_sessions),
        },
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
