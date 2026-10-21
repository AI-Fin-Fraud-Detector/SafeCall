"""LINE-only presentation of a Notification as a Flex Message card.

Generic over events: the card is built from the notification's title, score,
badges, fields and advice, so new event types need no LINE-specific code.
The look mirrors the frontend call page (call_page.dart): dark page, tinted
risk panel with a large score, level pill and score bar, then a warning banner.
"""

from ..events import Notification

PAGE_BG = "#0D0D1A"
TEXT = "#FFFFFF"
TEXT_MUTED = "#FFFFFF8A"  # white 54%
DIVIDER = "#FFFFFF1F"     # white 12%

# (panel background, accent) per level: the frontend's _riskBg / _riskFg.
LEVEL_STYLES = {
    "info": ("#1A2A3A", "#4FC3F7"),
    "safe": ("#1A3A2A", "#66BB6A"),
    "caution": ("#3A2E0A", "#FFCA28"),
    "warning": ("#3A1E0A", "#FF7043"),
    "danger": ("#3A0A0A", "#EF5350"),
}

# Badge colors per tone: the frontend's caller type badge.
BADGE_COLORS = {
    "safe": "#4CAF50",
    "warning": "#F39C12",
    "danger": "#F44336",
}

# LINE limits altText to 400 characters.
ALT_TEXT_MAX = 400


def _alpha(color: str, alpha: float) -> str:
    """#RRGGBB -> #RRGGBBAA."""
    return f"{color}{round(alpha * 255):02X}"


def _pill(label: str, color: str) -> dict:
    return {
        "type": "box",
        "layout": "vertical",
        "flex": 0,
        "paddingStart": "10px",
        "paddingEnd": "10px",
        "paddingTop": "3px",
        "paddingBottom": "3px",
        "cornerRadius": "20px",
        "backgroundColor": _alpha(color, 0.15),
        "borderColor": _alpha(color, 0.5),
        "borderWidth": "1px",
        "contents": [
            {"type": "text", "text": label, "size": "xs", "weight": "bold", "color": color},
        ],
    }


def _header(notification: Notification) -> dict:
    return {
        "type": "box",
        "layout": "horizontal",
        "alignItems": "center",
        "spacing": "sm",
        "contents": [
            {"type": "text", "text": "SafeCall", "size": "sm", "weight": "bold",
             "color": TEXT_MUTED, "flex": 1},
            *[_pill(label, BADGE_COLORS.get(tone, TEXT_MUTED))
              for label, tone in notification.badges],
        ],
    }


def _risk_panel(score: int, level: str, panel: str, accent: str) -> dict:
    return {
        "type": "box",
        "layout": "vertical",
        "backgroundColor": panel,
        "cornerRadius": "16px",
        "paddingAll": "16px",
        "spacing": "md",
        "contents": [
            {
                "type": "box",
                "layout": "horizontal",
                "alignItems": "flex-end",
                "spacing": "md",
                "contents": [
                    {"type": "text", "text": str(score), "size": "5xl", "weight": "bold",
                     "color": accent, "flex": 0},
                    {
                        "type": "box",
                        "layout": "vertical",
                        "spacing": "xs",
                        "paddingBottom": "8px",
                        "contents": [
                            {"type": "text", "text": "Risk Score", "size": "xs",
                             "color": _alpha(accent, 0.75)},
                            {"type": "box", "layout": "horizontal",
                             "contents": [_pill(level.capitalize(), accent), {"type": "filler"}]},
                        ],
                    },
                ],
            },
            # Score bar: track at 15% accent, fill proportional to the score.
            {
                "type": "box",
                "layout": "vertical",
                "height": "6px",
                "cornerRadius": "3px",
                "backgroundColor": _alpha(accent, 0.15),
                "contents": [
                    {"type": "box", "layout": "vertical", "width": f"{max(1, min(score, 100))}%",
                     "height": "6px", "cornerRadius": "3px", "backgroundColor": accent,
                     "contents": [{"type": "filler"}]},
                ],
            },
        ],
    }


def _field_row(label: str, value: str) -> dict:
    return {
        "type": "box",
        "layout": "horizontal",
        "spacing": "md",
        "contents": [
            {"type": "text", "text": label, "size": "sm", "color": TEXT_MUTED, "flex": 2},
            {"type": "text", "text": value, "size": "sm", "color": TEXT, "weight": "bold",
             "wrap": True, "flex": 5, "align": "end"},
        ],
    }


def _advice_banner(advice: str, panel: str, accent: str) -> dict:
    return {
        "type": "box",
        "layout": "horizontal",
        "spacing": "md",
        "paddingAll": "14px",
        "cornerRadius": "14px",
        "backgroundColor": panel,
        "borderColor": _alpha(accent, 0.55),
        "borderWidth": "medium",
        "contents": [
            {"type": "text", "text": "⚠️", "size": "lg", "flex": 0, "gravity": "center"},
            {"type": "text", "text": advice, "size": "sm", "color": accent, "weight": "bold",
             "wrap": True, "gravity": "center"},
        ],
    }


def flex_message(notification: Notification) -> dict:
    panel, accent = LEVEL_STYLES.get(notification.level, LEVEL_STYLES["info"])

    contents = [
        _header(notification),
        {"type": "text", "text": notification.title, "size": "xl", "weight": "bold",
         "color": TEXT, "wrap": True},
    ]
    if notification.score is not None:
        contents.append(_risk_panel(notification.score, notification.level, panel, accent))
    if notification.fields:
        contents.append({"type": "separator", "color": DIVIDER})
        contents.append({
            "type": "box",
            "layout": "vertical",
            "spacing": "md",
            "contents": [_field_row(label, value) for label, value in notification.fields],
        })
    elif notification.score is None:
        contents.append({"type": "text", "text": notification.text, "size": "sm",
                         "color": TEXT, "wrap": True})
    if notification.advice:
        contents.append(_advice_banner(notification.advice, panel, accent))

    return {
        "type": "flex",
        "altText": notification.text[:ALT_TEXT_MAX],
        "contents": {
            "type": "bubble",
            "size": "mega",
            "styles": {"body": {"backgroundColor": PAGE_BG}},
            "body": {
                "type": "box",
                "layout": "vertical",
                "spacing": "lg",
                "paddingAll": "20px",
                "contents": contents,
            },
        },
    }
