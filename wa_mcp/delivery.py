from __future__ import annotations

import logging
import secrets
import time

from .whatsapp import jid as J

log = logging.getLogger(__name__)

REPLY_TOOLS = ("wa_send", "wa_send_media", "wa_typing")

PROTOCOL_METHODS = ("initialize", "ping", "tools/list", "resources/list",
                    "prompts/list", "notifications/initialized")

KV_PREFIX = "token."


def _kv(token: str) -> str:
    return f"{KV_PREFIX}{token}"


async def mint(store, chat_jid: str, ttl_seconds: int) -> str:
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


async def mint_routine(store) -> str:
    token = secrets.token_urlsafe(32)
    await store.put_kv(_kv(token), {
        "token": token,
        "client_id": "routine",
        "scopes": ["reply"],
        "subject": "routine",
        "routine": True,
        "expires_at": None,
    })
    return token


async def load(store, token: str) -> dict | None:
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
            arguments: dict | None, delivery: dict | None = None) -> str | None:
    routine = bool(record and record.get("routine"))
    if not record or not (record.get("delivery_chat") or routine):
        return None

    if method in PROTOCOL_METHODS:
        return None
    if method != "tools/call":
        return f"{method} is not available to this token"

    if tool not in REPLY_TOOLS:
        return (f"{tool} is not available to this token — it may only reply "
                f"in the conversation it was issued for")

    if routine:
        if not delivery:
            return ("this call needs a live reply_token — pass the one from "
                    "the message payload")
        allowed = delivery["delivery_chat"]
    else:
        allowed = record["delivery_chat"]
    target = J.normalise(str((arguments or {}).get("to") or ""))
    if not target:
        return "no destination given"
    if target != allowed:
        return f"this token may only reply to {allowed}, not {target}"
    return None


class Scope:

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
        log.debug("mcp call: subject=%s scoped=%s",
                  (record or {}).get("subject", "unknown-or-rejected"),
                  bool(record and (record.get("delivery_chat") or record.get("routine"))))
        if not record or not (record.get("delivery_chat") or record.get("routine")):
            return await self.app(scope, receive, send)

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

        why = await self._check(body, record)
        if why:
            log.warning("refused %s: %s", record.get("subject", "?"), why)
            return await _error(send, why)

        sent = False

        async def replay():
            nonlocal sent
            if not sent:
                sent = True
                return {"type": "http.request", "body": body, "more_body": False}
            return await receive()

        return await self.app(scope, replay, send)

    async def _check(self, body: bytes, record: dict) -> str | None:
        import json

        try:
            payload = json.loads(body or b"{}")
        except Exception:
            return None
        for call in (payload if isinstance(payload, list) else [payload]):
            if not isinstance(call, dict):
                continue
            params = call.get("params") or {}
            args = params.get("arguments") or {}
            delivery = None
            if record.get("routine") and args.get("reply_token"):
                delivery = await load(self.rt.store, str(args["reply_token"]))
                if delivery and not delivery.get("delivery_chat"):
                    delivery = None
            why = refusal(record, call.get("method", ""),
                          params.get("name", ""), args, delivery)
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
