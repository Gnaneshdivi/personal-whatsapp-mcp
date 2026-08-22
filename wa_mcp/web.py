"""The browser UI: chat list, conversation, pairing, live sync progress.

Server-rendered HTML and one EventSource. No build step, because a
`node_modules` in the install path of a `pip install` tool is the difference
between someone trying it and closing the tab.
"""
from __future__ import annotations

import asyncio
import html
import json
import logging

import segno

from .config import Settings
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
    place-items:center;font-weight:600;color:#8696a0;font-size:15px}
.meta{min-width:0;flex:1}
.nm{font-weight:500;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.pv{color:#8696a0;font-size:13px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.badge{background:#00a884;color:#111b21;border-radius:10px;padding:1px 7px;font-size:12px;
       font-weight:600;align-self:center}
.pane{display:flex;flex-direction:column;background:#0b141a}
.pane .hd{background:#202c33;border-bottom:1px solid #222d34}
.msgs{flex:1;overflow-y:auto;padding:18px;display:flex;flex-direction:column;gap:6px}
.m{max-width:min(75%,560px);padding:7px 11px;border-radius:8px;background:#202c33;
   align-self:flex-start;word-wrap:break-word}
.m.me{background:#005c4b;align-self:flex-end}
.m .t{font-size:11px;color:#8696a0;margin-top:3px;text-align:right}
.m .s{font-size:12px;color:#53bdeb;margin-bottom:2px}
.empty{margin:auto;color:#8696a0;text-align:center;padding:40px}
.qr{background:#fff;padding:14px;border-radius:12px;display:inline-block;line-height:0}
.qr svg{display:block;width:min(70vw,300px);height:auto}
code{background:#182229;padding:2px 5px;border-radius:4px;font-size:12px;word-break:break-all}
"""


def _esc(s) -> str:
    return html.escape(str(s or ""))


def _page(title: str, body: str) -> str:
    return (f'<!doctype html><meta charset="utf-8"><title>{_esc(title)}</title>'
            f'<meta name="viewport" content="width=device-width,initial-scale=1">'
            f"<style>{CSS}</style>{body}")


def mount_web(app, rt: Runtime, settings: Settings) -> None:
    from starlette.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response
    from starlette.routing import Route

    def _q(request) -> str:
        k = request.query_params.get("k", "")
        return f"?k={k}" if k else ""

    async def index(request):
        st = rt.status()
        if not st["number"] and st["phase"] in ("unpaired", "logged_out"):
            return Response(status_code=307,
                            headers={"location": f"/connect{_q(request)}"})

        chats = await rt.store.list_chats(limit=100)
        rows = []
        for c in chats:
            name = rt.contacts.display_name(c.chat_jid, chat_name=c.name)
            initial = _esc(name[:1].upper() or "?")
            badge = f'<span class="badge">{c.unread_count}</span>' if c.unread_count else ""
            rows.append(
                f'<div class="row" onclick="location=\'/c/{_esc(c.chat_jid)}{_q(request)}\'">'
                f'<div class="av">{initial}</div><div class="meta">'
                f'<div class="nm">{_esc(name)}</div>'
                f'<div class="pv">{_esc(c.last_message_text or "")}</div></div>{badge}</div>'
            )
        listing = "".join(rows) or '<div class="empty">No chats yet.<br>They appear as they sync.</div>'
        return HTMLResponse(_page("WhatsApp", f"""
<div class="wrap"><div class="list">
  <div class="hd"><span class="dot{'' if st['ready'] else ' warn'}"></span>
    <b>{_esc(st['push_name'] or 'WhatsApp')}</b>
    <span style="margin-left:auto;color:#8696a0;font-size:13px">{_esc(st['number'] or '')}</span>
  </div>
  <div class="sync" id="sync"></div>
  {listing}
</div><div class="pane"><div class="empty">Select a chat</div></div></div>
<script>
 const es = new EventSource("/events{_q(request)}");
 const box = document.getElementById("sync");
 function render(s) {{
   if (!s) return;
   if (s.ready) {{ box.style.display = "none"; return; }}
   box.style.display = "block";
   box.innerHTML = "Syncing " + (s.detail||"") +
     '<div class="bar"><i style="width:' + (s.percent||0) + '%"></i></div>';
 }}
 es.addEventListener("status", e => render(JSON.parse(e.data).sync));
 // Reload once sync finishes so the chat list is not left half-populated.
 let wasReady = {str(bool(st['ready'])).lower()};
 es.addEventListener("status", e => {{
   const r = JSON.parse(e.data).ready;
   if (r && !wasReady) location.reload();
   wasReady = r;
 }});
 fetch("/api/status{_q(request)}").then(r=>r.json()).then(d=>render(d.sync));
</script>"""))

    async def chat(request):
        jid = request.path_params["jid"]
        msgs = list(reversed(await rt.store.get_messages(jid, limit=60)))
        c = await rt.store.get_chat(jid)
        name = rt.contacts.display_name(jid, chat_name=c.name if c else None)
        out = []
        for m in msgs:
            who = ""
            if not m.is_from_me and c and c.is_group:
                who = (f'<div class="s">'
                       f'{_esc(rt.contacts.display_name(m.sender_jid or "", push_name=m.sender_name))}'
                       f"</div>")
            body = _esc(m.text) if m.text else f'<i style="opacity:.6">[{_esc(m.type)}]</i>'
            when = (m.public()["timestamp"] or "")[11:16]
            out.append(f'<div class="m{" me" if m.is_from_me else ""}">{who}{body}'
                       f'<div class="t">{when}</div></div>')
        return HTMLResponse(_page(name, f"""
<div class="wrap open"><div class="list"></div><div class="pane">
  <div class="hd"><a href="/{_q(request)}" style="font-size:20px">&larr;</a>
    <b>{_esc(name)}</b></div>
  <div class="msgs" id="m">{"".join(out) or '<div class="empty">No messages</div>'}</div>
</div></div>
<script>const m=document.getElementById("m");m.scrollTop=m.scrollHeight;</script>"""))

    async def connect(request):
        st = rt.status()
        if st["number"]:
            return HTMLResponse(_page("Connected", f"""
<div style="display:grid;place-items:center;height:100vh;text-align:center">
 <div><h2 style="color:#00a884">&#10003; Connected</h2>
 <p style="font-family:monospace">{_esc(st['number'])}</p>
 <p><a href="/{_q(request)}">Open chats</a></p></div></div>"""))

        if rt.wa is not None and not rt.wa.qr:
            try:
                await rt.wa.pair()
            except Exception as exc:
                log.debug("pair: %s", exc)
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

        svg = segno.make_qr(qr).svg_inline(scale=8, border=3, dark="#000", light="#fff")
        svg = svg if isinstance(svg, str) else svg.decode()
        return HTMLResponse(_page("Link WhatsApp", f"""
<div style="display:grid;place-items:center;height:100vh;padding:20px">
 <div style="text-align:center;max-width:420px">
  <h2 style="font-size:17px">Link your WhatsApp</h2>
  <div class="qr">{svg}</div>
  <p style="color:#8696a0;font-size:13px">
    WhatsApp &rarr; Settings &rarr; Linked devices &rarr; Link a device</p>
  <details style="text-align:left;margin-top:14px">
   <summary style="color:#53bdeb;font-size:13px;cursor:pointer">Copy the code as text</summary>
   <p><code>{_esc(qr)}</code></p>
   <p style="color:#8696a0;font-size:12px">Expires ~20s after it appeared.
      This page refreshes itself.</p>
  </details>
 </div></div>
<script>
 // Pause the reload while the copy panel is open, or it clears the selection.
 setInterval(() => {{ if(!document.querySelector("details[open]")) location.reload(); }}, 4000);
</script>"""))

    async def status_api(request):
        return JSONResponse(rt.status())

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
                while True:
                    yield f"event: status\ndata: {json.dumps(rt.status())}\n\n"
                    try:
                        item = await asyncio.wait_for(q.get(), timeout=2.0)
                        yield f"event: wa\ndata: {json.dumps(item)}\n\n"
                    except asyncio.TimeoutError:
                        pass
            finally:
                rt.unsubscribe(q)

        return Response(stream(), media_type="text/event-stream",
                        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})

    for route in [
        Route("/", index),
        Route("/c/{jid}", chat),
        Route("/connect", connect),
        Route("/api/status", status_api),
        Route("/qr.txt", qr_txt),
        Route("/events", events),
    ]:
        app.router.routes.append(route)
