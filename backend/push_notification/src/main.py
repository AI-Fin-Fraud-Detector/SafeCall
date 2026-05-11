import asyncio
import json
import os
from contextlib import asynccontextmanager
from copy import deepcopy

import firebase_admin
from fastapi import FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from firebase_admin import messaging
from pydantic import BaseModel
from pywebpush import WebPushException, webpush

# from apns2.client import APNsClient
# from apns2.payload import Payload
from .db_manager import database

# VAPID keys - should be set in environment
VAPID_PRIVATE_KEY = os.getenv("VAPID_PRIVATE_KEY")
VAPID_CLAIMS = {
    "sub": "mailto:" + os.getenv("VAPID_EMAIL", "test@example.com"),
}

# Firebase initialization
firebase_app = None
if os.getenv("FIREBASE_CREDENTIALS_PATH"):
    firebase_app = firebase_admin.initialize_app(
        firebase_admin.credentials.Certificate(os.getenv("FIREBASE_CREDENTIALS_PATH"))
    )
    print("[INFO] Firebase app initialized successfully.")

# APNs initialization
# apns_client = None
# if os.getenv("APNS_AUTH_KEY_PATH") and os.getenv("APNS_KEY_ID") and os.getenv("APNS_TEAM_ID"):
#     apns_client = APNsClient(
#         credentials=os.getenv("APNS_AUTH_KEY_PATH"),
#         key_id=os.getenv("APNS_KEY_ID"),
#         team_id=os.getenv("APNS_TEAM_ID"),
#         use_sandbox=bool(os.getenv("APNS_USE_SANDBOX", "false").lower() == "true")
#     )


class PushSubscriptionKeys(BaseModel):
    p256dh: str
    auth: str


class PushSubscription(BaseModel):
    endpoint: str
    keys: PushSubscriptionKeys | None = None
    expirationTime: int | None = None


class SubscribeRequest(BaseModel):
    # user_id: str | None = None
    pushSubscription: PushSubscription | None = None
    fcm_token: str | None = None
    apns_token: str | None = None
    platform: str  # "web", "fcm", "apns"


class NotificationPayload(BaseModel):
    title: str | None = None
    body: str | None = None
    icon: str | None = None
    tag: str | None = None
    data: dict | None = None
    silent: bool = False
    android_priority: str | None = None
    app: str | None = None  # host_mobile | kebbi — if set, only deliver to that app's tokens


STREAM_KEY = "push_notification_stream"
BLOCK_MS = 5000  # wait up to 5 seconds
READ_COUNT = 10  # max number of events to read at once


async def process_push_stream():
    """
    Continuously read events from Redis stream and send notifications to the correct user.
    """
    last_id = "0"  # start from the beginning. Use "$" to read only new events.
    print("[Stream Consumer] Starting...")

    while True:
        try:
            # Read events from Redis stream
            response = await database.redis_client.xread(
                {STREAM_KEY: last_id}, block=BLOCK_MS, count=READ_COUNT
            )
            if not response:
                continue  # nothing new, loop again

            for stream_name, events in response:
                for event_id, fields in events:
                    last_id = event_id  # update last_id to avoid reprocessing
                    await database.redis_client.xdel(STREAM_KEY, event_id)

                    # Decode Redis fields (they might be bytes)
                    event = dict(fields)

                    payload_json = event.get("payload")
                    target_user_id = event.get("target_user_id")

                    if payload_json and target_user_id:
                        try:
                            # Deserialize payload back to Python dict
                            payload = json.loads(payload_json)
                        except json.JSONDecodeError as e:
                            print(
                                f"[Stream] Failed to parse payload JSON: {payload_json}, error: {e}"
                            )
                            continue

                        try:
                            app = event.get("app")
                            sent, failed = await send_push_to_user(
                                target_user_id, payload, app=app
                            )
                            print(
                                f"[Stream] Notification sent to {target_user_id}: {sent} sent, {failed} failed"
                            )
                        except Exception as e:
                            print(
                                f"[Stream] Failed sending push to {target_user_id}: {e}"
                            )
                    else:
                        print(
                            f"[Stream] Invalid event, missing payload or target_user_id: {event}"
                        )

        except asyncio.CancelledError:
            print("[Stream Consumer] Task cancelled, shutting down...")
            break  # allow graceful shutdown
        except Exception as e:
            print(f"[Stream] Error reading from Redis stream: {e}")
            await asyncio.sleep(1)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await database.connect_to_db()
    # Check if the redis_client was successfully initialized before using it.
    if database.redis_client:
        await database.redis_client.ping()  # Establishes connection and checks health
    else:
        raise RuntimeError(
            "Redis client could not be initialized. Application cannot start."
        )
    stream_task = asyncio.create_task(process_push_stream())
    try:
        yield  # run the app
    finally:
        # Gracefully cancel the consumer when app shuts down
        stream_task.cancel()
        try:
            await stream_task
        except asyncio.CancelledError:
            print("[Stream Consumer] Cancelled on shutdown")

        # Close DB / Redis connections
        await database.close_db_connection()
        await database.close_redis_connection()


