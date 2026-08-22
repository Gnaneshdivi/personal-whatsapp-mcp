"""The ASGI app: MCP tools, the web UI, and the auth wrapper around both.

One process serves everything. The MCP endpoint and the browser UI share a
runtime, so a message that arrives while a model is mid-conversation is already
in the store by the time the next tool call runs.
"""
from __future__ import annotations

import logging
import secrets
from datetime import datetime, timezone
from typing import Any

from fastmcp import FastMCP

from .config import Settings, Storage
from .runtime import Runtime
from .store.base import to_ms
from .whatsapp import jid as J

log = logging.getLogger(__name__)

RT: Runtime | None = None


def rt() -> Runtime:
    if RT is None:
        raise ToolError("server is still starting")
    return RT


class ToolError(Exception):
    """Surfaced to the model as text it can act on."""


mcp = FastMCP(
    "whatsapp",
    instructions=(
        "WhatsApp via a linked device on this machine. Read chats and history, "
        "search, send messages and media, react, and manage groups.\n\n"
        "Chat identifiers are JIDs — 919876543210@s.whatsapp.net (direct), "
        "1234-5678@g.us (group), 12345@lid (privacy id). Anywhere a recipient is "
        "taken you may pass a plain international phone number instead, or a "
        "contact name, which is resolved against the chat list.\n\n"
        "Before sending to someone for the first time, confirm the recipient with "
        "the user. Sending is rate limited to protect the account from being "
        "banned; do not send bulk or unsolicited messages."
    ),
)


