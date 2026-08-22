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

ICONS = {
    # Stroked 24x24 paths, currentColor. Emoji render differently on every
    # platform and cannot be recoloured for an active state.
    "chats": '<path d="M21 11.5a8.4 8.4 0 0 1-9 8.4 8.5 8.5 0 0 1-3.8-.9L3 21l1.9-5.2A8.4 8.4 0 0 1 12 3a8.4 8.4 0 0 1 9 8.5z"/>',
    "settings": '<circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.9l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-2.9 1.2 2 2 0 1 1-4 0 1.7 1.7 0 0 0-2.9-1.2l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1A1.7 1.7 0 0 0 3 15a2 2 0 1 1 0-4 1.7 1.7 0 0 0 1.2-2.9l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1A1.7 1.7 0 0 0 9 4.6a2 2 0 1 1 4 0 1.7 1.7 0 0 0 2.9 1.2l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1A1.7 1.7 0 0 0 21 11a2 2 0 1 1 0 4z"/>',
    "tick": '<path d="M2 12.5 7 17l4-4"/>',
    "ticks": '<path d="M1 12.5 5.5 17 14 8"/><path d="M8 12.5 12.5 17 21 8"/>',
}


def icon(name: str, size: int = 21) -> str:
    return (f'<svg viewBox="0 0 24 24" width="{size}" height="{size}" fill="none" '
            f'stroke="currentColor" stroke-width="1.8" stroke-linecap="round" '
            f'stroke-linejoin="round">{ICONS[name]}</svg>')


