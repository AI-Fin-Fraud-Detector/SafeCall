from typing import Protocol


class Channel(Protocol):
    """An outbound notification channel (LINE, WhatsApp, mail, ...)."""

    name: str

    async def send(self, user: dict, text: str) -> bool:
        """Deliver `text` to `user` (a row from database.get_user).

        Return False when the user has no binding for this channel or the
        provider rejected the message; never raise.
        """
        ...
