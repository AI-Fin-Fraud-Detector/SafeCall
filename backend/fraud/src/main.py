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
from .const import CALLER_TYPE_CONTACT, CALLER_TYPE_NON_CONTACT, CALLER_TYPE_PRIVATE

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


# ─── Health Check ─────────────────────────────────────────────────────────────

@app.get("/health")
async def health_check():
    return {"status": "healthy"}


# ─── Pydantic models ──────────────────────────────────────────────────────────


class IncomingCallRequest(BaseModel):
    phone_number: str
    caller_name: str | None = None


class CallEventRequest(BaseModel):
    callee_user_id: str


class WebrtcAnswerRequest(BaseModel):
    sdp: str


class ConnectCallRequest(BaseModel):
    call_token: str


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
    """
    Determine whether conversation text is classified as fraudulent by the configured detection service.
    
    Parameters:
        conversation_text (str): Conversation text submitted for classification.
    
    Returns:
        Optional[bool]: `True` for a fraudulent classification, `False` for a non-fraudulent classification, or `None` when detection is unavailable or the response is unrecognized.
    """
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


def detect_caller_type(caller_phone_number: str, is_known_contact: bool) -> str:
    """
    Classify a caller based on phone number availability and contact status.
    
    Parameters:
    	caller_phone_number (str): The caller's phone number.
    	is_known_contact (bool): Whether the caller is saved as a known contact.
    
    Returns:
    	str: The private, contact, or non-contact caller type.
    """
    if not caller_phone_number:
        return CALLER_TYPE_PRIVATE
    if is_known_contact:
        return CALLER_TYPE_CONTACT
    return CALLER_TYPE_NON_CONTACT


# ─── Endpoints ────────────────────────────────────────────────────────────────


@app.post("/api/fraud/incoming-call")
async def incoming_call(
    body: IncomingCallRequest,
    x_user_id: str | None = Header(None, alias="X-User-Id"),
    x_email: str | None = Header(None, alias="X-Email"),
    x_installation_id: str | None = Header("", alias="X-Installation-Id"),
):
    """
    Handle an incoming call and route it through fraud detection or direct-call handling.
    
    Parameters:
        body (IncomingCallRequest): Incoming caller phone number and optional caller name.
        x_user_id (str | None): Authenticated user identifier from the `X-User-Id` header.
    
    Returns:
        dict: Call status, conversation identifier, caller type, and either fraud-detection
            information or a direct-call token.
    
    Raises:
        HTTPException: If the `X-User-Id` header is missing.
    """
    print(
        f"[HTTP] incoming_call: caller={body.phone_number} ({body.caller_name}), user={x_user_id}",
        flush=True,
    )
    if not x_user_id:
        raise HTTPException(status_code=400, detail="Missing X-User-Id header")

    caller_phone_number = body.phone_number.strip()
    contact = await database.get_contact_by_phone(x_user_id, caller_phone_number)
    is_known_contact = contact is not None
    resolved_caller_name = body.caller_name or (contact["name"] if contact else None)
    caller_type = detect_caller_type(caller_phone_number, is_known_contact)

    conversation_id = await database.create_conversation(
        x_user_id,
        caller_phone_number,
        resolved_caller_name,
        caller_type,
    )

    is_fraud_detection_enabled = await database.is_fraud_detection_enabled(x_user_id)
    should_use_fraud_detection = is_fraud_detection_enabled and not is_known_contact

    if should_use_fraud_detection:
        async with sessions_lock:
            session = active_sessions.get(x_user_id)
        if session:
            await session.on_incoming_call(
                conversation_id,
                caller_phone_number,
                resolved_caller_name,
            )
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
                        "caller_name": resolved_caller_name,
                        "conversation_id": conversation_id,
                        "caller_type": caller_type,
                    },
                },
                silent=False,
            ),
            app="kebbi",
        )

        return {
            "status": "ok",
            "fraud_detection": "enabled",
            "conversation_id": conversation_id,
            "caller_type": caller_type,
        }

    else:
        call_token = await database.set_call_token(
            caller_phone_number,
            x_user_id,
            conversation_id,
            resolved_caller_name,
            caller_type,
        )
        async with sessions_lock:
            session = active_sessions.get(x_user_id)
        if session:
            await session.on_direct_call(
                conversation_id,
                caller_phone_number,
                resolved_caller_name,
            )
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
                        "caller_name": resolved_caller_name,
                        "conversation_id": conversation_id,
                        "caller_type": caller_type,
                        "call_token": call_token,
                    },
                },
                silent=True,
                android_priority="high",
            ),
            app="kebbi",
        )
        return {
            "status": "ok",
            "fraud_detection": "disabled",
            "call_token": call_token,
            "conversation_id": conversation_id,
            "caller_type": caller_type,
            "is_known_contact": is_known_contact,
        }


