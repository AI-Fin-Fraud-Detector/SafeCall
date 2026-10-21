"""Event types other services can send via POST /internal/notify, and their content.

Renderers build a channel-neutral Notification; each channel decides how to
present it (LINE: Flex Message card, others: plain `text`).
"""

from dataclasses import dataclass, field


@dataclass
class Notification:
    event: str
    title: str
    # Plain-text version, for channels without rich formatting and as a fallback.
    text: str
    # Ordered (label, value) rows describing the event.
    fields: list[tuple[str, str]] = field(default_factory=list)
    # "info", or a risk level from risk_level()
    level: str = "info"
    # 0-100 risk score, when the event has one.
    score: int | None = None
    # Short (label, tone) tags; tone is "safe" | "warning" | "danger".
    badges: list[tuple[str, str]] = field(default_factory=list)
    # One-line recommendation for the recipient.
    advice: str | None = None


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


# Short forms of the frontend's _riskDescription (call_summary_page.dart).
RISK_ADVICE = {
    "safe": "No fraud detected.",
    "caution": "Avoid sharing personal information.",
    "warning": "We recommend ending the call.",
    "danger": "Do not share personal info or transfer money.",
}

# Same labels and tones as the frontend's caller type badge (call_page.dart).
CALLER_TYPE_BADGES = {
    "contact": ("Contact", "safe"),
    "non_contact": ("Unknown number", "warning"),
    "private": ("Private Number", "danger"),
}


def _scam_detected(user: dict, data: dict) -> Notification:
    name = user.get("name") or "SafeCall user"
    caller = data.get("caller_phone") or "Private number"
    if data.get("caller_name"):
        caller = f"{data['caller_name']} ({caller})"

    fields = [("Caller", caller), ("User", name)]
    badge = CALLER_TYPE_BADGES.get(data.get("caller_type"))

    level = "warning"
    score = None
    probability = data.get("scam_probability")
    if probability is not None:
        score = round(probability * 100)
        level = risk_level(score)

    lines = [f"SafeCall\n{name}'s phone scam warning!", f"Caller: {caller}"]
    if score is not None:
        lines.append(f"Scam risk: {score}%")
    return Notification(
        event="scam_detected",
        title="Phone scam warning",
        text="\n".join(lines),
        fields=fields,
        level=level,
        score=score,
        badges=[badge] if badge else [],
        advice=RISK_ADVICE[level],
    )


RENDERERS = {
    "scam_detected": _scam_detected,
}
