"""The browser UI: chat list, conversation, pairing, live sync progress.

Server-rendered HTML and one EventSource. No build step, because a
`node_modules` in the install path of a `pip install` tool is the difference
between someone trying it and closing the tab.
"""
from __future__ import annotations

import asyncio
import html
import json
import time
import logging

import segno

from .config import Settings, data_dir
from .runtime import Runtime

log = logging.getLogger(__name__)

CSS = """
*{box-sizing:border-box}
body{margin:0;font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
     background:#0b141a;color:#e9edef}
a{color:#53bdeb;text-decoration:none}
.wrap{display:grid;grid-template-columns:340px 1fr;height:100vh}
@media(max-width:760px){.wrap{grid-template-columns:1fr}.pane{display:none}.wrap.open .list{display:none}.wrap.open .pane{display:flex}}
.list{border-right:1px solid #222d34;overflow-y:auto;background:#111b21}
.hd{padding:14px 16px;border-bottom:1px solid #222d34;display:flex;align-items:center;gap:10px;
    position:sticky;top:0;background:#111b21;z-index:2}
.hd b{font-size:15px}
.dot{width:8px;height:8px;border-radius:50%;background:#00a884;flex:none}
.dot.warn{background:#f0b232}.dot.off{background:#8696a0}
.sync{padding:10px 16px;background:#182229;border-bottom:1px solid #222d34;font-size:13px;color:#8696a0}
.bar{height:3px;background:#222d34;border-radius:2px;margin-top:8px;overflow:hidden}
.bar i{display:block;height:100%;background:#00a884;transition:width .4s}
.row{display:flex;gap:12px;padding:11px 16px;border-bottom:1px solid #1b262d;cursor:pointer}
.row:hover{background:#182229}
.av{width:40px;height:40px;border-radius:50%;background:#2a3942;flex:none;display:grid;
    place-items:center;font-weight:600;color:#8696a0;font-size:15px;
    position:relative;overflow:hidden}
.av img{position:absolute;inset:0;width:100%;height:100%;object-fit:cover}
.meta{min-width:0;flex:1}
.nm{font-weight:500;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.pv{color:#8696a0;font-size:13px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.badge{background:#00a884;color:#111b21;border-radius:10px;padding:1px 7px;font-size:12px;
       font-weight:600;align-self:center}
.pin{align-self:center;font-size:12px;opacity:.75;margin-right:4px}
.pane{display:flex;flex-direction:column;background:#0b141a}
.pane .hd{background:#202c33;border-bottom:1px solid #222d34}
.msgs{flex:1;overflow-y:auto;padding:18px;display:flex;flex-direction:column;gap:6px}
.m{max-width:min(75%,560px);padding:7px 11px;border-radius:8px;background:#202c33;
   align-self:flex-start;word-wrap:break-word}
.m.me{background:#005c4b;align-self:flex-end}
.m .t{font-size:11px;color:#8696a0;margin-top:3px;text-align:right}
.m .s{font-size:12px;color:#53bdeb;margin-bottom:2px}
.empty{margin:auto;color:#8696a0;text-align:center;padding:40px}
.qr{background:#fff;padding:16px;border-radius:14px;line-height:0;
    width:min(86vw,420px);box-sizing:content-box;margin:0 auto}
.qr svg{display:block;width:100%;height:auto}
code{background:#182229;padding:2px 5px;border-radius:4px;font-size:12px;word-break:break-all}
.set{max-width:720px;margin:0 auto;padding:28px 20px 60px}
.set h1{font-size:20px;margin:0 0 6px}
.set h2{font-size:14px;text-transform:uppercase;letter-spacing:.08em;color:#8696a0;
        margin:28px 0 10px;padding-bottom:6px;border-bottom:1px solid #222d34}
.set label{display:block;margin:12px 0 4px;font-size:13px;color:#8696a0}
.set input,.set select,.set textarea{width:100%;background:#111b21;color:#e9edef;
   border:1px solid #2a3942;border-radius:7px;padding:9px 11px;font:inherit;font-size:14px}
.set textarea{font-family:ui-monospace,monospace;font-size:13px;min-height:84px;resize:vertical}
.set .two{display:grid;grid-template-columns:1fr 1fr;gap:12px}
.set button{background:#00a884;color:#0b141a;border:0;border-radius:7px;padding:10px 18px;
   font:inherit;font-weight:600;cursor:pointer;margin-top:18px}
.set button.ghost{background:#2a3942;color:#e9edef;margin-left:8px}
.warn{background:#3b2a15;border:1px solid #6b4a1f;border-radius:8px;padding:12px 14px;
      font-size:13px;color:#f0c987;margin:14px 0}
.ok{color:#00a884}.bad{color:#f15c6d}
.pill{display:inline-block;padding:2px 9px;border-radius:11px;font-size:12px;background:#2a3942}
.pill.on{background:#00a884;color:#0b141a}
#out{white-space:pre-wrap;font-family:ui-monospace,monospace;font-size:12px;
     background:#111b21;border:1px solid #2a3942;border-radius:7px;padding:11px;margin-top:12px}
"""