@app.post("/api/fraud/call/connect")
async def connect_direct_call(
    body: ConnectCallRequest,
    x_user_id: str | None = Header(None, alias="X-User-Id"),
):
    """
    Connects an authenticated direct call to the user's active edge session.
    
    Raises:
        HTTPException: If the user header is missing, the call token is invalid,
            belongs to another user, contains incomplete data, or no active edge
            session is available.
    
    Returns:
        dict: Acknowledgment containing the conversation and caller details, with
            ``edge_session_ready`` set to ``True``.
    """
    if not x_user_id:
        raise HTTPException(status_code=400, detail="Missing X-User-Id header")

    token_data = await database.get_call_token(body.call_token)
    if token_data is None:
        raise HTTPException(status_code=404, detail="Invalid or expired call token")

    if token_data.get("callee") != x_user_id:
        raise HTTPException(status_code=403, detail="Call token does not belong to this user")

    conversation_id = token_data.get("conversation_id")
    caller_phone = token_data.get("caller", "")
    caller_name = token_data.get("caller_name")

    if not isinstance(conversation_id, str) or not conversation_id:
        raise HTTPException(status_code=400, detail="Call token data is incomplete")

    async with sessions_lock:
        session = active_sessions.get(x_user_id)
    if not session:
        raise HTTPException(
            status_code=409,
            detail="No active edge session. Keep call token and retry.",
        )

    consumed_data = await database.consume_call_token(body.call_token)
    if consumed_data is None:
        raise HTTPException(status_code=404, detail="Invalid or expired call token")

    await session.on_direct_call(conversation_id, caller_phone, caller_name)

    return {
        "status": "ok",
        "conversation_id": conversation_id,
        "caller_phone": caller_phone,
        "caller_name": caller_name,
        "caller_type": token_data.get("caller_type"),
        "edge_session_ready": True,
    }


@app.get("/api/fraud/active-call")
async def get_active_call(
    x_user_id: str | None = Header(None, alias="X-User-Id"),
):
    """Get current active call info if any, for notification handling."""
    if not x_user_id:
        raise HTTPException(status_code=400, detail="Missing X-User-Id header")

    async with sessions_lock:
        session = active_sessions.get(x_user_id)

    if not session or not session.call_active.is_set():
        return {
            "has_active_call": False,
        }

    # Return active call info
    import time as time_module
    duration_seconds = int(time_module.monotonic() - session.call_started_at) if session.call_started_at else 0
    return {
        "has_active_call": True,
        "conversation_id": session.conversation_id,
        "phone_number": session.caller_phone,
        "caller_name": session.caller_name,
        "call_start_time": session.call_start_datetime,
        "duration_seconds": duration_seconds,
        "current_score": int(session.scam_probability * 100),
    }


@app.post("/api/fraud/call-end")
async def call_end(
    x_user_id: str | None = Header(None, alias="X-User-Id"),
):
    """Ends the active call for the authenticated user and notifies connected applications.
    
    Parameters:
    	x_user_id (str | None): User identifier from the `X-User-Id` header.
    
    Returns:
    	dict: A status response indicating that the call ended.
    """
    if not x_user_id:
        raise HTTPException(status_code=400, detail="Missing X-User-Id header")
    async with sessions_lock:
        session = active_sessions.get(x_user_id)
    if session:
        await session.on_call_end("call_end")
    # Send hangup event to both host_mobile and kebbi apps
    for app in ["host_mobile", "kebbi"]:
        await send_push(
            target_user_id=x_user_id,
            payload=NotificationPayload(
                data={"type": "call_event", "action": "hangup"},
                silent=True,
                android_priority="high",
            ),
            app=app,
        )
    return {"status": "ok"}


@app.post("/api/fraud/answer-call")
async def answer_call(
    x_user_id: str | None = Header(None, alias="X-User-Id"),
):
    """
    Marks the active call as answered for the authenticated user.
    
    Returns:
        dict[str, str]: A status response with ``"ok"`` when the call is marked as answered.
    """
    if not x_user_id:
        raise HTTPException(status_code=400, detail="Missing X-User-Id header")
    async with sessions_lock:
        session = active_sessions.get(x_user_id)
    if session:
        await session.on_user_answer_call()
    return {"status": "ok"}


@app.get("/api/fraud/webrtc/offer")
async def get_webrtc_offer(
    x_user_id: str | None = Header(None, alias="X-User-Id"),
):
    """
    Retrieve the pending WebRTC SDP offer for the active session.
    
    Returns:
        dict: A status payload with ``"pending"`` when no offer is ready, or
        ``"ready"`` and the SDP string when an offer is available.
    """
    if not x_user_id:
        raise HTTPException(status_code=400, detail="Missing X-User-Id header")
    async with sessions_lock:
        session = active_sessions.get(x_user_id)
    if not session:
        raise HTTPException(status_code=404, detail="No active edge session")
    if not session.webrtc_offer_sdp:
        return {"status": "pending"}
    return {"status": "ready", "sdp": session.webrtc_offer_sdp}


@app.post("/api/fraud/webrtc/answer")
async def post_webrtc_answer(
    body: WebrtcAnswerRequest,
    x_user_id: str | None = Header(None, alias="X-User-Id"),
):
    """
    Receive a WebRTC SDP answer and forward it to the active edge session.
    
    Parameters:
    	body (WebrtcAnswerRequest): The answer payload containing the SDP.
    
    Returns:
    	dict: A response with status set to "ok".
    """
    if not x_user_id:
        raise HTTPException(status_code=400, detail="Missing X-User-Id header")
    # Consume the pending offer atomically so a retried/duplicate answer for the
    # same negotiation is rejected (a second setRemoteDescription is an invalid
    # state transition on edge).
    async with sessions_lock:
        session = active_sessions.get(x_user_id)
        if not session:
            raise HTTPException(status_code=404, detail="No active edge session")
        if not session.webrtc_offer_sdp:
            raise HTTPException(status_code=409, detail="No pending WebRTC offer")
        session.webrtc_offer_sdp = None
    await session.forward_answer_to_edge(body.sdp)
    return {"status": "ok"}


@app.get("/api/fraud/conversations")
async def get_conversations(
    x_user_id: str | None = Header(None, alias="X-User-Id"),
    before: str | None = None,
    limit: int = 50,
):
    """
    List the user's phone conversations in reverse chronological order.
    
    Parameters:
        before (str | None): Cursor conversation ID; returns conversations created before that conversation.
        limit (int): Maximum number of conversations to return.
    
    Returns:
        dict: A mapping containing the conversation list and the next cursor, if more results are available.
    """
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
