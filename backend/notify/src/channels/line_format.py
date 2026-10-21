"""LINE-only presentation of a Notification as a Flex Message card.

Generic over events: the card is built from the notification's title, fields
and level, so new event types need no LINE-specific code.
"""

from ..events import Notification

# Header (background, text) per level. Risk colors match the frontend's
# _riskBg / _riskFg (call_page.dart).
LEVEL_STYLES = {
    "info": ("#06C755", "#FFFFFF"),
    "safe": ("#1A3A2A", "#66BB6A"),
    "caution": ("#3A2E0A", "#FFCA28"),
    "warning": ("#3A1E0A", "#FF7043"),
    "danger": ("#3A0A0A", "#EF5350"),
}

# LINE limits altText to 400 characters.
ALT_TEXT_MAX = 400


def _field_row(label: str, value: str) -> dict:
    return {
        "type": "box",
        "layout": "baseline",
        "spacing": "md",
        "contents": [
            {"type": "text", "text": label, "size": "sm", "color": "#888888", "flex": 2},
            {"type": "text", "text": value, "size": "sm", "color": "#222222",
             "weight": "bold", "wrap": True, "flex": 5},
        ],
    }


def flex_message(notification: Notification) -> dict:
    background, foreground = LEVEL_STYLES.get(notification.level, LEVEL_STYLES["info"])
    brand = "SafeCall"
    if notification.level != "info":
        brand = f"SafeCall · {notification.level.capitalize()}"
    return {
        "type": "flex",
        "altText": notification.text[:ALT_TEXT_MAX],
        "contents": {
            "type": "bubble",
            "header": {
                "type": "box",
                "layout": "vertical",
                "backgroundColor": background,
                "paddingAll": "lg",
                "contents": [
                    {"type": "text", "text": brand, "size": "xs", "color": foreground},
                    {"type": "text", "text": notification.title, "size": "lg",
                     "weight": "bold", "color": foreground, "wrap": True},
                ],
            },
            "body": {
                "type": "box",
                "layout": "vertical",
                "spacing": "sm",
                "contents": [_field_row(label, value) for label, value in notification.fields]
                or [{"type": "text", "text": notification.text, "wrap": True, "size": "sm"}],
            },
        },
    }
