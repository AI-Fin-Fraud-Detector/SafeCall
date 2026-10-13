import os

import httpx


LINE_MESSAGING_CHANNEL_ACCESS_TOKEN = os.getenv(
    "LINE_MESSAGING_CHANNEL_ACCESS_TOKEN",
    "",
)

LINE_PUSH_URL = "https://api.line.me/v2/bot/message/push"


async def send_line_fraud_alert(
    line_user_id: str,
    user_name: str,
) -> bool:
    if not line_user_id:
        print(
            "[LINE] User has no bound LINE account.",
            flush=True,
        )
        return False

    if not LINE_MESSAGING_CHANNEL_ACCESS_TOKEN:
        print(
            "[LINE] Messaging channel access token "
            "is not configured.",
            flush=True,
        )
        return False

    message = (
        "SafeCall\n"
        f"{user_name}'s phone scam warning!"
    )

    payload = {
        "to": line_user_id,
        "messages": [
            {
                "type": "text",
                "text": message,
            }
        ],
    }

    try:
        async with httpx.AsyncClient(
            timeout=10.0
        ) as client:
            response = await client.post(
                LINE_PUSH_URL,
                json=payload,
                headers={
                    "Authorization": (
                        "Bearer "
                        f"{LINE_MESSAGING_CHANNEL_ACCESS_TOKEN}"
                    ),
                    "Content-Type": "application/json",
                },
            )

        if 200 <= response.status_code < 300:
            print(
                f"[LINE] Fraud alert sent to {user_name}",
                flush=True,
            )
            return True

        print(
            "[LINE] Push failed: "
            f"{response.status_code} "
            f"{response.text}",
            flush=True,
        )
        return False

    except Exception as e:
        print(
            f"[LINE] Push error: {e}",
            flush=True,
        )
        return False