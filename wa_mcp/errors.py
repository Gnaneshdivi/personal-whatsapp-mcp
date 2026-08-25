"""Exception hierarchy.

Everything this package raises derives from WhatsAppError, so a caller can wrap
one `except WhatsAppError` around the whole surface.

Deliberately short. An earlier version carried OwnershipLost, StaleFence,
PairingFailed, AlreadyPaired, LoggedOut and TemporarilyBanned, plus an
http_status on each — vocabulary from a multi-tenant design where several pods
leased one number between them. None of it was ever raised here, and this
server runs one number in one process. An exception nothing raises is a claim
about the code that is not true.
"""
from __future__ import annotations


class WhatsAppError(Exception):
    """Base for everything this package raises."""


class NotConnected(WhatsAppError):
    """No session, or the socket has never come up."""


class RateLimited(WhatsAppError):
    """The local send bucket is empty; wait rather than retrying now.

    Carries the wait so a caller can say how long. Raised with a bare float,
    which str()s to "2.5" and tells the reader nothing, so the message is
    built here.
    """

    def __init__(self, retry_after: float = 0.0):
        super().__init__(f"rate limited — retry in {retry_after:.1f}s")
        self.retry_after = retry_after


class SendFailed(WhatsAppError):
    """The message did not go out. The reason is in the message."""
