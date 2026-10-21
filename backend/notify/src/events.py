"""Event types other services can send via POST /internal/notify, and their content.

Renderers build a channel-neutral Notification; each channel decides how to
present it (LINE: Flex Message card, others: plain `text`).
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

TAIPEI = timezone(timedelta(hours=8))


@dataclass
class Notification:
    event: str
    # Short label, e.g. "Scam alert".
    title: str
    # Plain-text version, for channels without rich formatting and as a fallback.
    text: str
    # Main subject line (e.g. the caller) and a one-line detail under it.
    headline: str
    detail: str = ""
    # "info", or a risk level from risk_level()
    level: str = "info"
    # 0-100 risk score, when the event has one.
    score: int | None = None
    # When the event happened, in the user's time zone.
    time: datetime = field(default_factory=lambda: datetime.now(TAIPEI))


def risk_level(score: int) -> str:
    """Risk level for a 0-100 scam score. Same bands as the frontend's
    _riskLabel (call_page.dart): keep the two in sync."""
    if score < 40:
        return "safe"
    if score < 60:
        return "caution"
    if score < 80:
        return "warning"
    return "danger"


# How the caller relates to the user, by fraud's caller_type.
CALLER_TYPE_LABELS = {
    "contact": "Saved contact",
    "non_contact": "Not in contacts",
}


def _scam_detected(user: dict, data: dict) -> Notification:
    name = user.get("name") or "SafeCall user"
    phone = data.get("caller_phone")
    caller_name = data.get("caller_name")

    headline = caller_name or phone or "Hidden number"
    detail = [phone] if caller_name and phone else []
    if data.get("caller_type") in CALLER_TYPE_LABELS:
        detail.append(CALLER_TYPE_LABELS[data["caller_type"]])
    detail.append(f"called {name}")

    level = "warning"
    score = None
    probability = data.get("scam_probability")
    if probability is not None:
        score = round(probability * 100)
        level = risk_level(score)

    lines = [f"SafeCall\n{name}'s phone scam warning!", f"Caller: {phone or 'Hidden number'}"]
    if score is not None:
        lines.append(f"Scam risk: {score}%")
    return Notification(
        event="scam_detected",
        title="Scam alert",
        text="\n".join(lines),
        headline=headline,
        detail=" · ".join(detail),
        level=level,
        score=score,
    )


RENDERERS = {
    "scam_detected": _scam_detected,
}
