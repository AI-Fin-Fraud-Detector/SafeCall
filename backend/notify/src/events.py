"""Event types other services can send via POST /internal/notify, and their text."""


def _scam_detected(user: dict, data: dict) -> str:
    text = f"SafeCall\n{user.get('name') or 'SafeCall user'}'s phone scam warning!"
    caller = data.get("caller_phone") or "Private number"
    if data.get("caller_name"):
        caller = f"{data['caller_name']} ({caller})"
    text = f"{text}\nCaller: {caller}"
    if data.get("scam_probability") is not None:
        text = f"{text}\nScam risk: {round(data['scam_probability'] * 100)}%"
    return text


RENDERERS = {
    "scam_detected": _scam_detected,
}
