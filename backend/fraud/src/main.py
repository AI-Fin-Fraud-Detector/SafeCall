import asyncio
import json
import math
import os
import urllib.parse
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, Optional

import httpx
from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from pydantic import BaseModel

# Import the database module itself to access its global variables correctly.
from .db_manager import database, get_user_latest_conversation


class IncomingCallRequest(BaseModel):
    phone_number: str

class FraudMessage(BaseModel):
    prompt: str
    phone_number: str  # 受話者的手機號碼
    initiate_conversation: Optional[bool] = False


class NotificationPayload(BaseModel):
    title: str | None = None
    body: str | None = None
    icon: str | None = None
    tag: str | None = None
    data: dict | None = None
    silent: bool = False
    android_priority: str | None = None


async def get_user_by_phone(phone_number: str) -> Optional[dict]:
    """
    透過手機號碼查詢用戶資訊

    Args:
        phone_number: 手機號碼

    Returns:
        dict: 用戶資訊 (包含 name, email 等)，如果找不到則返回 None
    """
    try:
        if database.pool:
            async with database.pool.acquire() as conn:
                query = """
                    SELECT uuid, name, email, phone_number
                    FROM users
                    WHERE phone_number = $1
                """
                result = await conn.fetchrow(query, phone_number)
                if result:
                    return {
                        "uuid": str(result["uuid"]),
                        "name": result["name"],
                        "email": result["email"],
                        "phone_number": result["phone_number"],
                    }
        return None
    except Exception as e:
        print(f"[ERROR] Failed to query user by phone {phone_number}: {e}")
        return None


async def format_conversation_for_detection(conversation_id: str) -> str:
    """
    格式化對話歷史為詐騙檢測API所需的格式
    格式: caller:...receiver:...caller:...receiver:...
    注意：只包含user和assistant的對話內容，不包含system prompt

    Args:
        conversation_id: 對話ID

    Returns:
        str: 格式化後的對話文本
    """
    try:
        if database.pool:
            async with database.pool.acquire() as conn:
                query = """
                    SELECT role, content, created_at 
                    FROM fraud_messages 
                    WHERE conversation_id = $1 
                    AND role IN ('user', 'assistant')
                    ORDER BY created_at ASC
                """
                messages = await conn.fetch(query, conversation_id)

                formatted_parts = []
                for msg in messages:
                    role = msg["role"]
                    content = msg["content"].strip()

                    # user對應caller, assistant對應receiver
                    if role == "user":
                        formatted_parts.append(f"caller: {content}")
                    elif role == "assistant":
                        formatted_parts.append(f"receiver: {content}")

                formatted_text = " ".join(formatted_parts)
                print(
                    f"[DEBUG] Formatted conversation for detection ({len(messages)} messages): {formatted_text[:200]}..."
                )
                return formatted_text
        return ""
    except Exception as e:
        print(f"[ERROR] Failed to format conversation {conversation_id}: {e}")
        return ""


