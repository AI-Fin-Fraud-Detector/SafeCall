"""Event types other services can send via POST /internal/notify, and their text."""


def _scam_detected(user: dict) -> str:
    return f"SafeCall\n{user.get('name') or 'SafeCall user'}'s phone scam warning!"


RENDERERS = {
    "scam_detected": _scam_detected,
}
