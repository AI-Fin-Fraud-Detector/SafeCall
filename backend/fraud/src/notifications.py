import asyncio
import json
import os
import uuid

import httpx
from pydantic import BaseModel
from .db_manager import database

class NotificationPayload(BaseModel):
    title: str | None = None
    body: str | None = None
    data: dict | None = None
    silent: bool = False
    android_priority: str | None = None


async def send_push(
    target_user_id: uuid.UUID,
    payload: NotificationPayload,
    app: str | None = None,
):
    event = {
        "target_user_id": str(target_user_id),
        "payload": json.dumps(payload.model_dump(mode='json')),
    }
    if app is not None:
        event["app"] = app
    if database.redis_client:
        await database.redis_client.xadd("push_notification_stream", event)


NOTIFY_SERVICE_URL = os.getenv("NOTIFY_SERVICE_URL", "http://notify:8000")


async def notify_user(user_id: str, event: str, data: dict | None = None) -> bool:
    """Ask the internal notify service to deliver `event` on the user's bound
    channels (LINE, ...). Never raises; returns whether the call succeeded."""
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                f"{NOTIFY_SERVICE_URL}/internal/notify",
                json={"user_id": user_id, "event": event, "data": data or {}},
            )
        response.raise_for_status()
        return True
    except Exception as e:
        print(f"[NOTIFY] {event} for {user_id} failed: {e}", flush=True)
        return False


def notify_user_in_background(user_id: str, event: str, data: dict | None = None) -> None:
    asyncio.create_task(notify_user(user_id, event, data))
