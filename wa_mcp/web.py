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
    <a href="/settings{_q(request)}" title="Settings" style="font-size:17px">&#9881;</a>
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
            if m.type in ("image", "sticker") and m.media_meta:
                cap = f"<div>{_esc(m.text)}</div>" if m.text else ""
                body = (f'<img src="/media/{_esc(m.message_id)}{_q(request)}" '
                        f'style="max-width:100%;border-radius:6px;display:block" '
                        f'loading="lazy" alt="">{cap}')
            elif m.text:
                body = _esc(m.text)
            else:
                body = f'<i style="opacity:.6">[{_esc(m.type)}]</i>'
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

    async def settings_page(request):
        st = rt.status()
        t = rt.trigger.settings
        ar = st["auto_reply"]
        ready = st["ready"]
        gate = "" if ready else (
            '<div class="warn">Still syncing. Settings save, but replies stay '
            'held until history finishes — otherwise the first thing this does '
            'is answer weeks of old messages at once.</div>')
        state = ('<span class="pill on">active</span>' if ar["active"]
                 else f'<span class="pill">idle — {_esc(ar["reason"] or "off")}</span>')
        chats = await rt.store.list_chats(limit=200)
        opts = "".join(
            f'<option value="{_esc(c.chat_jid)}">'
            f'{_esc(rt.contacts.display_name(c.chat_jid, chat_name=c.name))}</option>'
            for c in chats if not c.is_group)
        gopts = "".join(
            f'<option value="{_esc(c.chat_jid)}">'
            f'{_esc(rt.contacts.display_name(c.chat_jid, chat_name=c.name))}</option>'
            for c in chats if c.is_group)

        def sel(cur, *vals):
            return "".join(f'<option value="{v}"{" selected" if cur == v else ""}>{v}</option>'
                           for v in vals)

        return HTMLResponse(_page("Settings", f"""
<div class="set">
 <a href="/{_q(request)}" style="font-size:13px">&larr; chats</a>
 <h1>Auto-reply {state}</h1>
 <p style="color:#8696a0;font-size:13px">Replies are off until you turn them on,
    per scope. This sends from your real number — bulk or unsolicited messages
    can get it banned.</p>
 {gate}
 <form id="f">
  <h2>Backend</h2>
  <label>Enabled</label>
  <select name="enabled">{sel("yes" if t.enabled else "no", "no", "yes")}</select>
  <label>Reply using</label>
  <select name="backend">{sel(t.backend, "model", "webhook")}</select>

  <div id="model">
   <h2>Model — any OpenAI-compatible endpoint</h2>
   <label>Base URL</label>
   <input name="model.base_url" value="{_esc(t.model.base_url)}"
          placeholder="https://openrouter.ai/api/v1" list="presets">
   <datalist id="presets">
     <option value="https://openrouter.ai/api/v1">
     <option value="https://api.openai.com/v1">
     <option value="https://api.groq.com/openai/v1">
     <option value="https://api.together.xyz/v1">
     <option value="http://localhost:11434/v1">
     <option value="http://localhost:1234/v1">
   </datalist>
   <div class="two">
    <div><label>API key</label>
      <input name="model.api_key" type="password"
             value="{'***' if t.model.api_key else ''}" placeholder="sk-..."></div>
    <div><label>Model</label>
      <input name="model.model" value="{_esc(t.model.model)}"
             placeholder="anthropic/claude-sonnet-4.5"></div>
   </div>
   <label>System prompt</label>
   <textarea name="model.system_prompt">{_esc(t.model.system_prompt)}</textarea>
   <p style="color:#8696a0;font-size:12px">Tokens: {{{{me_name}}}} {{{{chat_name}}}}
      {{{{sender_name}}}} {{{{message}}}}</p>
  </div>

  <div id="webhook">
   <h2>Webhook</h2>
   <label>URL</label>
   <input name="webhook.url" value="{_esc(t.webhook.url)}" placeholder="https://...">
   <label>Body template</label>
   <textarea name="webhook.body">{_esc(t.webhook.body)}</textarea>
   <label>Reply path in the response</label>
   <input name="webhook.reply_path" value="{_esc(t.webhook.reply_path)}">
  </div>

  <h2>Guardrails</h2>
  <label>Answer only from this conversation</label>
  <select name="guardrails.context_only">
    {sel("yes" if t.guardrails.context_only else "no", "yes", "no")}</select>
  <p style="color:#8696a0;font-size:12px">On: the model works only from the chat
     history and says so when it does not know. Off without the flag below it
     will invent prices, dates and order numbers.</p>
  <label>Allow outside knowledge and search (explicit)</label>
  <select name="guardrails.allow_external_knowledge">
    {sel("yes" if t.guardrails.allow_external_knowledge else "no", "no", "yes")}</select>
  <p style="color:#8696a0;font-size:12px">Stated to the model in words when on.</p>

  <label>Only answer about these topics (comma separated)</label>
  <input name="guardrails.allowed_topics"
         value="{_esc(', '.join(t.guardrails.allowed_topics))}"
         placeholder="orders, delivery, opening hours">
  <label>Reject anything not mentioning one of them</label>
  <select name="guardrails.require_allowed_topic">
    {sel("yes" if t.guardrails.require_allowed_topic else "no", "no", "yes")}</select>
  <label>Never discuss (comma separated)</label>
  <input name="guardrails.blocked_topics"
         value="{_esc(', '.join(t.guardrails.blocked_topics))}"
         placeholder="pricing negotiation, legal advice">
  <label>Hard-blocked words — checked before the model runs</label>
  <input name="guardrails.blocked_keywords"
         value="{_esc(', '.join(t.guardrails.blocked_keywords))}"
         placeholder="refund, chargeback">
  <label>House rules added to the prompt</label>
  <textarea name="guardrails.policy_note"
            placeholder="Be brief and formal. Never promise a delivery date.">{_esc(t.guardrails.policy_note)}</textarea>
  <label>Default message when refusing</label>
  <input name="guardrails.fallback_message" value="{_esc(t.guardrails.fallback_message)}">

  <h2>Notify a human</h2>
  <label>Send alerts to this number (blank = the same chat)</label>
  <input name="notify.jid" value="{_esc(t.notify.jid)}"
         placeholder="919812345678 — e.g. the owner's personal phone">
  <div class="two">
   <div><label>When the assistant asks for a human</label>
     <select name="notify.on_handoff">
       {sel("yes" if t.notify.on_handoff else "no", "yes", "no")}</select></div>
   <div><label>When a guardrail refuses</label>
     <select name="notify.on_blocked">
       {sel("yes" if t.notify.on_blocked else "no", "no", "yes")}</select></div>
  </div>
  <label>Hand-off marker the model can emit</label>
  <input name="notify.handoff_marker" value="{_esc(t.notify.handoff_marker)}">

  <h2>Tell me when</h2>
  <p style="color:#8696a0;font-size:12px">These work even with auto-reply off —
     useful for watching a number without answering on it.</p>
  <label>Alert me when a message contains (comma separated)</label>
  <input name="notify.on_keywords" value="{_esc(', '.join(t.notify.on_keywords))}"
         placeholder="urgent, complaint, cancel">
  <label>Always alert me when these people message</label>
  <select name="notify.vip_contacts" multiple size="4">{opts}</select>
  <label>Never alert me about these</label>
  <select name="notify.mute_contacts" multiple size="4">{opts}</select>
  <label>Watch group chats too</label>
  <select name="notify.watch_groups">
    {sel("yes" if t.notify.watch_groups else "no", "no", "yes")}</select>

  <h2>Images</h2>
  <label>Download images the model produces and send them as photos</label>
  <select name="send_images">{sel("yes" if t.send_images else "no", "no", "yes")}</select>

  <h2>Who gets replies</h2>
  <label>Direct messages</label>
  <select name="reply.personal">{sel(t.reply.personal, "none", "all", "allowlist")}</select>
  <label>Only these people (ctrl-click for several)</label>
  <select name="reply.personal_allowlist" multiple size="4">{opts}</select>
  <label>Groups</label>
  <select name="reply.groups">{sel(t.reply.groups, "none", "all", "allowlist")}</select>
  <label>Only these groups</label>
  <select name="reply.groups_allowlist" multiple size="4">{gopts}</select>
  <label>In groups, only reply when mentioned</label>
  <select name="reply.require_mention_in_groups">
    {sel("yes" if t.reply.require_mention_in_groups else "no", "yes", "no")}</select>
  <div class="two">
   <div><label>Cooldown per chat (seconds)</label>
     <input name="reply.cooldown_seconds" type="number" value="{t.reply.cooldown_seconds}"></div>
   <div><label>Max replies per hour</label>
     <input name="reply.max_replies_per_hour" type="number"
            value="{t.reply.max_replies_per_hour}"></div>
  </div>

  <button type="submit">Save</button>
  <button type="button" class="ghost" onclick="test()">Test backend</button>
 </form>
 <div id="out" style="display:none"></div>
</div>
<script>
 const q = "{_q(request)}";
 function toggle() {{
   const b = document.querySelector('[name=backend]').value;
   document.getElementById('model').style.display   = b === 'model'   ? '' : 'none';
   document.getElementById('webhook').style.display = b === 'webhook' ? '' : 'none';
 }}
 document.querySelector('[name=backend]').onchange = toggle; toggle();

 function collect() {{
   const o = {{model:{{}}, webhook:{{}}, reply:{{}}}};
   for (const el of document.querySelectorAll('#f [name]')) {{
     let v = el.multiple ? [...el.selectedOptions].map(x=>x.value) : el.value;
     if (v === 'yes') v = true; if (v === 'no') v = false;
     if (el.type === 'number') v = parseInt(v||'0', 10);
     // Comma lists are friendlier to type than JSON arrays.
     if (['guardrails.allowed_topics','guardrails.blocked_topics',
          'guardrails.blocked_keywords','notify.on_keywords'].includes(el.name))
       v = String(v).split(',').map(x=>x.trim()).filter(Boolean);
     const p = el.name.split('.');
     if (p.length === 1) o[p[0]] = v; else o[p[0]][p[1]] = v;
   }}
   return o;
 }}
 const out = document.getElementById('out');
 function show(t, good) {{ out.style.display='block'; out.textContent=t;
   out.style.borderColor = good ? '#00a884' : '#f15c6d'; }}

 document.getElementById('f').onsubmit = async e => {{
   e.preventDefault();
   const r = await fetch('/api/settings'+q, {{method:'POST',
     headers:{{'Content-Type':'application/json'}}, body: JSON.stringify(collect())}});
   const d = await r.json();
   show(d.ok ? ('Saved. ' + (d.would_fire ? 'Replies are live.' :
        'Not firing: ' + (d.blocked_by||''))) : ('Error: '+d.error), d.ok);
 }};
 async function test() {{
   show('Testing…', true);
   const r = await fetch('/api/test-reply'+q, {{method:'POST'}});
   const d = await r.json();
   show(d.ok ? ('Backend replied:\n\n' + d.reply) : ('Failed:\n\n' + d.error), d.ok);
 }}
</script>"""))

    async def settings_api(request):
        raw = await request.json()
        from .trigger.settings import TriggerSettings
        cur = rt.trigger.settings
        if isinstance(raw.get("model"), dict) and raw["model"].get("api_key") in ("***", ""):
            raw["model"]["api_key"] = cur.model.api_key
        merged = TriggerSettings.from_dict(raw)
        await rt.trigger.save(merged)
        ok, why = merged.ready()
        return JSONResponse({"ok": True, "would_fire": ok and rt.status()["ready"],
                             "blocked_by": why})

    async def test_reply_api(request):
        from .trigger.backends import Context, reply_via_model, reply_via_webhook
        t = rt.trigger.settings
        ctx = Context(message="hello, are you there?", chat_name="Test",
                      chat_jid="test@s.whatsapp.net", sender_name="Test",
                      sender_jid="test@s.whatsapp.net",
                      me_name=getattr(rt.wa, "push_name", "") or "me",
                      message_id="test", timestamp="0",
                      history=[(False, "Test", "hello, are you there?")])
        try:
            reply = await (reply_via_model(t.model, ctx) if t.backend == "model"
                           else reply_via_webhook(t.webhook, ctx))
            return JSONResponse({"ok": True, "reply": reply})
        except Exception as exc:
            return JSONResponse({"ok": False, "error": str(exc)})

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
        Route("/api/flow/{flow}", flow_api),
        Route("/settings", settings_page),
        Route("/api/settings", settings_api, methods=["POST"]),
        Route("/api/test-reply", test_reply_api, methods=["POST"]),
        Route("/qr.txt", qr_txt),
        Route("/media/{mid}", media),
        Route("/events", events),
    ]:
        app.router.routes.append(route)
