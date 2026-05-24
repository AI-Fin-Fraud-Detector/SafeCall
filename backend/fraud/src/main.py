import asyncio
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

from .db_manager import database
from .notifications import send_push, NotificationPayload
from .voice_session import _Session

from .protos import voice_pb2_grpc

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
            await context.abort(
                grpc.StatusCode.UNAUTHENTICATED, "Invalid or expired token"
            )
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
    voice_pb2_grpc.add_VoiceServiceServicer_to_server(
        VoiceServiceServicer(), _grpc_server
    )
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
    caller_name: str | None = None


class CallEventRequest(BaseModel):
    callee_user_id: str


# ─── Helpers ──────────────────────────────────────────────────────────────────


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
    print(
        f"[HTTP] incoming_call: caller={body.phone_number} ({body.caller_name}), user={x_user_id}",
        flush=True,
    )
    if not x_user_id:
        raise HTTPException(status_code=400, detail="Missing X-User-Id header")

    caller_phone_number = body.phone_number
    conversation_id = await database.create_conversation(x_user_id, caller_phone_number, body.caller_name)

    is_fraud_detection_enabled = await database.is_fraud_detection_enabled(x_user_id)

    if is_fraud_detection_enabled:
        async with sessions_lock:
            session = active_sessions.get(x_user_id)
        if session:
            await session.on_incoming_call(conversation_id, body.phone_number, body.caller_name)
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
                        "caller_name": body.caller_name,
                        "conversation_id": conversation_id,
                    },
                },
                silent=False,
            ),
            app="kebbi",
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
                    },
                },
                silent=True,
                android_priority="high",
            ),
            app="kebbi",
        )
        return {"status": "ok", "fraud_detection": "disabled", "call_token": call_token}


@app.post("/api/fraud/call-end")
async def call_end(
    x_user_id: str | None = Header(None, alias="X-User-Id"),
):
    """Call has ended; edge should stop."""
    async with sessions_lock:
        session = active_sessions.get(x_user_id)
    if session:
        await session.on_call_end("call_end")
    await send_push(
        target_user_id=x_user_id,
        payload=NotificationPayload(
            data={"type": "call_event", "action": "hangup"},
            silent=True,
            android_priority="high",
        ),
        app="host_mobile",
    )
    return {"status": "ok"}


@app.post("/api/fraud/answer-call")
async def answer_call(
    x_user_id: str | None = Header(None, alias="X-User-Id"),
):
    """User answered the call."""
    if not x_user_id:
        raise HTTPException(status_code=400, detail="Missing X-User-Id header")
    async with sessions_lock:
        session = active_sessions.get(x_user_id)
    if session:
        await session.on_user_answer_call()
    return {"status": "ok"}


@app.get("/api/fraud/conversations")
async def get_conversations(
    x_user_id: str | None = Header(None, alias="X-User-Id"),
    before: str | None = None,
    limit: int = 50,
):
    if not x_user_id:
        raise HTTPException(status_code=400, detail="Missing X-User-Id header")
    if limit < 1 or limit > 200:
        raise HTTPException(status_code=400, detail="limit must be between 1 and 200")

    if not database.pool:
        raise HTTPException(status_code=503, detail="Database unavailable")

    async with database.pool.acquire() as conn:
        if before:
            try:
                before_uuid = uuid.UUID(before)
            except ValueError:
                raise HTTPException(status_code=400, detail="Invalid before cursor")
            rows = await conn.fetch(
                """
                SELECT id, title, metadata, created_at, updated_at
                FROM conversations
                WHERE user_uuid = $1 AND type = 'phone'
                  AND created_at < (SELECT created_at FROM conversations WHERE id = $2)
                ORDER BY created_at DESC
                LIMIT $3
                """,
                uuid.UUID(x_user_id),
                before_uuid,
                limit,
            )
        else:
            rows = await conn.fetch(
                """
                SELECT id, title, metadata, created_at, updated_at
                FROM conversations
                WHERE user_uuid = $1 AND type = 'phone'
                ORDER BY created_at DESC
                LIMIT $2
                """,
                uuid.UUID(x_user_id),
                limit,
            )

    conversations = [
        {
            "conversation_id": str(r["id"]),
            "title": r["title"],
            "metadata": r["metadata"],
            "created_at": r["created_at"].isoformat(),
            "updated_at": r["updated_at"].isoformat(),
        }
        for r in rows
    ]
    return {
        "conversations": conversations,
        "next_cursor": conversations[-1]["conversation_id"]
        if len(conversations) == limit
        else None,
    }


