"""The ASGI app: MCP tools, the web UI, and the auth wrapper around both.

One process serves everything. The MCP endpoint and the browser UI share a
runtime, so a message that arrives while a model is mid-conversation is already
in the store by the time the next tool call runs.
"""
from __future__ import annotations

import logging
import re
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
    # Phone numbers get written with spaces, dashes and brackets — by people
    # and by models copying them out of a contact card. Testing isdigit() on
    # the raw string sent "+91 98123 45678" down the name-search path, where
    # it matched nothing and failed as an unknown contact.
    if "@" in value:
        return J.to_jid(value)          # a JID: dots are part of the domain
    compact = re.sub(r"[\s\-().]", "", value)
    if compact.lstrip("+").isdigit() and len(compact.lstrip("+")) >= 7:
        return J.to_jid(compact)

    from .search import find_chats

    # Both sources, merged. Looking at chats first and stopping there hid the
    # address book whenever any conversation happened to match: "akbar" landed
    # on "Asif Akbar Brother" without ever mentioning that "Akbar Ktr Srm" and
    # "Akbar Baig" also exist. Ambiguity across the two is still ambiguity.
    candidates: dict[str, tuple[str, str]] = {}
    for c, name in await find_chats(rt(), query=value, limit=50):
        candidates[J.normalise(c.chat_jid)] = (c.chat_jid, name)
    for jid, name in rt().contacts.search(value, limit=50):
        candidates.setdefault(J.normalise(jid), (jid, name))

    if not candidates:
        raise ToolError(f"no chat or contact matching {value!r}. "
                        f"Use wa_search to look, or pass a phone number.")

    # An exact name wins outright: 18 names here are contained in some other
    # name, so "Dad" competed with "Dad's office" and could not be sent to.
    hits = list(candidates.values())
    exact = [h for h in hits if (h[1] or "").strip().lower() == value.lower()]
    if len(exact) == 1:
        return exact[0][0]
    if exact:
        hits = exact                    # narrow to the real tie before listing

    if len(hits) == 1:
        return hits[0][0]

    listing = "\n".join(f"    {name}  {jid}" for jid, name in hits[:8])
    same = len({(n or "").strip().lower() for _, n in hits[:8]}) == 1
    detail = ("They share a name, so the number is the only thing telling them "
              "apart." if same else "")
    raise ToolError(
        f"{len(hits)} chats or contacts match {value!r}. ASK which one is "
        f"meant and wait for an answer — do not pick one. {detail}\n{listing}")


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
async def wa_logout(keep_history: bool = False) -> dict[str, Any]:
    """Unlink the device and delete everything it collected.

    IRREVERSIBLE. WhatsApp sends history exactly once, at pair time, so what
    this deletes cannot be fetched again by pairing — the archive starts empty.

    Clears messages, chats, settings and the local session by default: an
    unlinked server holding a full copy of somebody's conversations is stale,
    unreachable and still readable by anyone with the file.

    `keep_history=True` unlinks only. Ask the user first either way; nothing
    about this is recoverable.
    """
    try:
        return {"ok": True, **await rt().wa.logout(purge=not keep_history)}
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
        from .search import find_chats
        found = await find_chats(rt(), query=query, limit=limit, archived=archived)
        chats = [{**c.public(), "name": name} for c, name in found]
        return {"ok": True, "count": len(chats), "chats": chats}
    except Exception as exc:
        return _fail(exc)