async def call_fraud_detection_api(conversation_text: str) -> bool | None:
    """
    調用詐騙檢測API

    Args:
        conversation_text: 格式化後的對話文本

    Returns:
        bool: True表示可能是詐騙，False表示正常對話，None表示API調用失敗
    """
    try:
        print(
            f"[DEBUG] Calling fraud detection API with text length: {len(conversation_text)}"
        )
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                FRAUD_DETECTION_API_URL,
                json={"text": conversation_text},
                headers={"Content-Type": "application/json"},
            )

            print(f"[DEBUG] Fraud detection API status: {response.status_code}")
            print(
                f"[DEBUG] Response content type: {response.headers.get('content-type')}"
            )
            print(f"[DEBUG] Raw response: {response.text}")

            if response.status_code == 200:
                # API返回純文本 "True" 或 "False"
                response_text = response.text.strip()

                if response_text.lower() == "true":
                    prediction = True
                    print("[INFO] Fraud detection result: True (possible scam)")
                elif response_text.lower() == "false":
                    prediction = False
                    print("[INFO] Fraud detection result: False (normal conversation)")
                else:
                    # 如果返回其他內容，嘗試解析JSON
                    try:
                        result = response.json()
                        print(f"[DEBUG] Parsed JSON response: {result}")
                        prediction = result.get(
                            "prediction", result.get("result", None)
                        )
                        print(f"[INFO] Fraud detection result: {prediction}")
                    except json.JSONDecodeError:
                        print(f"[ERROR] Unexpected response format: {response_text}")
                        return None

                return prediction
            else:
                print(
                    f"[WARNING] Fraud detection API returned status {response.status_code}"
                )
                print(f"[WARNING] Response body: {response.text}")
                return None
    except httpx.TimeoutException as e:
        print(f"[ERROR] Fraud detection API timeout: {e}")
        return None
    except httpx.RequestError as e:
        print(f"[ERROR] Fraud detection API request error: {e}")
        return None
    except Exception as e:
        print(f"[ERROR] Failed to call fraud detection API: {e}")
        return None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global llm, tts_service
    await database.connect_to_db()
    # Check if the redis_client was successfully initialized before using it.
    if database.redis_client:
        await database.redis_client.ping()  # Establishes connection and checks health
    else:
        raise RuntimeError(
            "Redis client could not be initialized. Application cannot start."
        )

    yield
    await database.close_db_connection()
    await database.close_redis_connection()


async def notify_callee_event(
    caller_user_id: str,
    caller_name: str,
    callee_user_id: str,
    conversation_id: str,
    call_token: str,
):
    """Publishes events to Redis for other services to consume."""
    # Event for the socket gateway to broadcast the text message
    notification_payload = NotificationPayload(
        title="來電通知",
        body=f"偵測到來自 {caller_name} 的來電",
        data={
            "type": "incoming_call",
            "call_token": call_token,
            "caller_name": caller_name,
            "caller_user_id": caller_user_id,
        },
        silent=False, # TODO in the future change to callstylenotification
        android_priority="high",
    )
    gateway_event = {
        "type": "call_notify",
        "service": "anti-fraud",
        "caller_user_id": caller_user_id,
        "callee_user_id": callee_user_id,
        "conversation_id": conversation_id,
        "payload": json.dumps(notification_payload.model_dump()),
        "app": "kebbi",
    }
    # Ensure the client is available before publishing
    if database.redis_client:
        # XADD adds events to a stream. The '*' generates a unique ID.
        await database.redis_client.xadd("push_notification_stream", gateway_event)
        print(
            f"[Anti-Fraud Service] Added call_notify to push_notification_stream for caller {caller_user_id} > callee {callee_user_id}"
        )
    else:
        print(
            "[Anti-Fraud Service] ERROR: Redis client not available. Cannot publish events."
        )


app = FastAPI(
    title="Anti-Fraud Communication Service",
    description="A GPT-powered service designed to engage with scammers and extract information about their schemes",
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

@app.post("/api/fraud/incoming-call")
async def incoming_call(
    body: IncomingCallRequest,
    x_user_id: str | None = Header(None, alias="X-User-Id"),
    x_email: str | None = Header(None, alias="X-Email"),
    x_installation_id: str | None = Header("", alias="X-Installation-Id"),
):
    print(
        f"Received incoming call: {body.phone_number} ({x_email} - {x_user_id})"
    )
    return


@app.get("/health")
async def health_check():
    """
    Health Check Endpoint

    Returns the health status of the anti-fraud service and its components.
    """
    try:
        # 檢查Redis連接
        if database.redis_client:
            await database.redis_client.ping()
            redis_status = "healthy"
        else:
            redis_status = "disconnected"

        # 檢查LLM狀態
        llm_status = "healthy" if llm else "not_initialized"

        return {
            "status": "healthy",
            "service": "anti-fraud",
            "components": {
                "redis": redis_status,
                # "llm": llm_status,
                # "tts": "healthy" if tts_service else "not_initialized",
            },
        }
    except Exception as e:
        return {"status": "unhealthy", "error": str(e)}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
