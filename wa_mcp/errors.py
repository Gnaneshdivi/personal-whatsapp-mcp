from __future__ import annotations


class WhatsAppError(Exception):
    pass


class NotConnected(WhatsAppError):
    pass


class RateLimited(WhatsAppError):

    def __init__(self, retry_after: float = 0.0):
        super().__init__(f"rate limited — retry in {retry_after:.1f}s")
        self.retry_after = retry_after


class SendFailed(WhatsAppError):
    pass