def _esc(s) -> str:
    return html.escape(str(s or ""))


def qr_svg(payload: str) -> str:
    """Render a pairing code as a responsive inline SVG.

    `omitsize=True` is the whole trick. segno's default emits
    `width="690" height="690"` and NO viewBox, so CSS `width` resizes the
    element box while the drawing stays at its intrinsic 690px and spills out
    of whatever contains it. With a viewBox and no fixed dimensions the code
    scales to its container, which is what every other image on the web does.

    border=4 is the spec's minimum quiet zone. At 3 the finder patterns sit too
    close to the edge and some scanners refuse the code — it looks fine and
    simply will not read.
    """
    svg = segno.make_qr(payload).svg_inline(
        scale=10, border=4, dark="#000", light="#fff", omitsize=True)
    return svg if isinstance(svg, str) else svg.decode()


def _page(title: str, body: str) -> str:
    return (f'<!doctype html><meta charset="utf-8"><title>{_esc(title)}</title>'
            f'<meta name="viewport" content="width=device-width,initial-scale=1">'
            f"<style>{CSS}</style>{body}")


def mount_web(app, rt: Runtime, settings: Settings) -> None:
    from starlette.responses import (HTMLResponse, JSONResponse, PlainTextResponse,
                                 Response, StreamingResponse)
    from starlette.routing import Route

    def _q(request) -> str:
        k = request.query_params.get("k", "")
        return f"?k={k}" if k else ""

    async def index(request):
        st = rt.status()
        if not st["number"] and st["phase"] in ("unpaired", "logged_out"):
            return Response(status_code=307,
                            headers={"location": f"/connect{_q(request)}"})
        from .ui import CSS as APP_CSS, shell
        return HTMLResponse(
            f'<!doctype html><meta charset="utf-8"><title>WhatsApp</title>'
            f'<meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<style>{APP_CSS}</style>{shell(_q(request))}')

    async def connect(request):
        st = rt.status()
        flow = request.query_params.get("flow", "")

        # An OAuth flow that is already satisfied — the number was linked before
        # the client asked — should not make the user scan again. Hand the code
        # straight back.
        if flow and st["number"] and rt.oauth is not None:
            back = await rt.oauth.complete_flow(flow)
            if back:
                return Response(status_code=303, headers={"location": back})

        if st["number"]:
            # Straight into the app. A "Connected" screen with a link is a step
            # nobody wants after they have already succeeded.
            return Response(status_code=303, headers={"location": f"/{_q(request)}"})

        if rt.wa is not None and not rt.wa.qr:
            try:
                await rt.wa.pair()
            except Exception as exc:
                # Show it. Swallowing this rendered a spinner forever while the
                # real cause was a one-line install command.
                log.error("pairing could not start: %s", exc)
                return HTMLResponse(_page("Cannot pair", f"""
<div style="display:grid;place-items:center;height:100vh;padding:24px">
 <div style="max-width:480px">
  <h2 style="color:#f15c6d;font-size:17px">Pairing cannot start</h2>
  <pre style="white-space:pre-wrap;background:#182229;padding:14px;border-radius:8px;
       font-size:13px">{_esc(exc)}</pre>
  <p style="color:#8696a0;font-size:13px">Fix it, restart the server, then reload
     this page.</p>
 </div></div>"""), status_code=503)
            for _ in range(30):
                if rt.wa.qr:
                    break
                await asyncio.sleep(0.7)

        qr = rt.wa.qr if rt.wa else None
        if not qr:
            return HTMLResponse(_page("Connecting", """
<div style="display:grid;place-items:center;height:100vh">
<div style="text-align:center"><p>Preparing a code&hellip;</p></div></div>
<script>setTimeout(()=>location.reload(),2000)</script>"""))

        svg = qr_svg(qr)
        # During an OAuth flow the page polls for the pairing and then hands the
        # browser back to whoever started it. Without this the user scans, sees
        # a chat list, and the connector sits waiting forever.
        after = (f"fetch('/api/flow/{_esc(flow)}').then(r=>r.json()).then(d=>{{"
                 f"if(d.redirect) location.href=d.redirect;}});") if flow else ""
        return HTMLResponse(_page("Link WhatsApp", f"""
<div style="display:grid;place-items:center;height:100vh;padding:20px">
 <div style="text-align:center;max-width:420px">
  <h2 style="font-size:17px">Link your WhatsApp</h2>
  <div class="qr">{svg}</div>
  <p style="color:#8696a0;font-size:13px">
    WhatsApp &rarr; Settings &rarr; Linked devices &rarr; Link a device</p>
  <p id="phase" style="color:#00a884;font-size:13px">Waiting for you to scan&hellip;</p>
  <details style="text-align:left;margin-top:14px">
   <summary style="color:#53bdeb;font-size:13px;cursor:pointer">Copy the code as text</summary>
   <p><code>{_esc(qr)}</code></p>
   <p style="color:#8696a0;font-size:12px">Expires ~20s after it appeared.
      This page refreshes itself.</p>
  </details>
 </div></div>
<script>
 // Pause the reload while the copy panel is open, or it clears the selection.
 // Poll status rather than blind-reloading: a reload while pairing is in
 // flight used to start a second client.
 setInterval(async () => {{
   {after}
   const s = await (await fetch("/api/status{_q(request)}")).json();
   if (s.number) {{ location.href = "/{_q(request)}"; return; }}
   const el = document.getElementById("phase");
   if (el) el.textContent = s.phase === "pairing"
     ? "Waiting for you to scan…" : ("Status: " + s.phase);
   if (!document.querySelector("details[open]") && !s.number) location.reload();
 }}, 4000);
</script>"""))

    async def chats_api(request):
        q = request.query_params
        from .search import find_chats, find_contacts, find_messages
        query = q.get("q") or ""
        found = await find_chats(
            rt, query=query, kind=q.get("filter") or "all",
            limit=int(q.get("limit", 60)), archived=q.get("archived") == "1")
        out = []
        for c, name in found:
            d = c.public(); d["name"] = name
            out.append(d)
        # One query, two lists — the same shape WhatsApp answers with.
        messages = await find_messages(rt, query=query, limit=25) if query else []
        # Three lists, like WhatsApp: who you talk to, who you could talk to,
        # and where it was said.
        contacts = [{"chat_jid": j, "name": n}
                    for j, n in await find_contacts(rt, query=query, limit=15)] \
            if query else []
        return JSONResponse({"chats": out, "contacts": contacts,
                             "messages": messages})

    async def messages_api(request):
        """A page of history, oldest-first for direct rendering.

        before_id pages BACKWARDS from a known message rather than by offset:
        rows arrive continuously, and an offset would silently skip or repeat
        messages as new ones land above the window.
        """
        jid = request.path_params["jid"]
        q = request.query_params
        msgs = await rt.store.get_messages(
            jid, limit=int(q.get("limit", 40)), before_id=q.get("before") or None)
        chat = await rt.store.get_chat(jid)
        out = []
        for m in reversed(msgs):                 # store returns newest-first
            d = m.public()
            d["sender_name"] = (rt.contacts.display_name(
                m.sender_jid or "", push_name=m.sender_name)
                if m.sender_jid and not m.is_from_me else None)
            out.append(d)
        return JSONResponse({
            "messages": out,
            "has_more": len(msgs) == int(q.get("limit", 40)),
            "chat": {"jid": jid, "is_group": bool(chat and chat.is_group),
                     "name": rt.contacts.display_name(
                         jid, chat_name=chat.name if chat else None)},
        })

    async def send_api(request):
        body = await request.json()
        try:
            res = await rt.wa.send_text(body["to"], body["text"])
            return JSONResponse({"ok": True, **res})
        except Exception as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)

    async def read_api(request):
        try:
            await rt.wa.mark_read(request.path_params["jid"], [])
        except Exception:
            await rt.store.set_unread(request.path_params["jid"], 0)
        return JSONResponse({"ok": True})

    async def status_api(request):
        return JSONResponse(rt.status())

    async def flow_api(request):
        """Has this OAuth flow's pairing completed? If so, where to send them."""
        if rt.oauth is None:
            return JSONResponse({"redirect": None})
        if not rt.status()["number"]:
            return JSONResponse({"redirect": None, "waiting": True})
        back = await rt.oauth.complete_flow(request.path_params["flow"])
        return JSONResponse({"redirect": back})

    async def sign_out(request):
        """Clear the session cookie and ask for the token again.

        Only the browser session — the WhatsApp device stays linked and every
        message stays where it is. Unlinking is a different thing entirely and
        is not a button, because history syncs once at pair time and cannot be
        fetched again.
        """
        # Max-Age=0 rather than an empty value: an empty cookie still presents
        # itself and would fail auth on every request instead of falling back
        # to asking for the token.
        headers = {"Set-Cookie": "wa_session=; Path=/; HttpOnly; SameSite=Lax; "
                                 "Max-Age=0",
                   "Cache-Control": "no-store"}
        return HTMLResponse(
            "<!doctype html><meta charset=utf-8><title>Signed out</title>"
            "<style>body{background:#0b141a;color:#e9edef;font:15px/1.6 "
            "-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;"
            "display:grid;place-items:center;height:100vh;margin:0;text-align:center}"
            "p{color:#8696a0;max-width:34ch}</style>"
            "<div><h2>Signed out</h2>"
            "<p>This browser will need the token again. WhatsApp is still "
            "linked and nothing has been deleted.</p></div>",
            headers=headers)

    async def revoke_all(request):
        """Expire every issued credential. Not the configured one.

        Signing out of a browser leaves connectors untouched: an OAuth token
        lasts thirty days, and a routine token does not expire at all. Without
        this the only way to sign a lost or unwanted client out is to wait, or
        to change WA_AUTH_TOKEN and restart — which signs out everything
        including you.

        WA_AUTH_TOKEN itself is deliberately spared. It comes from the
        environment and is re-registered on every start, so revoking it would
        do nothing after a restart while locking you out until then.
        """
        keep = f"oauth.token.{settings.auth_token}" if settings.auth_token else ""
        revoked = 0
        for prefix in ("oauth.token.", "oauth.refresh."):
            for key in await rt.store.list_kv(prefix):
                if key == keep:
                    continue
                await rt.store.put_kv(key, {"expires_at": 0})
                revoked += 1
        log.warning("revoked %d issued credential(s)", revoked)
        return JSONResponse({"ok": True, "revoked": revoked},
                            headers={"Set-Cookie": "wa_session=; Path=/; "
                                                   "HttpOnly; SameSite=Lax; Max-Age=0"})

    async def settings_page(request):
        from .settings_ui import build

        return HTMLResponse(build(rt, _q(request), rt.status()))

    async def settings_api(request):
        raw = await request.json()
        from .trigger.settings import TriggerSettings
        cur = rt.trigger.settings
        if isinstance(raw.get("model"), dict) and raw["model"].get("api_key") in ("***", ""):
            raw["model"]["api_key"] = cur.model.api_key
        merged = TriggerSettings.from_dict(raw)
        await rt.trigger.save(merged)
        # Two independent things stop a reply: the settings not being usable,
        # and the sync gate still being closed. Only the first had a reason
        # attached, so a perfectly good config saved during a sync reported
        # "not firing" with nothing after the colon.
        ok, why = merged.ready()
        synced = rt.status()["ready"]
        if not ok:
            blocked = why
        elif not synced:
            blocked = "still syncing"
        else:
            blocked = ""
        return JSONResponse({"ok": True, "would_fire": ok and synced,
                             "blocked_by": blocked})

    async def media(request):
        """Serve an attachment, fetching and caching it on first request."""
        mid = request.path_params["mid"]
        row = await rt.store.get_message(mid)
        if row is None or not row.media_meta:
            return PlainTextResponse("no media", status_code=404)

        cache = data_dir() / "media"
        cache.mkdir(parents=True, exist_ok=True)
        kind = (row.media_meta or {}).get("mime_type", "application/octet-stream")
        path = cache / mid.replace("/", "_")

        if not path.exists():
            try:
                blob = await rt.wa.download_media(mid)
            except Exception as exc:
                return PlainTextResponse(f"download failed: {exc}", status_code=502)
            if not blob:
                return PlainTextResponse("no media", status_code=404)
            path.write_bytes(blob)
            await rt.store.set_media_ref(mid, str(path))

        return Response(path.read_bytes(), media_type=kind,
                        headers={"Cache-Control": "private, max-age=86400"})

    async def avatar(request):
        """Profile picture for a chat, fetched once and cached on disk.

        A 204 is cached like a hit. Most chats have no picture, and asking
        WhatsApp again on every page load for an answer that will not change
        is both slow and the sort of chatter that draws rate limiting.
        """
        jid = request.path_params["jid"]
        cache = data_dir() / "avatars"
        cache.mkdir(parents=True, exist_ok=True)
        safe = jid.replace("/", "_")
        hit, miss = cache / f"{safe}.jpg", cache / f"{safe}.none"

        if miss.exists():
            return Response(status_code=204)
        if hit.exists():
            return Response(hit.read_bytes(), media_type="image/jpeg",
                            headers={"Cache-Control": "private, max-age=86400"})

        url = None
        try:
            if rt.wa is not None:
                url = await rt.wa.avatar_url(jid)
        except Exception as exc:
            log.debug("avatar lookup %s: %s", jid, exc)
        if not url:
            miss.write_bytes(b"")
            return Response(status_code=204)

        try:
            import httpx
            async with httpx.AsyncClient(timeout=15, follow_redirects=True) as c:
                r = await c.get(url)
            if r.status_code >= 400 or not r.content:
                miss.write_bytes(b"")
                return Response(status_code=204)
            hit.write_bytes(r.content)
        except Exception as exc:
            log.debug("avatar fetch %s: %s", jid, exc)
            return Response(status_code=204)

        return Response(hit.read_bytes(), media_type="image/jpeg",
                        headers={"Cache-Control": "private, max-age=86400"})

    async def qr_txt(request):
        qr = rt.wa.qr if rt.wa else None
        if not qr:
            return PlainTextResponse("no live code\n", status_code=404)
        return PlainTextResponse(qr, headers={"Cache-Control": "no-store"})

    async def events(request):
        """SSE. Pushes status on a timer plus anything the runtime emits."""
        q = rt.subscribe()

        async def stream():
            try:
                # Status first, before waiting on anything, so a freshly loaded
                # page paints the real state immediately instead of showing the
                # server-rendered "syncing" placeholder until the first tick.
                yield f"event: status\ndata: {json.dumps(rt.status())}\n\n"
                last_status = time.monotonic()
                while True:
                    try:
                        item = await asyncio.wait_for(q.get(), timeout=2.0)
                        yield f"event: wa\ndata: {json.dumps(item)}\n\n"
                        # Drain the rest of the burst. Receipts arrive in
                        # groups; emitting one per two-second tick would take
                        # half a minute to tick a dozen messages.
                        while not q.empty():
                            yield f"event: wa\ndata: {json.dumps(q.get_nowait())}\n\n"
                    except asyncio.TimeoutError:
                        pass
                    if time.monotonic() - last_status >= 2.0:
                        last_status = time.monotonic()
                        yield f"event: status\ndata: {json.dumps(rt.status())}\n\n"
            finally:
                rt.unsubscribe(q)

        # StreamingResponse, not Response. A plain Response tries to use the
        # generator as a body and 500s — which is what this endpoint did from
        # the day it was written, so no live update ever reached the browser
        # and the UI appeared to work only because of its 20-second poll.
        return StreamingResponse(stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-store",
                                          "X-Accel-Buffering": "no"})

    for route in [
        Route("/", index),
        Route("/connect", connect),
        Route("/api/chats", chats_api),
        Route("/api/messages/{jid}", messages_api),
        Route("/api/send", send_api, methods=["POST"]),
        Route("/api/read/{jid}", read_api, methods=["POST"]),
        Route("/api/status", status_api),
        Route("/api/flow/{flow}", flow_api),
        Route("/settings", settings_page),
        Route("/logout", sign_out),
        Route("/api/revoke-all", revoke_all, methods=["POST"]),
        Route("/api/settings", settings_api, methods=["POST"]),
        Route("/qr.txt", qr_txt),
        Route("/media/{mid}", media),
        Route("/avatar/{jid}", avatar),
        Route("/events", events),
    ]:
        app.router.routes.append(route)
