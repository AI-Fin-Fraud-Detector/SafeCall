from typing import Protocol

from ..events import Notification


class Channel(Protocol):
    """An outbound notification channel (LINE, WhatsApp, mail, ...)."""

    name: str

    async def send(self, user: dict, notification: Notification) -> bool:
        """Deliver `notification` to `user` (a row from database.get_user),
        formatted however suits the channel (`notification.text` is a plain-text
        version for channels without rich formatting).

        Return False when the user has no binding for this channel or the
        provider rejected the message; never raise.
        """
        ...
