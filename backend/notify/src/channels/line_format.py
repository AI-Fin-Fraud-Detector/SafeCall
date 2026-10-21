"""LINE-only presentation of a Notification as a Flex Message card.

Generic over events: the card is built from the notification's title, score,
badges, fields and advice, so new event types need no LINE-specific code.
A single solid card in the level's color (the frontend call page's _riskFg).
"""

from ..events import Notification

# (card background, text) per level. Backgrounds are the frontend's _riskFg
# (call_page.dart); caution is light, so it takes dark text.
LEVEL_STYLES = {
    "info": ("#06C755", "#FFFFFF"),
    "safe": ("#66BB6A", "#FFFFFF"),
    "caution": ("#FFCA28", "#3E2723"),
    "warning": ("#FF7043", "#FFFFFF"),
    "danger": ("#EF5350", "#FFFFFF"),
}

# LINE limits altText to 400 characters.
ALT_TEXT_MAX = 400


def _alpha(color: str, alpha: float) -> str:
    """#RRGGBB -> #RRGGBBAA."""
    return f"{color}{round(alpha * 255):02X}"


def _score_row(score: int, level: str, fg: str) -> dict:
    return {
        "type": "box",
        "layout": "horizontal",
        "alignItems": "center",
        "spacing": "lg",
        "contents": [
            {"type": "text", "text": str(score), "size": "3xl", "weight": "bold",
             "color": fg, "flex": 0},
            {
                "type": "box",
                "layout": "vertical",
                "spacing": "sm",
                "contents": [
                    {"type": "text", "text": level.capitalize(), "size": "sm",
                     "weight": "bold", "color": fg},
                    # Score bar: faint track, solid fill proportional to the score.
                    {
                        "type": "box",
                        "layout": "vertical",
                        "height": "4px",
                        "cornerRadius": "2px",
                        "backgroundColor": _alpha(fg, 0.3),
                        "contents": [
                            {"type": "box", "layout": "vertical",
                             "width": f"{max(1, min(score, 100))}%", "height": "4px",
                             "cornerRadius": "2px", "backgroundColor": fg,
                             "contents": [{"type": "filler"}]},
                        ],
                    },
                ],
            },
        ],
    }


def _field_row(label: str, value: str, fg: str) -> dict:
    return {
        "type": "box",
        "layout": "horizontal",
        "contents": [
            {"type": "text", "text": label, "size": "xs", "color": _alpha(fg, 0.75), "flex": 2},
            {"type": "text", "text": value, "size": "xs", "color": fg, "weight": "bold",
             "wrap": True, "flex": 5, "align": "end"},
        ],
    }


def flex_message(notification: Notification) -> dict:
    bg, fg = LEVEL_STYLES.get(notification.level, LEVEL_STYLES["info"])

    contents = [
        {
            "type": "box",
            "layout": "horizontal",
            "alignItems": "center",
            "spacing": "sm",
            "contents": [
                {"type": "text", "text": notification.title, "size": "md", "weight": "bold",
                 "color": fg, "wrap": True, "flex": 1},
                *[{"type": "text", "text": label, "size": "xxs", "color": _alpha(fg, 0.75),
                   "flex": 0} for label, _ in notification.badges],
            ],
        },
    ]
    if notification.score is not None:
        contents.append(_score_row(notification.score, notification.level, fg))
    if notification.fields:
        contents.append({
            "type": "box",
            "layout": "vertical",
            "spacing": "xs",
            "contents": [_field_row(label, value, fg) for label, value in notification.fields],
        })
    elif notification.score is None:
        contents.append({"type": "text", "text": notification.text, "size": "xs",
                         "color": fg, "wrap": True})
    if notification.advice:
        contents.append({"type": "text", "text": notification.advice, "size": "xs",
                         "color": fg, "weight": "bold", "wrap": True})

    return {
        "type": "flex",
        "altText": notification.text[:ALT_TEXT_MAX],
        "contents": {
            "type": "bubble",
            "size": "kilo",
            "styles": {"body": {"backgroundColor": bg}},
            "body": {
                "type": "box",
                "layout": "vertical",
                "spacing": "md",
                "paddingAll": "16px",
                "contents": contents,
            },
        },
    }