@mcp.tool
async def wa_get_messages(chat: str, limit: int = 30, before_id: str = "",
                          since: str = "", until: str = "") -> dict[str, Any]:
    """Read a conversation, newest first.

    `chat` may be a JID, a phone number or a contact name.
    `before_id` pages backwards. `since`/`until` are ISO-8601 timestamps.
    
    Accepts a name and searches conversations and the address book. Several matches are listed rather than guessed — ask which is meant.
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
async def wa_send(to: str, text: str, reply_to: str = "",
                  reply_token: str = "") -> dict[str, Any]:
    """Send a text message.

    Naming a person or a number: ALWAYS resolve it before sending. Pass the
    name and let this tool search — it looks through both conversations and the
    full address book, including people you have never messaged.

    If more than one match comes back the tool refuses and lists them. Show
    that list to the user, ask which one, and WAIT for the answer. Never pick
    the first, the most recent, or the one that seems likeliest — a message
    sent to the wrong person cannot be recalled.

    `to` takes a JID, an international phone number (spaces and dashes are
    fine), or a contact name. `reply_to` quotes an existing message id.

    `reply_token` is not read here — the delivery gate in front of /mcp checks
    it before the call arrives. A routine's token authorises nothing without
    one, so the parameter has to exist for the call to be accepted at all.
    """
    try:
        jid = await _resolve_chat(to)
        return {"ok": True, **await rt().wa.send_text(jid, text, reply_to or None)}
    except Exception as exc:
        return _fail(exc)


@mcp.tool
async def wa_send_media(to: str, media_base64: str, kind: str = "image",
                        caption: str = "", filename: str = "",
                        reply_token: str = "") -> dict[str, Any]:
    """Send an image, video, audio, document or sticker.

    `media_base64` is the raw file bytes, base64-encoded.
    `kind` is one of: image, video, audio, document, sticker.
    
    Same resolution rules as wa_send: search by name, and ask when more than one matches.
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
async def wa_typing(chat: str, typing: bool = True,
                    reply_token: str = "") -> dict[str, Any]:
    """Show or clear the typing indicator in a chat.

    Worth doing before a slow reply — it is what makes an automated response
    read as a person rather than a bot posting instantly.
    
    Same resolution rules as wa_send.
    
    Resolves `to` the same way as wa_send: by name across chats and the
    address book, refusing and listing when several match — ask which.
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

    SKIP = ("/.well-known/", "/register", "/authorize", "/token", "/revoke")

    def __init__(self, app, token: str, oauth: bool = False, rt=None):
        self.app, self.token, self.oauth = app, token, oauth
        self.rt = rt

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not self.token:
            return await self.app(scope, receive, send)

        path = scope.get("path", "")
        query = scope.get("query_string", b"").decode()

        if any(path.startswith(p) for p in self.SKIP):
            if self.oauth:
                # These are the OAuth endpoints — fastmcp serves them and does
                # its own checks. Gating them behind our token would make the
                # flow impossible, since a client discovering us has no token.
                return await self.app(scope, receive, send)
            return await _plain(send, 404, b'{"error":"no_oauth_provider"}')

        # A browser arriving from /authorize carries no token — the flow id in
        # the URL is the capability, and it is a 24-byte secret minted by us for
        # exactly this request.
        if self.oauth and (path in ("/connect", "/qr.txt")
                           or path.startswith("/api/flow/")) and "flow" in (query + path):
            return await self.app(scope, receive, send)

        # With OAuth on, /mcp is authenticated by fastmcp against a real access
        # token. Rather than run two parallel checks, the static token is
        # REGISTERED as an access token at startup (see create_app) — so it is
        # not a special case at all, just a token that never expires. `?k=` is
        # rewritten into the header fastmcp expects, since connector dialogs
        # cannot set headers.
        if self.oauth and path.startswith("/mcp"):
            headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
            if not headers.get("authorization"):
                for part in query.split("&"):
                    if part.startswith("k="):
                        from urllib.parse import unquote
                        scope = dict(scope)
                        scope["headers"] = list(scope["headers"]) + [
                            (b"authorization", f"Bearer {unquote(part[2:])}".encode())]
                        break
            return await self.app(scope, receive, send)

        headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
        presented = headers.get("authorization", "")
        from_url = False
        if not presented:
            # Also accept ?k=<token>: the custom-connector dialog in most MCP
            # clients takes a URL and nothing else, so a header-only scheme
            # cannot be configured there at all.
            for part in scope.get("query_string", b"").decode().split("&"):
                if part.startswith("k="):
                    from urllib.parse import unquote
                    presented = f"Bearer {unquote(part[2:])}"
                    from_url = True
                    break
        if not presented:
            cookie = headers.get("cookie", "")
            for part in cookie.split(";"):
                if part.strip().startswith("wa_session="):
                    presented = f"Bearer {part.strip()[11:]}"
                    break

        if not secrets.compare_digest(presented, f"Bearer {self.token}"):
            # A delivery token is a real credential too, just a narrow one.
            # Without this branch the hand-off flow cannot authenticate at all
            # when OAuth is off, and the agent gets 401 rather than the scoped
            # access it was issued. Scope decides what it may then reach.
            if not await self._is_delivery(presented):
                return await _plain(send, 401, b'{"error":"unauthorized"}')
        elif from_url and self._is_browser(scope, headers):
            # Trade the token in the URL for a cookie, then send the browser to
            # the bare address. A URL nobody can remember gets pasted into a
            # notes app, and every visit leaves the credential in history, in
            # proxy logs and in the referrer of anything the page loads. One
            # redirect and the address is just https://host/ from then on.
            return await _set_session(send, presented[7:], scope)
        await self.app(scope, receive, send)

    @staticmethod
    def _is_browser(scope, headers: dict) -> bool:
        """A page load, not an API call.

        Only GETs that asked for HTML: redirecting an MCP client or a curl
        would break it, and neither of them keeps cookies anyway.
        """
        return (scope.get("method") == "GET"
                and "text/html" in headers.get("accept", ""))

    async def _is_delivery(self, presented: str) -> bool:
        if not presented.lower().startswith("bearer ") or self.rt is None:
            return False
        from .delivery import load

        return await load(self.rt.store, presented[7:].strip()) is not None


async def _set_session(send, token: str, scope) -> None:
    """Set the session cookie and redirect to the same path without ?k=."""
    from urllib.parse import urlencode, parse_qsl

    rest = [(k, v) for k, v in parse_qsl(scope.get("query_string", b"").decode())
            if k != "k"]
    target = scope.get("path", "/") + (f"?{urlencode(rest)}" if rest else "")
    # HttpOnly so a script cannot read it; SameSite=Lax so it survives the
    # redirect. Secure only when the request actually arrived over TLS —
    # browsers drop a Secure cookie on plain http, and localhost is a normal
    # way to run this. Behind a proxy the scheme is in x-forwarded-proto,
    # since the hop to us is http even when the browser used https.
    headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
    https = (scope.get("scheme") == "https"
             or headers.get("x-forwarded-proto", "").split(",")[0].strip() == "https")
    cookie = (f"wa_session={token}; Path=/; HttpOnly; SameSite=Lax; "
              f"Max-Age={30 * 86400}" + ("; Secure" if https else ""))
    await send({"type": "http.response.start", "status": 303,
                "headers": [(b"location", target.encode()),
                            (b"set-cookie", cookie.encode()),
                            (b"content-length", b"0")]})
    await send({"type": "http.response.body", "body": b""})


async def _plain(send, status: int, body: bytes) -> None:
    await send({"type": "http.response.start", "status": status,
                "headers": [(b"content-type", b"application/json")]})
    await send({"type": "http.response.body", "body": body})


def create_app(settings: Settings, storage: Storage):
    global RT
    RT = Runtime(settings, storage)

    base = settings.public_base_url or f"http://{settings.host}:{settings.port}"
    provider = None
    if settings.oauth:
        from .oauth import WhatsAppOAuth

        provider = WhatsAppOAuth(RT, base)
        RT.oauth = provider
        log.info("OAuth enabled — connectors can pair by scanning the QR")

    # Assigned unconditionally, including None. `mcp` is a module-level object,
    # so setting this only when OAuth is on leaves a provider from a previous
    # app in place — still holding that app's runtime, and its closed store.
    mcp.auth = provider

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
        if provider is not None and settings.auth_token:
            # The configured token becomes an ordinary, non-expiring access
            # token. One code path validates everything, and curl keeps working.
            await RT.store.put_kv(f"oauth.token.{settings.auth_token}", {
                "token": settings.auth_token, "client_id": "static",
                "scopes": [], "expires_at": None, "subject": "static-token",
            })
        try:
            async with prior_lifespan(a):
                yield
        finally:
            await RT.stop()

    app.router.lifespan_context = lifespan

    # Inside Auth, so the token has already been accepted as valid by the time
    # this asks what it is allowed to do — authentication first, then what that
    # identity may reach. Wrapped after the lifespan is set, since a plain ASGI
    # callable has no .router for that assignment.
    from .delivery import Scope

    scoped = Scope(app, RT)

    if settings.auth_token:
        log.info("bearer auth enabled")
        return Auth(scoped, settings.auth_token, oauth=bool(provider), rt=RT)
    log.warning("WA_AUTH_TOKEN is not set — this server is UNAUTHENTICATED. "
                "Fine on localhost, never behind a tunnel.")
    return scoped


# ========================================================== auto-reply

@mcp.tool
async def wa_get_reply_settings() -> dict[str, Any]:
    """Current auto-reply configuration, with secrets redacted.

    Shows which backend is selected, who is in scope, and — when it is not
    firing — the reason why.
    """
    try:
        r = rt()
        ok, why = r.trigger.settings.ready()
        return {"ok": True, "settings": r.trigger.settings.redacted(),
                "would_fire": ok and bool(r.wa and r.wa.sync.state.ready),
                "blocked_by": why or ("" if r.wa and r.wa.sync.state.ready
                                      else "still syncing")}
    except Exception as exc:
        return _fail(exc)


@mcp.tool
async def wa_set_reply_settings(settings_json: str) -> dict[str, Any]:
    """Change the auto-reply configuration. Send only what you are changing.

    Merged over the current settings, so a fragment is safe: {"enabled": true}
    switches replies on and touches nothing else. Unknown keys are ignored.

    Nested objects merge too, so {"reply": {"personal": "all"}} leaves the
    allowlist and the cooldown alone.

    Read it back with wa_get_reply_settings, whose shape this accepts.

    Everything defaults to off. Enabling replies on a real number can get it
    banned — confirm with the user before switching this on.
    """
    try:
        import json as _json

        from .trigger.settings import TriggerSettings

        raw = _json.loads(settings_json)
        if not isinstance(raw, dict):
            raise ToolError("settings_json must be a JSON object")
        # Merged, not replaced: an agent sending {"enabled": true} means to
        # switch replies on, not to clear the model and the allowlist with it.
        raw = rt().trigger.settings.merged_with(raw).to_dict()
        # Never let a redacted value overwrite a real secret.
        current = rt().trigger.settings
        if raw.get("model", {}).get("api_key") == "***":
            raw["model"]["api_key"] = current.model.api_key
        merged = TriggerSettings.from_dict(raw)
        await rt().trigger.save(merged)
        ok, why = merged.ready()
        return {"ok": True, "saved": merged.redacted(),
                "would_fire": ok, "blocked_by": why}
    except ValueError as exc:
        return {"ok": False, "error": f"invalid JSON: {exc}"}
    except Exception as exc:
        return _fail(exc)


@mcp.tool
async def wa_test_reply(text: str = "hello, are you there?",
                        chat: str = "") -> dict[str, Any]:
    """Run the configured backend against a made-up message WITHOUT sending.

    The debugging tool to reach for first: it shows what the model or webhook
    actually returns, including the raw error when it fails.
    """
    try:
        r = rt()
        from .trigger.backends import Context, reply_via_model, reply_via_webhook

        jid = await _resolve_chat(chat) if chat else "test@s.whatsapp.net"
        c = await r.store.get_chat(jid)
        ctx = Context(
            message=text,
            chat_name=r.contacts.display_name(jid, chat_name=c.name if c else None),
            chat_jid=jid, sender_name="Test", sender_jid=jid,
            me_name=getattr(r.wa, "push_name", "") or "me",
            message_id="test", timestamp="0",
            history=[(False, "Test", text)],
        )
        s = r.trigger.settings
        from .trigger.backends import render

        ctx.system = render(s.model.system_prompt, ctx)
        ctx.policy = s.guardrails.as_prompt()
        if s.backend == "model":
            reply = await reply_via_model(s.model, ctx)
        else:
            reply = await reply_via_webhook(s.webhook, ctx)
        return {"ok": True, "backend": s.backend, "reply": reply, "sent": False}
    except Exception as exc:
        return {"ok": False, "backend": rt().trigger.settings.backend,
                "error": str(exc), "sent": False}


@mcp.tool
async def wa_reply_log(limit: int = 20) -> dict[str, Any]:
    """Recent auto-reply decisions and why each one fired or did not.

    The answer to "why didn't it reply?" — every skip records its reason.
    """
    try:
        entries = list(rt().trigger.log)[:limit]
        return {"ok": True, "count": len(entries), "decisions": entries}
    except Exception as exc:
        return _fail(exc)


@mcp.tool
async def wa_delivery_status(chat: str, limit: int = 20) -> dict[str, Any]:
    """Delivery state of your recent messages in a chat: sent, delivered, read.

    Use this to tell whether someone has actually seen a message. `read` is a
    far stronger signal than `delivered` — it is the one worth acting on when
    deciding to follow up or wait.
    """
    try:
        jid = await _resolve_chat(chat)
        msgs = await rt().store.get_messages(jid, limit=limit)
        mine = [m for m in msgs if m.is_from_me]
        return {"ok": True, "chat_jid": jid, "count": len(mine), "messages": [
            {"message_id": m.message_id, "text": (m.text or "")[:120],
             "status": m.status,
             "status_at": (m.public()["timestamp"] if m.status_at else None),
             "timestamp": m.public()["timestamp"]}
            for m in mine]}
    except Exception as exc:
        return _fail(exc)


# ============================================================== groups

@mcp.tool
async def wa_list_groups() -> dict[str, Any]:
    """Groups this number is in, with names."""
    try:
        chats = await rt().store.list_chats(limit=500)
        groups = [_chat_out(c) for c in chats if c.is_group]
        return {"ok": True, "count": len(groups), "groups": groups}
    except Exception as exc:
        return _fail(exc)


@mcp.tool
async def wa_group_info(chat: str) -> dict[str, Any]:
    """Name, topic and participants of a group."""
    try:
        jid = await _resolve_chat(chat)
        info = await rt().wa.group_info(jid)
        return {"ok": True, **info}
    except Exception as exc:
        return _fail(exc)


# =============================================================== media

@mcp.tool
async def wa_download_media(message_id: str) -> dict[str, Any]:
    """Download the media attached to a message and return it base64-encoded.

    Media is fetched on demand rather than eagerly — a large history would
    otherwise fill the disk before anything was read.
    """
    try:
        import base64 as _b64

        data = await rt().wa.download_media(message_id)
        if data is None:
            return {"ok": False, "error": "no media on that message"}
        return {"ok": True, "message_id": message_id, "bytes": len(data),
                "media_base64": _b64.b64encode(data).decode()}
    except Exception as exc:
        return _fail(exc)
