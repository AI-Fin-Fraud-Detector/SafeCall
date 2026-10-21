"""LINE-only presentation of a Notification as a Flex Message card.

Generic over events: the card is built from the notification's title, time,
score, headline and detail, so new event types need no LINE-specific code.
One tinted card in the level's call-page colors: title and time, large score
with the level, score bar, headline and detail, then a faint brand/date footer.
"""

from ..events import Notification

# (card background, accent) per level: the frontend's _riskBg / _riskFg
# (call_page.dart), with the danger background one shade darker.
LEVEL_STYLES = {
    "info": ("#0A2A3A", "#4FC3F7"),
    "safe": ("#1A3A2A", "#66BB6A"),
    "caution": ("#3A2E0A", "#FFCA28"),
    "warning": ("#3A1E0A", "#FF7043"),
    "danger": ("#310909", "#EF5350"),
}

# LINE limits altText to 400 characters.
ALT_TEXT_MAX = 400


def _alpha(color: str, alpha: float) -> str:
    """#RRGGBB -> #RRGGBBAA."""
    return f"{color}{round(alpha * 255):02X}"


def _tint(color: str, amount: float) -> str:
    """Mix #RRGGBB toward white by `amount` (0-1)."""
    channels = (int(color[i:i + 2], 16) for i in (1, 3, 5))
    return "#" + "".join(f"{round(c + (255 - c) * amount):02X}" for c in channels)


def _score_row(score: int, level: str, accent: str) -> dict:
    return {
        "type": "box",
        "layout": "baseline",
        "contents": [
            {"type": "text", "text": str(score), "size": "56px", "weight": "bold",
             "color": accent, "flex": 0},
            {"type": "text", "text": "%", "size": "xl", "weight": "bold",
             "color": accent, "flex": 0},
            {"type": "filler"},
            {"type": "text", "text": level.capitalize(), "size": "sm", "weight": "bold",
             "color": accent, "flex": 0},
        ],
    }


def _score_bar(score: int, accent: str) -> dict:
    return {
        "type": "box",
        "layout": "vertical",
        "height": "8px",
        "cornerRadius": "4px",
        "backgroundColor": _alpha(accent, 0.15),
        "contents": [
            {"type": "box", "layout": "vertical", "width": f"{max(1, min(score, 100))}%",
             "height": "8px", "cornerRadius": "4px", "backgroundColor": accent,
             "contents": [{"type": "filler"}]},
        ],
    }


def flex_message(notification: Notification) -> dict:
    card, accent = LEVEL_STYLES.get(notification.level, LEVEL_STYLES["info"])
    soft = _alpha(accent, 0.75)
    faint = _alpha(accent, 0.5)

    contents = [
        {
            "type": "box",
            "layout": "horizontal",
            "contents": [
                {"type": "text", "text": notification.title, "size": "xs", "weight": "bold",
                 "color": soft, "flex": 1},
                {"type": "text", "text": notification.time.strftime("%H:%M"), "size": "xs",
                 "color": soft, "flex": 0},
            ],
        },
    ]
    if notification.score is not None:
        contents.append(_score_row(notification.score, notification.level, accent))
        contents.append(_score_bar(notification.score, accent))
    headline = [{"type": "text", "text": notification.headline, "size": "md",
                 "weight": "bold", "color": _tint(accent, 0.75), "wrap": True}]
    if notification.detail:
        headline.append({"type": "text", "text": notification.detail, "size": "xs",
                         "color": soft, "wrap": True})
    contents.append({"type": "box", "layout": "vertical", "spacing": "xs",
                     "contents": headline})
    contents.append({
        "type": "box",
        "layout": "horizontal",
        "margin": "sm",
        "contents": [
            {"type": "text", "text": "SafeCall", "size": "xxs", "color": faint, "flex": 1},
            {"type": "text", "text": notification.time.strftime("%b %d, %Y"), "size": "xxs",
             "color": faint, "flex": 0},
        ],
    })

    return {
        "type": "flex",
        "altText": notification.text[:ALT_TEXT_MAX],
        "contents": {
            "type": "bubble",
            "size": "kilo",
            "styles": {"body": {"backgroundColor": card}},
            "body": {
                "type": "box",
                "layout": "vertical",
                "spacing": "md",
                "paddingAll": "18px",
                "contents": contents,
            },
        },
    }
