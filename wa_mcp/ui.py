"""The chat client: two panes, live updates, and history that pages as you scroll.

Server-rendered shell plus vanilla JS against the JSON endpoints. No build step,
because a `node_modules` in the install path of a `pip install` tool is the
difference between someone trying it and closing the tab.

Three things drive the layout, and each is a correctness concern rather than a
cosmetic one:

**Nothing outside the message list scrolls.** The panes are a fixed grid and
only `.msgs` has `overflow-y`. Letting the page scroll means the composer and
the chat list drift off-screen while you read history, which is what makes a
long conversation feel broken.

**Paging up preserves scroll position.** Older messages are prepended, and the
scroll offset is restored by measuring `scrollHeight` before and after — without
that the viewport jumps to the top the instant a page loads, and you lose the
message you were reading.

**Live messages only auto-scroll if you were already at the bottom.** Yanking
someone down to a new message while they are reading last week is the single
most irritating thing a chat UI can do.
"""
from __future__ import annotations

CSS = """
*{box-sizing:border-box}
html,body{height:100%;margin:0;overflow:hidden}
body{font:15px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
     background:#0b141a;color:#e9edef}
a{color:#53bdeb;text-decoration:none}
button{font:inherit;cursor:pointer}

.app{display:grid;grid-template-columns:minmax(300px,380px) 1fr;height:100vh}
@media(max-width:820px){
  .app{grid-template-columns:1fr}
  .pane{display:none}
  body.open .list{display:none}
  body.open .pane{display:grid}
}

/* ---------------- left: chat list ---------------- */
.list{display:grid;grid-template-rows:auto auto auto 1fr;min-height:0;
      background:#111b21;border-right:1px solid #222d34}
.lhead{display:flex;align-items:center;gap:10px;padding:16px 16px 10px}
.lhead h1{font-size:19px;margin:0;font-weight:600}
.me{margin-left:auto;font-size:12px;color:#8696a0;text-align:right;line-height:1.3}
.dot{width:8px;height:8px;border-radius:50%;background:#00a884;display:inline-block}
.dot.warn{background:#f0b232}
.search{padding:0 12px 8px}
.search input{width:100%;background:#202c33;border:0;border-radius:8px;
  padding:9px 13px;color:#e9edef;font:inherit;font-size:14px;outline:none}
.filters{display:flex;gap:7px;padding:0 12px 10px;overflow-x:auto;scrollbar-width:none}
.filters::-webkit-scrollbar{display:none}
.chip{background:#202c33;border:0;color:#8696a0;border-radius:14px;
      padding:5px 13px;font-size:13px;white-space:nowrap}
.chip.on{background:#0a3d34;color:#00d9a5}
.rows{overflow-y:auto;min-height:0;overscroll-behavior:contain}

.row{display:flex;gap:12px;padding:10px 14px;cursor:pointer;align-items:center;
     border-bottom:1px solid #16232b}
.row:hover{background:#182229}
.row.sel{background:#2a3942}
.av{width:44px;height:44px;border-radius:50%;background:#2a3942;flex:none;
    display:grid;place-items:center;font-weight:600;color:#8696a0;font-size:16px;
    position:relative;overflow:hidden}
.av img{position:absolute;inset:0;width:100%;height:100%;object-fit:cover}
.mid{min-width:0;flex:1}
.top{display:flex;align-items:baseline;gap:8px}
.nm{font-weight:500;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;flex:1}
.when{font-size:12px;color:#8696a0;flex:none}
.bot{display:flex;align-items:center;gap:6px;margin-top:2px}
.pv{color:#8696a0;font-size:13.5px;white-space:nowrap;overflow:hidden;
    text-overflow:ellipsis;flex:1}
.badge{background:#00a884;color:#111b21;border-radius:11px;min-width:20px;
       text-align:center;padding:1px 6px;font-size:12px;font-weight:600;flex:none}
.pin{font-size:12px;opacity:.6;flex:none}

/* ---------------- right: conversation ---------------- */
.pane{display:grid;grid-template-rows:auto 1fr auto;min-height:0;background:#0b141a}
.phead{display:flex;align-items:center;gap:12px;padding:11px 16px;
       background:#202c33;border-bottom:1px solid #222d34}
.phead .nm{font-size:16px}
.back{display:none;font-size:22px;color:#e9edef;background:none;border:0;padding:0 4px}
@media(max-width:820px){.back{display:block}}

.msgs{overflow-y:auto;min-height:0;padding:16px 18px;display:flex;
      flex-direction:column;gap:3px;overscroll-behavior:contain}
.more{align-self:center;background:#202c33;border:0;color:#8696a0;border-radius:14px;
      padding:6px 16px;font-size:13px;margin-bottom:10px}
.m{max-width:min(72%,620px);padding:6px 10px 5px;border-radius:8px;background:#202c33;
   align-self:flex-start;word-wrap:break-word;white-space:pre-wrap;position:relative}
.m.me{background:#005c4b;align-self:flex-end}
.m+.m{margin-top:1px}
.m.first{margin-top:9px}
.m .s{font-size:12.5px;color:#53bdeb;margin-bottom:2px;font-weight:500}
.m .t{font-size:11px;color:#8696a0;float:right;margin:6px 0 0 10px;line-height:1}
.m img{max-width:100%;border-radius:6px;display:block;margin-bottom:4px}
.day{align-self:center;background:#182229;color:#8696a0;font-size:12px;
     padding:4px 12px;border-radius:10px;margin:12px 0 6px}
.sys{align-self:center;color:#8696a0;font-size:12.5px;margin:10px 0}

.comp{display:flex;gap:10px;padding:11px 16px;background:#202c33;align-items:flex-end}
.comp textarea{flex:1;background:#2a3942;border:0;border-radius:9px;color:#e9edef;
  font:inherit;padding:10px 13px;resize:none;max-height:120px;outline:none}
.comp button{background:#00a884;border:0;color:#0b141a;border-radius:50%;
  width:40px;height:40px;font-size:17px;flex:none}
.comp button:disabled{opacity:.45}

.blank{display:grid;place-items:center;height:100%;color:#8696a0;text-align:center}
.sync{padding:9px 14px;background:#182229;font-size:13px;color:#8696a0;
      border-bottom:1px solid #222d34}
.bar{height:3px;background:#222d34;border-radius:2px;margin-top:7px;overflow:hidden}
.bar i{display:block;height:100%;background:#00a884;transition:width .4s}
"""

