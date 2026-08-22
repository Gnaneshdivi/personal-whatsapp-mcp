"""Delivery tokens: one reply, one chat, a few minutes.

Fire-and-forget hands an untrusted message to an agent that holds this
connector. That agent can reach every conversation on the account, and the
message it is reasoning about was written by anyone who knows the number. The
instruction telling it to reply only in that chat is a sentence in a prompt —
it raises the cost of an attack without being a boundary, and prompt injection
is precisely the technique for talking a model out of its instructions.

So the boundary is not asked of the model. The webhook is given a token minted
for that one delivery, and this server refuses anything outside it:

    full token      all 22 tools, every chat, no expiry   (your own connector)
    delivery token  send/typing only, one chat, minutes   (per inbound message)

An agent that has been completely talked over can then do exactly one thing:
reply in the conversation that triggered it. Reading other chats, listing
groups, downloading media and messaging a different number are not refusals it
has to be persuaded into — they are not available.

The decision is a pure function so it can be tested without a socket, an agent
or a live account.
"""
from __future__ import annotations

import secrets
import time

from .whatsapp import jid as J

# Everything a reply legitimately needs, and nothing else. Each of these takes
# the destination as `to`, which is what makes confining them to one chat
# checkable rather than a matter of trust.
REPLY_TOOLS = ("wa_send", "wa_send_media", "wa_typing")

# The MCP handshake. Refusing these would stop the client connecting at all.
PROTOCOL_METHODS = ("initialize", "ping", "tools/list", "resources/list",
                    "prompts/list", "notifications/initialized")

KV_PREFIX = "oauth.token."


def _kv(token: str) -> str:
    return f"{KV_PREFIX}{token}"


async def mint(store, chat_jid: str, ttl_seconds: int) -> str:
    """A token good for replying to one chat, until it expires.

    Written through the same kv the OAuth tokens use, so the normal auth path
    accepts it without a parallel code path deciding what counts as a valid
    token — a second such path is how one of them ends up more permissive.
    """
    token = secrets.token_urlsafe(32)
    await store.put_kv(_kv(token), {
        "token": token,
        "client_id": "delivery",
        "scopes": ["reply"],
        "subject": "delivery",
        "delivery_chat": J.normalise(chat_jid),
        "expires_at": time.time() + max(30, int(ttl_seconds)),
    })
    return token


async def load(store, token: str) -> dict | None:
    """The stored record, or None when it is unknown or expired."""
    if not token:
        return None
    raw = await store.get_kv(_kv(token))
    if not raw:
        return None
    expires = raw.get("expires_at")
    if expires is not None and expires < time.time():
        return None
    return raw


def refusal(record: dict | None, method: str, tool: str,
            arguments: dict | None) -> str | None:
    """Why this call is not allowed, or None if it is.

    `record` is the token's stored form. Anything without `delivery_chat` is an
    ordinary full-access token and is never restricted here.
    """
    if not record or not record.get("delivery_chat"):
        return None                      # a full token; not our business

    if method in PROTOCOL_METHODS:
        return None
    if method != "tools/call":
        return f"{method} is not available to a delivery token"

    if tool not in REPLY_TOOLS:
        return (f"{tool} is not available to a delivery token — it may only "
                f"reply in the conversation it was issued for")

    allowed = record["delivery_chat"]
    target = J.normalise(str((arguments or {}).get("to") or ""))
    if not target:
        return "no destination given"
    if target != allowed:
        # The whole point. An injected "message this to 9199..." arrives here
        # as an ordinary, well-formed call — the only thing that distinguishes
        # it from a legitimate reply is the destination.
        return (f"this token may only reply to {allowed}, not {target}")
    return None


class Scope:
    """ASGI enforcement for /mcp.

    One gate rather than a check inside each of 22 tools: a tool added later
    without the check would silently be reachable, and a security boundary that
    depends on remembering to opt in is not one.

    The body has to be read to see which tool is being called, so it is
    buffered and replayed downstream. MCP bodies are a JSON-RPC envelope, not
    uploads, so this is bounded by MAX_BODY rather than being a way to make the
    server hold anything large.
    """

    MAX_BODY = 256 * 1024

    def __init__(self, app, rt):
        self.app, self.rt = app, rt

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not scope.get("path", "").startswith("/mcp"):
            return await self.app(scope, receive, send)

        headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
        auth = headers.get("authorization", "")
        token = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
        record = await load(self.rt.store, token) if token else None
        if not record or not record.get("delivery_chat"):
            return await self.app(scope, receive, send)   # full token, or none

        body, more, size = b"", True, 0
        buffered = []
        while more:
            msg = await receive()
            buffered.append(msg)
            body += msg.get("body", b"")
            size += len(msg.get("body", b""))
            if size > self.MAX_BODY:
                return await _error(send, "request too large for a delivery token")
            more = msg.get("more_body", False)

        why = self._check(body, record)
        if why:
            return await _error(send, why)

        sent = False

        async def replay():
            nonlocal sent
            if not sent:
                sent = True
                return {"type": "http.request", "body": body, "more_body": False}
            return await receive()

        return await self.app(scope, replay, send)

    def _check(self, body: bytes, record: dict) -> str | None:
        import json

        try:
            payload = json.loads(body or b"{}")
        except Exception:
            return None                  # not ours to reject; let MCP answer
        # Batched calls are a list; every one of them has to pass.
        for call in (payload if isinstance(payload, list) else [payload]):
            if not isinstance(call, dict):
                continue
            params = call.get("params") or {}
            why = refusal(record, call.get("method", ""),
                          params.get("name", ""), params.get("arguments") or {})
            if why:
                return why
        return None


async def _error(send, why: str) -> None:
    import json

    body = json.dumps({
        "jsonrpc": "2.0", "id": None,
        "error": {"code": -32001, "message": f"refused: {why}"},
    }).encode()
    await send({"type": "http.response.start", "status": 403,
                "headers": [(b"content-type", b"application/json"),
                            (b"content-length", str(len(body)).encode())]})
    await send({"type": "http.response.body", "body": body})