CSS = """
*{box-sizing:border-box}
html,body{height:100%;margin:0;overflow:hidden}
body{font:15px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
     background:#0b141a;color:#e9edef}
a{color:#53bdeb;text-decoration:none}
button{font:inherit;cursor:pointer}

.app{display:grid;grid-template-columns:60px minmax(300px,380px) 1fr;height:100vh}

/* far-left icon rail */
.rail{background:#111b21;border-right:1px solid #222d34;display:flex;
      flex-direction:column;align-items:center;padding:12px 0;gap:4px}
.rail .sp{flex:1}
.rail button{width:42px;height:42px;border:0;background:none;border-radius:50%;
  color:#8696a0;font-size:19px;display:grid;place-items:center;position:relative}
.rail button:hover{background:#202c33;color:#e9edef}
.rail button.on{background:#2a3942;color:#00d9a5}
.rail .me{width:34px;height:34px;border-radius:50%;overflow:hidden;background:#2a3942;
  display:grid;place-items:center;font-size:14px;font-weight:600;color:#8696a0;
  cursor:pointer;position:relative}
.rail .me img{position:absolute;inset:0;width:100%;height:100%;object-fit:cover}
@media(max-width:820px){
  .app{grid-template-columns:52px 1fr}
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
.pin{flex:none;display:inline-flex;color:#8696a0;opacity:.7}
.ptk{flex:none;display:inline-flex;margin-right:-2px}
.sec{padding:12px 16px 6px;font-size:12px;text-transform:uppercase;
     letter-spacing:.08em;color:#00a884;font-weight:600}
.row.hit{padding-left:16px}
.row.hit .pv{white-space:normal;display:-webkit-box;-webkit-line-clamp:2;
             -webkit-box-orient:vertical;overflow:hidden}

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
.m.me{background:#005c4b;align-self:flex-end;color:#fff}
.m+.m{margin-top:1px}
.m.first{margin-top:9px}
.m .s{font-size:12.5px;color:#53bdeb;margin-bottom:2px;font-weight:500}
.m .t{font-size:11px;color:#8696a0;float:right;margin:6px 0 0 10px;line-height:1;
      display:inline-flex;align-items:center;gap:3px}
.tk{display:inline-flex;color:#8696a0}
.tk.rd{color:#53bdeb}
.m img{max-width:100%;border-radius:6px;display:block;margin-bottom:4px}
.m a{color:#53bdeb;text-decoration:underline}
.m.me a{color:#a7f3e4}
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
.panel{overflow-y:auto;padding:26px 24px;min-height:0}
.panel h2{font-size:17px;margin:0 0 4px}
.panel .sub{color:#8696a0;font-size:13px;margin-bottom:22px}
.card{background:#111b21;border:1px solid #222d34;border-radius:10px;
      padding:16px 18px;margin-bottom:12px}
.card h3{font-size:13px;text-transform:uppercase;letter-spacing:.07em;
         color:#8696a0;margin:0 0 12px;font-weight:600}
.kv{display:flex;justify-content:space-between;gap:16px;padding:7px 0;
    border-bottom:1px solid #1b262d;font-size:14px}
.kv:last-child{border-bottom:0}
.kv span:last-child{color:#8696a0;text-align:right;word-break:break-all}
.bigav{width:96px;height:96px;border-radius:50%;background:#2a3942;margin:0 auto 14px;
  display:grid;place-items:center;font-size:34px;font-weight:600;color:#8696a0;
  position:relative;overflow:hidden}
.bigav img{position:absolute;inset:0;width:100%;height:100%;object-fit:cover}
.panel a.btn{display:inline-block;background:#00a884;color:#0b141a;border-radius:8px;
  padding:9px 18px;font-weight:600;margin-top:6px}
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
let currentIsGroup = false;
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

// viewBox matches the rendered aspect ratio, or the glyph is squashed. The
// earlier version put a checkmark in the corner of a square box and drew it
// non-uniformly, which came out looking like a small chevron.
const TICK_ONE = `<svg viewBox="0 0 16 12" width="14" height="10.5" fill="none"
  stroke="currentColor" stroke-width="1.9" stroke-linecap="round"
  stroke-linejoin="round"><path d="M2 6.5 5.5 10 14 2"/></svg>`;
const TICK_TWO = `<svg viewBox="0 0 22 12" width="17" height="10.5" fill="none"
  stroke="currentColor" stroke-width="1.9" stroke-linecap="round"
  stroke-linejoin="round"><path d="M1 6.5 4.5 10 13 2"/><path d="M8 6.5 11.5 10 20 2"/></svg>`;

// sent = one tick, delivered = two, read/played = two in blue. The colour is
// the signal an agent acts on, so it is worth getting exactly right.
const PIN = `<svg viewBox="0 0 24 24" width="12" height="12" fill="currentColor" aria-hidden="true">`
  + `<path d="M14 2 22 10l-2.1 2.1-1.4-.5-3.6 3.6.5 3.4-1.8 1.8-4-4L4 22l-.7-.7 5.6-5.6-4-4L6.7 9.9l3.4.5 3.6-3.6-.5-1.4L14 2z"/></svg>`;

function ticks(status) {
  if (!status) return "";
  const blue = (status === "read" || status === "played");
  const mark = status === "sent" ? TICK_ONE : TICK_TWO;
  return `<span class="tk${blue ? " rd" : ""}" data-s="${status}">${mark}</span>`;
}

/* ------------------------------------------------------------ chat list */

let hits = [];
async function loadChats() {
  const u = `/api/chats?limit=80&filter=${filter}` + (query ? `&q=${encodeURIComponent(query)}` : "");
  const d = await (await fetch(KA(u))).json();
  chats = d.chats; hits = d.messages || [];
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
        <div class="bot">${c.last_from_me ? `<span class="ptk">${ticks(c.last_status)}</span>` : ""}<span class="pv">${esc(c.last_message || "")}</span>
          ${c.pinned ? `<span class="pin">${PIN}</span>` : ""}
          ${c.unread ? `<span class="badge">${c.unread}</span>` : ""}</div>
      </div></div>`).join("")
    || (query ? "" : '<div class="blank" style="padding:40px">No chats</div>');

  // Searching answers with two lists, like WhatsApp: who, then where it was said.
  if (query) {
    $("#rows").insertAdjacentHTML("afterbegin",
      chats.length ? '<div class="sec">Chats</div>' : '<div class="sec">No matching chats</div>');
    if (hits.length) {
      $("#rows").insertAdjacentHTML("beforeend",
        '<div class="sec">Messages</div>' + hits.map(m => `
          <div class="row hit" data-jid="${esc(m.chat_jid)}">
            <div class="mid">
              <div class="top"><span class="nm">${esc(m.chat_name)}</span>
                <span class="when">${when(m.timestamp)}</span></div>
              <div class="bot"><span class="pv">${esc(m.sender_display)}: ${
                esc((m.text||"").slice(0,90))}</span></div>
            </div></div>`).join(""));
    } else {
      $("#rows").insertAdjacentHTML("beforeend",
        '<div class="sec">No messages found</div>');
    }
  }
  document.querySelectorAll(".row").forEach(r =>
    r.onclick = () => openChat(r.dataset.jid));
}

/* --------------------------------------------------------- conversation */

/* Message text arrives escaped. Turning URLs and chat addresses into links
   after escaping keeps the injection guarantee: nothing here reads the raw
   message, so a message containing markup still renders as text.

   Addresses matter because our own alerts quote one -- "Needs you: … (918…
   @s.whatsapp.net)" -- and reading it meant copying the number by hand to
   find the conversation it is about. */
const URL_RE = /https?:\/\/[^\s<]+[^\s<.,;:!?)\]}'"]/g;
const JID_RE = /\b(\d{6,})@s\.whatsapp\.net\b/g;

function linkify(escaped) {
  return escaped
    .replace(URL_RE, u => `<a href="${u}" target="_blank" rel="noopener noreferrer">${u}</a>`)
    .replace(JID_RE, (full, num) =>
      `<a href="#" class="jl" data-jid="${full}" title="Open this chat">${num}</a>`);
}

document.addEventListener("click", e => {
  const a = e.target.closest("a.jl");
  if (!a) return;
  e.preventDefault();
  openChat(a.dataset.jid);
});

async function openChat(jid) {
  current = jid; oldest = null;
  $("#profile").style.display = "none";
  $("#nav-chats").classList.add("on");
  document.body.classList.add("open");
  renderChats();
  const d = await (await fetch(KA(`/api/messages/${encodeURIComponent(jid)}?limit=40`))).json();
  hasMore = d.has_more;
  currentIsGroup = !!d.chat.is_group;
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
  const who = (currentIsGroup && !m.from_me && m.sender_name && !sameSpeaker)
    ? `<div class="s">${esc(m.sender_name)}</div>` : "";
  const img = (m.has_media && ["image","sticker"].includes(m.type))
    ? `<img src="${KA("/media/" + encodeURIComponent(m.message_id))}" alt="" loading="lazy"
            onerror="this.remove()">` : "";
  const body = m.revoked
    ? '<i style="opacity:.6">This message was deleted</i>'
    : (m.text ? linkify(esc(m.text))
              : (img ? "" : `<i style="opacity:.6">[${esc(m.type)}]</i>`));
  const t = new Date(m.timestamp).toLocaleTimeString([], {hour:"2-digit", minute:"2-digit"});
  const tick = m.from_me ? ticks(m.status) : "";
  return `${day}<div class="m ${m.from_me ? "me" : ""} ${sameSpeaker ? "" : "first"}"
      data-id="${esc(m.message_id)}" data-status="${esc(m.status||"")}">${who}${img}${body}<span class="t">${t}${
      m.edited ? " ·edited" : ""}${tick}</span></div>`;
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
function applyStatus(s) {
  $("#num").textContent = s.number || "";
  $("#pn").textContent = s.push_name || "WhatsApp";
  const me = $("#nav-profile");
  if (me && !me.dataset.set && s.number) { me.dataset.set = "1"; paintMe(); }
  $("#live").className = "dot" + (s.ready ? "" : " warn");
  const sy = $("#sync");
  // Hide it once the gate opens, and also whenever there is already a full
  // store to read. WhatsApp only ships history at pair time, so on every
  // restart after the first there is nothing to wait for -- the chats below
  // are complete while this bar claims otherwise.
  if (s.ready || s.have_local_history) { sy.style.display = "none"; }
  else {
    sy.style.display = "block";
    sy.innerHTML = "Syncing" + (s.sync.detail ? " \u2014 " + esc(s.sync.detail) : "") +
      `<div class="bar"><i style="width:${s.sync.percent||0}%"></i></div>`;
  }
}
es.addEventListener("status", e => applyStatus(JSON.parse(e.data)));

/* The stream is the fast path, not the only path. EventSource gives up
   silently often enough — a proxy idle-timeout, a laptop waking — and when it
   does, every one of these is driven by the poll below instead. The banner
   sticking on "Syncing" forever after the stream dropped is exactly the bug
   this guards against. */
async function refreshStatus() {
  try { applyStatus(await (await fetch(KA("/api/status"))).json()); } catch (e) {}
}

/* The sidebar is not the conversation. Reloading only the chat list is why a
   new message showed up on the left while the open thread sat unchanged. */
async function refreshOpen() {
  if (!current) return;
  const box = $("#msgs");
  const atBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 80;
  let d;
  try {
    d = await (await fetch(KA(`/api/messages/${encodeURIComponent(current)}?limit=20`))).json();
  } catch (e) { return; }
  const msgs = d.messages || [];
  const have = new Set([...box.querySelectorAll(".m")].map(n => n.dataset.id));
  const fresh = msgs.filter(m => !have.has(m.message_id));
  if (fresh.length) paint(fresh, false);
  // Ticks change without any new message arriving — someone opening the chat
  // on their phone turns grey to blue and nothing else about the thread moves.
  for (const m of msgs) {
    if (!m.is_from_me) continue;
    const n = box.querySelector(`.m[data-id="${CSS.escape(m.message_id)}"] .t .tk`);
    if (n && n.dataset.s !== m.status) n.outerHTML = ticks(m.status);
  }
  if (atBottom && fresh.length) box.scrollTop = box.scrollHeight;
}
es.addEventListener("wa", async e => {
  const ev = JSON.parse(e.data);
  if (ev.type && ev.type.startsWith("message.") &&
      ["delivered","read","played"].includes(ev.status)) {
    (ev.message_ids || []).forEach(id => {
      const n = document.querySelector(`.m[data-id="${CSS.escape(id)}"] .t .tk`);
      if (n) n.outerHTML = ticks(ev.status);
    });
    // The sidebar shows the same tick, so it has to move too. Debounced:
    // receipts arrive in bursts and each one would otherwise refetch the list.
    loadChatsSoon();
    return;
  }
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

let chatsSoon = null;
function loadChatsSoon() {
  clearTimeout(chatsSoon);
  chatsSoon = setTimeout(loadChats, 400);
}

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

/* ---------------------------------------------------------------- nav */

function showChats() {
  $("#nav-chats").classList.add("on");
  $("#profile").style.display = "none";
  $("#pane").style.display = current ? "" : "none";
  $("#blank").style.display = current ? "none" : "";
}

async function showProfile() {
  $("#nav-chats").classList.remove("on");
  $("#pane").style.display = "none";
  $("#blank").style.display = "none";
  const p = $("#profile");
  p.style.display = "";
  p.innerHTML = "<div class='sub'>Loading…</div>";
  const s = await (await fetch(KA("/api/status"))).json();
  const initial = (s.push_name || "?")[0].toUpperCase();
  const jid = s.number ? s.number + "@s.whatsapp.net" : "";
  p.innerHTML = `
    <div style="max-width:520px;margin:0 auto">
      <div class="bigav">${esc(initial)}${jid ? `<img src="${
        KA("/avatar/" + encodeURIComponent(jid))}" onerror="this.remove()">` : ""}</div>
      <h2 style="text-align:center">${esc(s.push_name || "WhatsApp")}</h2>
      <div class="sub" style="text-align:center">+${esc(s.number || "")}</div>
      <div class="card"><h3>Connection</h3>
        <div class="kv"><span>Status</span><span>${s.ready ? "Connected" : esc(s.phase)}</span></div>
        <div class="kv"><span>Sync</span><span>${s.sync.percent}% ${esc(s.sync.detail||"")}</span></div>
        <div class="kv"><span>Contacts known</span><span>${s.contacts_known ?? 0}</span></div>
      </div>
      <div class="card"><h3>Auto-reply</h3>
        <div class="kv"><span>Enabled</span><span>${s.auto_reply.enabled ? "yes" : "no"}</span></div>
        <div class="kv"><span>Backend</span><span>${esc(s.auto_reply.backend)}</span></div>
        <div class="kv"><span>Firing</span><span>${
          s.auto_reply.active ? "yes" : "no — " + esc(s.auto_reply.reason || "")}</span></div>
        <a class="btn" href="${KA("/settings")}">Open settings</a>
      </div>
      <div class="card"><h3>Storage</h3>
        <div class="kv"><span>Backend</span><span>${esc(s.storage.backend)}</span></div>
        <div class="kv"><span>Login on disk</span><span>${
          s.storage.session_persisted_as_file ? "yes — must persist" : "in the database"}</span></div>
      </div>
    </div>`;
}

$("#nav-chats").onclick = showChats;
$("#nav-profile").onclick = showProfile;
$("#nav-settings").onclick = () => location.href = KA("/settings");

async function paintMe() {
  const s = await (await fetch(KA("/api/status"))).json();
  const me = $("#nav-profile");
  if (!me || !s.number) return;
  me.innerHTML = esc((s.push_name || "?")[0].toUpperCase()) +
    `<img src="${KA("/avatar/" + encodeURIComponent(s.number + "@s.whatsapp.net"))}"
          onerror="this.remove()">`;
}

loadChats();
paintMe();                       // do not wait for the next status tick
setInterval(() => { loadChats(); refreshOpen(); refreshStatus(); }, 15000);
refreshStatus();   // don't wait 15s to find out we are already connected
"""


def shell(q: str) -> str:
    return f"""<div class="app">
  <div class="rail">
    <button id="nav-chats" class="on" title="Chats">{icon("chats")}</button>
    <div class="sp"></div>
    <button id="nav-settings" title="Settings">{icon("settings")}</button>
    <div class="me" id="nav-profile" title="Profile"></div>
  </div>
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
  <div class="panel" id="profile" style="display:none"></div>
</div>
<div class="sync" id="sync" style="display:none;position:fixed;bottom:0;left:0;
     right:0;z-index:5"></div>
<script>{JS}</script>"""
