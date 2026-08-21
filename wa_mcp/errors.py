"""Exception hierarchy.

Every error the package raises derives from WhatsAppError, so a caller can wrap
one `except WhatsAppError` around the whole surface. The HTTP status each maps to
is attached here rather than in the worker's route layer — the MCP gateway and
the worker must agree on what a 409 means, and that agreement belongs in one file.
"""
from __future__ import annotations


class WhatsAppError(Exception):
    """Base for everything this package raises."""

    http_status = 500
    code = "wa_error"


class NotConnected(WhatsAppError):
    """No active wa_connections row, or the socket has never come up."""

    http_status = 409
    code = "not_connected"


class OwnershipLost(WhatsAppError):
    """This pod's lease expired or was stolen; it may no longer touch the socket.

    Raised by the local lease check on the send path. The caller should re-resolve
    the owner and retry — a different pod now holds the number.
    """

    http_status = 409
    code = "ownership_lost"


class StaleFence(WhatsAppError):
    """Caller routed on a fence that is no longer current.

    The gateway read `wa:owner:*` and POSTed to the worker, but ownership moved in
    between. Caller re-reads the lock and retries once.
    """

    http_status = 409
    code = "stale_fence"

    def __init__(self, current_fence: int | None = None):
        super().__init__(f"stale fence; current={current_fence}")
        self.current_fence = current_fence


class LoggedOut(WhatsAppError):
    """WhatsApp logged this device out. Requires a fresh QR/pair-code scan."""

    http_status = 401
    code = "reauth_required"


class TemporarilyBanned(WhatsAppError):
    """WhatsApp issued a temporary ban on this account."""

    http_status = 403
    code = "temporarily_banned"

    def __init__(self, expires_at=None, reason: str = ""):
        super().__init__(f"temporarily banned: {reason}")
        self.expires_at = expires_at
        self.reason = reason


class RateLimited(WhatsAppError):
    """Local send-side throttle tripped. Ban protection, not a WhatsApp response."""

    http_status = 429
    code = "rate_limited"

    def __init__(self, retry_after: float):
        super().__init__(f"rate limited, retry in {retry_after:.1f}s")
        self.retry_after = retry_after


class SendFailed(WhatsAppError):
    """The socket accepted the call but WhatsApp rejected or dropped it."""

    http_status = 502
    code = "send_failed"


class PairingFailed(WhatsAppError):
    """QR expired, pair code rejected, or the pairing socket died."""

    http_status = 400
    code = "pairing_failed"


class AlreadyPaired(WhatsAppError):
    """A device is already paired in this session store.

    Opening a second pairing socket would load the SAME device (neonize selects by
    `jid`, not `uuid`), authenticate as the same account, and trigger a
    StreamReplaced that panics the Go layer and kills the worker. Refusing is the
    only safe answer — the caller should read the existing connection instead.
    """

    http_status = 409
    code = "already_paired"

    def __init__(self, phone_jid: str = ""):
        super().__init__(f"already paired: {phone_jid}")
        self.phone_jid = phone_jid