def _fail(exc: Exception) -> dict[str, Any]:
    from .errors import WhatsAppError

    if isinstance(exc, ToolError):
        return {"ok": False, "error": str(exc)}
    if isinstance(exc, WhatsAppError):
        hint = {
            "not_connected": "No live WhatsApp session. Check wa_status; it may still be syncing.",
            "rate_limited": "Sending too fast. Wait a few seconds and retry.",
            "reauth_required": "The device was unlinked. Pair again from the web UI.",
        }.get(getattr(exc, "code", ""), "")
        return {"ok": False, "error": str(exc), "code": getattr(exc, "code", ""), "hint": hint}
    log.exception("tool failed")
    return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def _ts(value: str | None) -> int | None:
    """Accept ISO-8601 from a model, or nothing."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return to_ms(dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc))
    except ValueError:
        raise ToolError(f"{value!r} is not an ISO-8601 timestamp, e.g. 2026-08-01T00:00:00Z")


async def _resolve_chat(value: str) -> str:
    """Accept a JID, a phone number, or a contact name.

    Name resolution is the difference between "send a message to mom" working
    and the model inventing a JID. On several matches the error names them, so
    the model can ask rather than guess.
    """
    value = (value or "").strip()
    if not value:
        raise ToolError("no chat given")
    if "@" in value or value.lstrip("+").isdigit():
        return J.to_jid(value)

    chats = await rt().store.list_chats(limit=500)
    book = rt().contacts
    matches = [
        c for c in chats
        if value.lower() in book.display_name(c.chat_jid, chat_name=c.name).lower()
    ]
    if not matches:
        raise ToolError(f"no chat matching {value!r}. Use wa_list_chats to see them.")
    if len(matches) > 1:
        listing = "\n".join(
            f"    {book.display_name(c.chat_jid, chat_name=c.name)}  {c.chat_jid}"
            for c in matches[:8]
        )
        raise ToolError(f"{len(matches)} chats match {value!r} — pass one:\n{listing}")
    return matches[0].chat_jid


def _chat_out(c) -> dict:
    return {**c.public(), "name": rt().contacts.display_name(c.chat_jid, chat_name=c.name)}


def _msg_out(m) -> dict:
    return {**m.public(),
            "sender_name": rt().contacts.display_name(
                m.sender_jid or "", push_name=m.sender_name) if m.sender_jid else None}


# ========================================================== connection

@mcp.tool
async def wa_status() -> dict[str, Any]:
    """Whether WhatsApp is linked, connected, and finished syncing.

    Call this first if anything else reports no session. `ready` is false while
    history is still downloading — reads work, but auto-reply is held off.
    """
    try:
        return {"ok": True, **rt().status()}
    except Exception as exc:
        return _fail(exc)


@mcp.tool
async def wa_pair() -> dict[str, Any]:
    """Begin linking a WhatsApp number, and return the QR payload as text.

    Open the returned URL in a browser to scan it — the page refreshes itself as
    the code rotates every ~20 seconds. The raw payload is also returned so it
    can be pasted into any QR generator.
    """
    try:
        r = rt()
        if r.wa is None:
            raise ToolError("server is still starting")
        if r.wa.paired_devices():
            return {"ok": True, "state": "already_paired",
                    "message": "A number is already linked. Use wa_logout to replace it."}
        await r.wa.pair()
        import asyncio
        for _ in range(35):
            if r.wa.qr:
                break
            await asyncio.sleep(0.7)
        base = r.settings.public_base_url or f"http://{r.settings.host}:{r.settings.port}"
        token = r.settings.auth_token
        return {
            "ok": True,
            "state": "awaiting_scan" if r.wa.qr else "starting",
            "qr": r.wa.qr,
            "qr_url": f"{base}/connect" + (f"?k={token}" if token else ""),
            "instructions": "WhatsApp -> Settings -> Linked devices -> Link a device. "
                            "The code expires in about 20 seconds; the page refreshes itself.",
        }
    except Exception as exc:
        return _fail(exc)


@mcp.tool
async def wa_logout() -> dict[str, Any]:
    """Unlink the device. History is kept; the number must be paired again."""
    try:
        return {"ok": True, **await rt().wa.logout()}
    except Exception as exc:
        return _fail(exc)


# ================================================================ read

@mcp.tool
async def wa_list_chats(limit: int = 30, archived: bool = False,
                        query: str = "") -> dict[str, Any]:
    """List conversations, most recent first, with names and unread counts.

    `query` filters by name — use it to find a chat before sending.
    """
    try:
        chats = await rt().store.list_chats(limit=limit, archived=archived,
                                            query=query or None)
        return {"ok": True, "count": len(chats), "chats": [_chat_out(c) for c in chats]}
    except Exception as exc:
        return _fail(exc)


@mcp.tool
async def wa_get_messages(chat: str, limit: int = 30, before_id: str = "",
                          since: str = "", until: str = "") -> dict[str, Any]:
    """Read a conversation, newest first.

    `chat` may be a JID, a phone number or a contact name.
    `before_id` pages backwards. `since`/`until` are ISO-8601 timestamps.
    """
    try:
        jid = await _resolve_chat(chat)
        msgs = await rt().store.get_messages(
            jid, limit=limit, before_id=before_id or None,
            from_ts=_ts(since), to_ts=_ts(until),
        )
        return {"ok": True, "chat_jid": jid, "count": len(msgs),
                "messages": [_msg_out(m) for m in msgs]}
    except Exception as exc:
        return _fail(exc)


@mcp.tool
async def wa_search(query: str, chat: str = "", limit: int = 25,
                    since: str = "", until: str = "") -> dict[str, Any]:
    """Full-text search across message history, best matches first.

    Supports "quoted phrases", -exclusion and prefix*. Scope with `chat`, and
    narrow with `since`/`until` as ISO-8601 timestamps.
    """
    try:
        jid = await _resolve_chat(chat) if chat else None
        msgs = await rt().store.search(query, chat_jid=jid, limit=limit,
                                       from_ts=_ts(since), to_ts=_ts(until))
        return {"ok": True, "count": len(msgs), "messages": [_msg_out(m) for m in msgs]}
    except Exception as exc:
        return _fail(exc)


@mcp.tool
async def wa_get_thread(message_id: str, radius: int = 15) -> dict[str, Any]:
    """Messages surrounding one message — context around a search hit."""
    try:
        msgs = await rt().store.thread_around(message_id, radius)
        return {"ok": True, "count": len(msgs), "messages": [_msg_out(m) for m in msgs]}
    except Exception as exc:
        return _fail(exc)


@mcp.tool
async def wa_unread(chat: str = "") -> dict[str, Any]:
    """Unread count for one chat, or across all chats when `chat` is empty."""
    try:
        jid = await _resolve_chat(chat) if chat else None
        return {"ok": True, "unread": await rt().store.unread_count(jid),
                "chat_jid": jid or "(all)"}
    except Exception as exc:
        return _fail(exc)


# =============================================================== write

@mcp.tool
async def wa_send(to: str, text: str, reply_to: str = "") -> dict[str, Any]:
    """Send a text message.

    `to` may be a JID, an international phone number, or a contact name — if a
    name matches several chats the error lists them, so ask the user which.
    `reply_to` quotes an existing message id.

    Confirm the recipient with the user before messaging someone new.
    """
    try:
        jid = await _resolve_chat(to)
        return {"ok": True, **await rt().wa.send_text(jid, text, reply_to or None)}
    except Exception as exc:
        return _fail(exc)


@mcp.tool
async def wa_send_media(to: str, media_base64: str, kind: str = "image",
                        caption: str = "", filename: str = "") -> dict[str, Any]:
    """Send an image, video, audio, document or sticker.

    `media_base64` is the raw file bytes, base64-encoded.
    `kind` is one of: image, video, audio, document, sticker.
    """
    try:
        jid = await _resolve_chat(to)
        return {"ok": True, **await rt().wa.send_media(
            jid, media_base64, kind, caption or None, filename or None)}
    except Exception as exc:
        return _fail(exc)


@mcp.tool
async def wa_react(chat: str, message_id: str, emoji: str) -> dict[str, Any]:
    """React to a message. Pass an empty emoji to remove the reaction."""
    try:
        return {"ok": True, **await rt().wa.react(await _resolve_chat(chat),
                                                  message_id, emoji)}
    except Exception as exc:
        return _fail(exc)


@mcp.tool
async def wa_mark_read(chat: str, up_to_message_id: str = "") -> dict[str, Any]:
    """Mark a chat as read, clearing its unread badge."""
    try:
        jid = await _resolve_chat(chat)
        ids = [up_to_message_id] if up_to_message_id else []
        return {"ok": True, **await rt().wa.mark_read(jid, ids)}
    except Exception as exc:
        return _fail(exc)


@mcp.tool
async def wa_typing(chat: str, typing: bool = True) -> dict[str, Any]:
    """Show or clear the typing indicator in a chat.

    Worth doing before a slow reply — it is what makes an automated response
    read as a person rather than a bot posting instantly.
    """
    try:
        return {"ok": True, **await rt().wa.set_typing(await _resolve_chat(chat), typing)}
    except Exception as exc:
        return _fail(exc)


@mcp.tool
async def wa_check_number(phone: str) -> dict[str, Any]:
    """Check whether a phone number is on WhatsApp before messaging it.

    `phone` in international format without +, e.g. 919876543210.
    """
    try:
        return {"ok": True, **await rt().wa.check_number(phone)}
    except Exception as exc:
        return _fail(exc)


# ============================================================== plumbing

class Auth:
    """Bearer token, with OAuth discovery deliberately turned off.

    A 401 carrying `WWW-Authenticate: Bearer` tells an MCP client that OAuth
    exists, so it walks /.well-known/*, reaches dynamic client registration,
    fails, and reports "couldn't register with the sign-in service" — never
    trying the token that was in the URL all along. 404 on those paths ends the
    search immediately and the client falls back to the URL as given.
    """

    SKIP = ("/.well-known/", "/register", "/authorize", "/token")

    def __init__(self, app, token: str):
        self.app, self.token = app, token

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not self.token:
            return await self.app(scope, receive, send)

        path = scope.get("path", "")
        if any(path.startswith(p) for p in self.SKIP):
            return await _plain(send, 404, b'{"error":"no_oauth_provider"}')

        headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
        presented = headers.get("authorization", "")
        if not presented:
            # Also accept ?k=<token>: the custom-connector dialog in most MCP
            # clients takes a URL and nothing else, so a header-only scheme
            # cannot be configured there at all.
            for part in scope.get("query_string", b"").decode().split("&"):
                if part.startswith("k="):
                    from urllib.parse import unquote
                    presented = f"Bearer {unquote(part[2:])}"
                    break
        if not presented:
            cookie = headers.get("cookie", "")
            for part in cookie.split(";"):
                if part.strip().startswith("wa_session="):
                    presented = f"Bearer {part.strip()[11:]}"
                    break

        if not secrets.compare_digest(presented, f"Bearer {self.token}"):
            return await _plain(send, 401, b'{"error":"unauthorized"}')
        await self.app(scope, receive, send)


async def _plain(send, status: int, body: bytes) -> None:
    await send({"type": "http.response.start", "status": status,
                "headers": [(b"content-type", b"application/json")]})
    await send({"type": "http.response.body", "body": body})


def create_app(settings: Settings, storage: Storage):
    global RT
    RT = Runtime(settings, storage)

    app = mcp.http_app(
        transport="http",
        stateless_http=True,
        host_origin_protection=False,   # the hostname behind a tunnel is random
    )

    from .web import mount_web
    mount_web(app, RT, settings)

    prior_lifespan = app.router.lifespan_context

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def lifespan(a):
        await RT.start()
        try:
            async with prior_lifespan(a):
                yield
        finally:
            await RT.stop()

    app.router.lifespan_context = lifespan

    if settings.auth_token:
        log.info("bearer auth enabled")
        return Auth(app, settings.auth_token)
    log.warning("WA_AUTH_TOKEN is not set — this server is UNAUTHENTICATED. "
                "Fine on localhost, never behind a tunnel.")
    return app