@app.get("/api/fraud/conversations/{conversation_id}")
async def get_conversation_info(
    conversation_id: str,
    x_user_id: str | None = Header(None, alias="X-User-Id"),
):
    if not x_user_id:
        raise HTTPException(status_code=400, detail="Missing X-User-Id header")
    try:
        conv_uuid = uuid.UUID(conversation_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid conversation_id")

    if not database.pool:
        raise HTTPException(status_code=503, detail="Database unavailable")

    async with database.pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM conversations WHERE id = $1 AND user_uuid = $2 AND type = 'phone'",
            conv_uuid,
            uuid.UUID(x_user_id),
        )
        if not row:
            raise HTTPException(status_code=404, detail="Conversation not found")
        return {
            "conversation_id": str(row["id"]),
            "title": row["title"],
            "metadata": row["metadata"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }


@app.get("/api/fraud/conversations/{conversation_id}/messages")
async def get_messages(
    conversation_id: str,
    x_user_id: str | None = Header(None, alias="X-User-Id"),
    before: str | None = None,
    limit: int = 50,
):
    if not x_user_id:
        raise HTTPException(status_code=400, detail="Missing X-User-Id header")
    if limit < 1 or limit > 200:
        raise HTTPException(status_code=400, detail="limit must be between 1 and 200")

    try:
        conv_uuid = uuid.UUID(conversation_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid conversation_id")

    if not database.pool:
        raise HTTPException(status_code=503, detail="Database unavailable")

    async with database.pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id FROM conversations WHERE id = $1 AND user_uuid = $2 AND type = 'phone'",
            conv_uuid,
            uuid.UUID(x_user_id),
        )
        if not row:
            raise HTTPException(status_code=404, detail="Conversation not found")

        if before:
            try:
                before_uuid = uuid.UUID(before)
            except ValueError:
                raise HTTPException(status_code=400, detail="Invalid before cursor")
            rows = await conn.fetch(
                """
                SELECT id, role, content, metadata, created_at, edited_at
                FROM messages
                WHERE conversation_id = $1
                  AND role != 'system'
                  AND created_at < (SELECT created_at FROM messages WHERE id = $2)
                ORDER BY created_at DESC
                LIMIT $3
                """,
                conv_uuid,
                before_uuid,
                limit,
            )
        else:
            rows = await conn.fetch(
                """
                SELECT id, role, content, metadata, created_at, edited_at
                FROM messages
                WHERE conversation_id = $1
                  AND role != 'system'
                ORDER BY created_at DESC
                LIMIT $2
                """,
                conv_uuid,
                limit,
            )

    messages = [
        {
            "id": str(r["id"]),
            "role": r["role"],
            "content": r["content"],
            "metadata": r["metadata"],
            "created_at": r["created_at"].isoformat(),
            "edited_at": r["edited_at"].isoformat() if r["edited_at"] else None,
        }
        for r in reversed(rows)
    ]
    return {
        "messages": messages,
        "next_cursor": messages[0]["id"] if len(messages) == limit else None,
    }


@app.get("/api/fraud/conversations/{conversation_id}/messages/{message_id}")
async def get_single_message(
    conversation_id: str,
    message_id: str,
    x_user_id: str | None = Header(None, alias="X-User-Id"),
):
    if not x_user_id:
        raise HTTPException(status_code=400, detail="Missing X-User-Id header")

    try:
        conv_uuid = uuid.UUID(conversation_id)
        msg_uuid = uuid.UUID(message_id)
    except ValueError:
        raise HTTPException(
            status_code=400, detail="Invalid conversation_id or message_id"
        )

    if not database.pool:
        raise HTTPException(status_code=503, detail="Database unavailable")

    async with database.pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id, role, content, metadata, created_at, edited_at FROM messages WHERE id = $1 AND conversation_id = $2",
            msg_uuid,
            conv_uuid,
        )
        if not row:
            raise HTTPException(status_code=404, detail="Message not found")

    return {
        "id": str(row["id"]),
        "role": row["role"],
        "content": row["content"],
        "metadata": row["metadata"],
        "created_at": row["created_at"].isoformat(),
        "edited_at": row["edited_at"].isoformat() if row["edited_at"] else None,
    }


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