app = FastAPI(lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.post("/api/push/subscribe/{app}")
async def subscribe(
    app: str,  # host_mobile | kebbi
    subscription: SubscribeRequest,
    x_user_id: str | None = Header(None, alias="X-User-Id"),
    x_email: str | None = Header(None, alias="X-Email"),
    x_installation_id: str | None = Header(None, alias="X-Installation-Id"),
):
    """Save or update a push subscription in the database."""
    print("Subscription request: ", subscription)
    try:
        if not x_user_id:
            raise HTTPException(status_code=400, detail="X-User-Id header is required.")
        user_id = x_user_id
        platform = subscription.platform
        if subscription.pushSubscription:
            push_sub = subscription.pushSubscription.model_dump()
            await database.save_subscription(user_id, push_sub, platform="web", app=app)
        elif subscription.fcm_token:
            push_sub = {"endpoint": subscription.fcm_token}
            await database.save_subscription(user_id, push_sub, platform="fcm", app=app)
        elif subscription.apns_token:
            push_sub = {"endpoint": subscription.apns_token}
            await database.save_subscription(user_id, push_sub, platform="apns", app=app)
        else:
            raise HTTPException(
                status_code=400,
                detail="Either pushSubscription, fcm_token, or apns_token must be provided",
            )

        return {"message": "Subscription saved!", "user_id": user_id}

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/push/vapid_public_key")
async def get_vapid_public_key():
    """Return VAPID public key for Web Push subscription."""
    public_key = os.getenv("VAPID_PUBLIC_KEY")
    if not public_key:
        raise HTTPException(status_code=500, detail="VAPID public key not configured")
    return {"public_key": public_key}


@app.post("/notify")
async def notify_all(payload: NotificationPayload):
    """Send a push notification to all users. Payload example: { "title": "System Alert", "body": "New update available.", "icon": "/static/icons/alert.png", "tag": "web-push", "data": { "url": "/updates" } }"""
    print(payload)
    payload_dict = payload.model_dump()  # convert to dict
    results = await database.get_all_subscriptions(app=payload.app)

    if not results:
        print("WARNING: No subscriptions found. Notification not sent.")
        return {"sent": 0, "failed": 0}

    success, failed = 0, 0
    for row in results:
        if await send_push(row, payload_dict):
            success += 1
        else:
            failed += 1

    return {"sent": success, "failed": failed}


@app.post("/notify/user/{user_id}")
async def notify_user(user_id: str, payload: NotificationPayload):
    """Send a push notification to a specific user."""
    print(f"Notifying user {user_id}: {payload}")
    payload_dict = payload.model_dump()
    results = await database.get_subscriptions_by_user(user_id, app=payload.app)

    if not results:
        print(
            f"WARNING: No subscriptions found for user {user_id}. Notification not sent."
        )
        return {"sent": 0, "failed": 0}

    success, failed = 0, 0
    for row in results:
        if await send_push(row, payload_dict):
            success += 1
        else:
            failed += 1

    return {"sent": success, "failed": failed}


async def send_push_to_user(user_id: str, payload: dict, app: str | None = None):
    """Send push notifications to subscriptions of a specific user, optionally filtered by app."""
    results = await database.get_subscriptions_by_user(user_id, app=app)
    if not results:
        print(f"WARNING: No subscriptions found for user {user_id}.")
        return 0, 0  # sent, failed

    success, failed = 0, 0
    for row in results:
        if await send_push(row, payload):
            success += 1
        else:
            failed += 1
    return success, failed


async def send_push(subscription, payload):
    print("Sending push: ", subscription)
    endpoint = subscription["endpoint"]
    platform = subscription.get("platform", "web")
    if platform == "web":
        # Web Push
        try:
            await asyncio.to_thread(
                webpush,
                subscription_info={
                    "endpoint": endpoint,
                    "keys": {
                        "p256dh": subscription["p256dh"],
                        "auth": subscription["auth"],
                    },
                },
                data=json.dumps(payload),
                vapid_private_key=VAPID_PRIVATE_KEY,
                vapid_claims=deepcopy(VAPID_CLAIMS),
            )
            return True
        except WebPushException as ex:
            # Check if subscription is gone or unsubscribed
            print("ERROR WebPushException:", ex, type(ex))
            print("response", ex.response)

            # Invalid p256dh key - remove subscription
            if "Invalid p256dh key" in str(ex):
                print("Invalid p256dh key, removing subscription")
                await database.delete_subscription(endpoint, platform)
                return False

            if ex.response is not None:
                print(
                    "status code",
                    ex.response.status_code,
                    type(ex.response.status_code),
                )
                # Permanent failures - remove subscription
                if ex.response.status_code in [410, 404]:
                    print("Subscription is gone/invalid, removing")
                    await database.delete_subscription(endpoint, platform)
                    print(f"Removed expired subscription: {endpoint}")
                else:
                    print(f"Push failed for {endpoint}: {ex}")
            else:
                # No response - could be network error, don't remove
                print(f"Push failed for {endpoint}: {ex} (no response)")
            return False
    elif platform == "fcm":
        # FCM Token
        if not firebase_app:
            print("Firebase not initialized")
            return False
        try:
            # Start with required args
            message_kwargs = {
                "token": endpoint,
                "data": {k: str(v) for k, v in (payload.get("data") or {}).items()},
            }

            # Add notification only if not silent
            if not payload.get("silent", False):
                message_kwargs["notification"] = messaging.Notification(
                    title=payload.get("title", ""), body=payload.get("body", "")
                )

            if payload.get("android_priority"):
                message_kwargs["android"] = messaging.AndroidConfig(
                    priority=payload["android_priority"]
                )

            message = messaging.Message(**message_kwargs)
            response = await asyncio.to_thread(messaging.send, message)
            print(f"Successfully sent FCM message: {response}")
            return True
        except Exception as ex:
            print(f"FCM push failed for {endpoint}: {ex}")
            # For FCM, remove invalid/not-found tokens
            error_str = str(ex)
            if any(
                err in error_str
                for err in [
                    "registration-token-not-registered",
                    "NOT_FOUND",
                    "Requested entity was not found",
                ]
            ):
                print("FCM token invalid/not-found, removing")
                await database.delete_subscription(endpoint, platform)
            return False
    elif platform == "apns":
        # APNs Token
        # if not apns_client:
        #     print("APNs not initialized")
        #     return False
        # try:
        #     payload = Payload(
        #         alert={"title": payload.get("title", ""), "body": payload.get("body", "")},
        #         sound="default",
        #         badge=1,
        #         custom=payload.get("data", {})
        #     )
        #     response = await asyncio.to_thread(apns_client.send_notification, endpoint, payload)
        #     print(f"Successfully sent APNs message: {response}")
        #     return True
        # except Exception as ex:
        #     print(f"APNs push failed for {endpoint}: {ex}")
        #     # Handle invalid tokens, etc.
        #     return False
        print("APNs push not implemented.")
        return False
    else:
        print(f"Unknown platform {platform}")
        return False


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
