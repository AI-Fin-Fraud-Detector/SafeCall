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


def _scam_detected(user: dict, data: dict) -> Notification:
    name = user.get("name") or "SafeCall user"
    caller = data.get("caller_phone") or "Private number"
    if data.get("caller_name"):
        caller = f"{data['caller_name']} ({caller})"

    fields = [("User", name), ("Caller", caller)]
    level = "warning"
    probability = data.get("scam_probability")
    if probability is not None:
        score = round(probability * 100)
        fields.append(("Scam risk", f"{score}%"))
        level = risk_level(score)

    title = "Phone scam warning"
    text = "\n".join(
        [f"SafeCall\n{name}'s phone scam warning!"]
        + [f"{label}: {value}" for label, value in fields[1:]]
    )
    return Notification("scam_detected", title, text, fields, level)


RENDERERS = {
    "scam_detected": _scam_detected,
}
