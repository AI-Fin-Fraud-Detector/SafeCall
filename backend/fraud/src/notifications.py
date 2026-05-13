from pydantic import BaseModel
import json
import uuid
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