JS = r"""
const qs = new URLSearchParams(location.search);
const K  = qs.get("k") ? "?k=" + encodeURIComponent(qs.get("k")) : "";
const KA = (u) => u + (K ? (u.includes("?") ? "&" + K.slice(1) : K) : "");

let chats = [], current = null, oldest = null, hasMore = false, loading = false;
let filter = "all", query = "";

const $ = (s) => document.querySelector(s);
const esc = (t) => { const d = document.createElement("div"); d.textContent = t ?? ""; return d.innerHTML; };

function when(iso) {
  if (!iso) return "";
  const d = new Date(iso), now = new Date();
  const days = Math.floor((now.setHours(0,0,0,0) - new Date(iso).setHours(0,0,0,0)) / 864e5);
  if (days === 0) return d.toLocaleTimeString([], {hour:"2-digit", minute:"2-digit"});
  if (days === 1) return "Yesterday";
  if (days < 7)  return d.toLocaleDateString([], {weekday:"long"});
  return d.toLocaleDateString([], {day:"2-digit", month:"2-digit", year:"2-digit"});
}
const dayKey = (iso) => new Date(iso).toDateString();

/* ------------------------------------------------------------ chat list */

async function loadChats() {
  const u = `/api/chats?limit=80&filter=${filter}` + (query ? `&q=${encodeURIComponent(query)}` : "");
  chats = (await (await fetch(KA(u))).json()).chats;
  renderChats();
}

function renderChats() {
  $("#rows").innerHTML = chats.map(c => `
    <div class="row ${c.chat_jid === current ? "sel" : ""}" data-jid="${esc(c.chat_jid)}">
      <div class="av">${esc((c.name||"?")[0].toUpperCase())}
        <img src="${KA("/avatar/" + encodeURIComponent(c.chat_jid))}" alt=""
             loading="lazy" onerror="this.remove()"></div>
      <div class="mid">
        <div class="top"><span class="nm">${esc(c.name)}</span>
          <span class="when">${when(c.last_message_at)}</span></div>
        <div class="bot"><span class="pv">${esc(c.last_message || "")}</span>
          ${c.pinned ? '<span class="pin">📌</span>' : ""}
          ${c.unread ? `<span class="badge">${c.unread}</span>` : ""}</div>
      </div></div>`).join("")
    || '<div class="blank" style="padding:40px">No chats</div>';
  document.querySelectorAll(".row").forEach(r =>
    r.onclick = () => openChat(r.dataset.jid));
}

/* --------------------------------------------------------- conversation */

async function openChat(jid) {
  current = jid; oldest = null;
  document.body.classList.add("open");
  renderChats();
  const d = await (await fetch(KA(`/api/messages/${encodeURIComponent(jid)}?limit=40`))).json();
  hasMore = d.has_more;
  $("#pname").textContent = d.chat.name;
  $("#pav").innerHTML = `${esc((d.chat.name||"?")[0].toUpperCase())}
    <img src="${KA("/avatar/" + encodeURIComponent(jid))}" alt="" onerror="this.remove()">`;
  $("#pane").style.display = "";
  $("#blank").style.display = "none";
  $("#msgs").innerHTML = "";
  paint(d.messages, false);
  $("#msgs").insertAdjacentHTML("afterbegin", hasMore
    ? '<button class="more" id="more">Load older messages</button>'
    : '<div class="sys" id="more">Start of the history WhatsApp sent for this chat</div>');
  const b0 = $("#more"); if (b0 && b0.tagName === "BUTTON") b0.onclick = loadOlder;
  $("#msgs").scrollTop = $("#msgs").scrollHeight;   // newest in view
  fetch(KA(`/api/read/${encodeURIComponent(jid)}`), {method:"POST"}).then(loadChats);
}

function bubble(m, prev) {
  const newDay = !prev || dayKey(prev.timestamp) !== dayKey(m.timestamp);
  const sameSpeaker = prev && prev.from_me === m.from_me &&
                      prev.sender_name === m.sender_name && !newDay;
  const day = newDay
    ? `<div class="day">${new Date(m.timestamp).toLocaleDateString([],
        {day:"numeric", month:"short", year:"numeric"})}</div>` : "";
  const who = (!m.from_me && m.sender_name && !sameSpeaker)
    ? `<div class="s">${esc(m.sender_name)}</div>` : "";
  const img = (m.has_media && ["image","sticker"].includes(m.type))
    ? `<img src="${KA("/media/" + encodeURIComponent(m.message_id))}" alt="" loading="lazy"
            onerror="this.remove()">` : "";
  const body = m.revoked
    ? '<i style="opacity:.6">This message was deleted</i>'
    : (m.text ? esc(m.text) : (img ? "" : `<i style="opacity:.6">[${esc(m.type)}]</i>`));
  const t = new Date(m.timestamp).toLocaleTimeString([], {hour:"2-digit", minute:"2-digit"});
  return `${day}<div class="m ${m.from_me ? "me" : ""} ${sameSpeaker ? "" : "first"}"
      data-id="${esc(m.message_id)}">${who}${img}${body}<span class="t">${t}${
      m.edited ? " ·edited" : ""}</span></div>`;
}

function paint(list, prepend) {
  const box = $("#msgs");
  let html = "";
  list.forEach((m, i) => html += bubble(m, list[i-1]));
  if (prepend) {
    // Preserve the reading position: measure before, restore after.
    const before = box.scrollHeight, top = box.scrollTop;
    // Say when history ends. Scrolling that just stops reads as a broken app;
    // WhatsApp only sends part of each conversation at pair time and there is
    // no way to ask for more, so the boundary has to be visible.
    const head = hasMore
      ? '<button class="more" id="more">Load older messages</button>'
      : '<div class="sys" id="more">Start of the history WhatsApp sent for this chat</div>';
    box.insertAdjacentHTML("afterbegin", head + html);
    box.scrollTop = top + (box.scrollHeight - before);
  } else {
    box.insertAdjacentHTML("beforeend", html);
  }
  if (list.length) oldest = oldest || list[0].message_id;
  if (prepend && list.length) oldest = list[0].message_id;
  const btn = $("#more");
  if (btn && btn.tagName === "BUTTON") btn.onclick = loadOlder;
}

async function loadOlder() {
  if (loading || !hasMore || !oldest) return;
  loading = true;
  const btn = $("#more"); if (btn) btn.remove();
  const d = await (await fetch(KA(
    `/api/messages/${encodeURIComponent(current)}?limit=40&before=${encodeURIComponent(oldest)}`))).json();
  hasMore = d.has_more;
  paint(d.messages, true);
  loading = false;
}

/* ------------------------------------------------------------- live */

const es = new EventSource(KA("/events"));
es.addEventListener("status", e => {
  const s = JSON.parse(e.data);
  $("#num").textContent = s.number || "";
  $("#pn").textContent = s.push_name || "WhatsApp";
  $("#live").className = "dot" + (s.ready ? "" : " warn");
  const sy = $("#sync");
  if (s.ready) { sy.style.display = "none"; }
  else {
    sy.style.display = "block";
    sy.innerHTML = "Syncing " + (s.sync.detail || "") +
      `<div class="bar"><i style="width:${s.sync.percent||0}%"></i></div>`;
  }
});
es.addEventListener("wa", async e => {
  const ev = JSON.parse(e.data);
  if (!["message.received","message.sent"].includes(ev.type)) return;
  loadChats();
  if (ev.chat_jid !== current) return;
  const box = $("#msgs");
  // Only follow the conversation if they were already at the bottom. Yanking
  // someone down while they read last week is the worst thing a chat UI does.
  const atBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 80;
  const d = await (await fetch(KA(`/api/messages/${encodeURIComponent(current)}?limit=8`))).json();
  const have = new Set([...box.querySelectorAll(".m")].map(n => n.dataset.id));
  const fresh = d.messages.filter(m => !have.has(m.message_id));
  if (fresh.length) paint(fresh, false);
  if (atBottom) box.scrollTop = box.scrollHeight;
});

/* ------------------------------------------------------------ composer */

async function send() {
  const ta = $("#ta"), text = ta.value.trim();
  if (!text || !current) return;
  ta.value = ""; ta.style.height = "auto";
  const r = await (await fetch(KA("/api/send"), {
    method:"POST", headers:{"Content-Type":"application/json"},
    body: JSON.stringify({to: current, text})})).json();
  if (!r.ok) { alert("Not sent: " + r.error); ta.value = text; return; }
  const d = await (await fetch(KA(`/api/messages/${encodeURIComponent(current)}?limit=5`))).json();
  const have = new Set([...$("#msgs").querySelectorAll(".m")].map(n => n.dataset.id));
  paint(d.messages.filter(m => !have.has(m.message_id)), false);
  $("#msgs").scrollTop = $("#msgs").scrollHeight;
  loadChats();
}

/* --------------------------------------------------------------- wire */

$("#msgs").addEventListener("scroll", () => {
  if ($("#msgs").scrollTop < 120) loadOlder();
});
$("#q").addEventListener("input", e => {
  query = e.target.value.trim();
  clearTimeout(window._t); window._t = setTimeout(loadChats, 200);
});
document.querySelectorAll(".chip").forEach(c => c.onclick = () => {
  document.querySelectorAll(".chip").forEach(x => x.classList.remove("on"));
  c.classList.add("on"); filter = c.dataset.f; loadChats();
});
$("#send").onclick = send;
$("#ta").addEventListener("keydown", e => {
  if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); }
});
$("#ta").addEventListener("input", e => {
  e.target.style.height = "auto";
  e.target.style.height = Math.min(e.target.scrollHeight, 120) + "px";
});
$("#back").onclick = () => document.body.classList.remove("open");

loadChats();
setInterval(loadChats, 20000);   // catches read state changed on the phone
"""


def shell(q: str) -> str:
    return f"""<div class="app">
  <div class="list">
    <div class="lhead">
      <span class="dot" id="live"></span>
      <h1>Chats</h1>
      <div class="me"><div id="pn">WhatsApp</div><div id="num"></div></div>
    </div>
    <div class="search"><input id="q" placeholder="Search chats" autocomplete="off"></div>
    <div class="filters">
      <button class="chip on" data-f="all">All</button>
      <button class="chip" data-f="unread">Unread</button>
      <button class="chip" data-f="groups">Groups</button>
    </div>
    <div class="rows" id="rows"></div>
  </div>

  <div class="pane" id="pane" style="display:none">
    <div class="phead">
      <button class="back" id="back">&larr;</button>
      <div class="av" id="pav"></div>
      <span class="nm" id="pname"></span>
    </div>
    <div class="msgs" id="msgs"></div>
    <div class="comp">
      <textarea id="ta" rows="1" placeholder="Type a message"></textarea>
      <button id="send">&#10148;</button>
    </div>
  </div>
  <div class="blank" id="blank">Select a chat to start reading</div>
</div>
<div class="sync" id="sync" style="display:none;position:fixed;bottom:0;left:0;
     right:0;z-index:5"></div>
<script>{JS}</script>"""
