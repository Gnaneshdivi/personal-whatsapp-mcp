from __future__ import annotations

import asyncio
import logging
import time

from ..whatsapp import jid as J
from .backends import BackendError, Context, reply_via_model

log = logging.getLogger(__name__)

STATE_KEY = "summary.last_run"

PROMPT = """\
Summarise the WhatsApp messages below for {me_name}, who has not been reading them.

{important}
Structure the summary in exactly these two parts, and omit a part only if it
would be empty:

NEEDS YOU
One bullet for each thing waiting on {me_name} personally. Include anything
where someone:
  - asks them to check, look at, review, confirm or approve something
  - asks a direct question that has not been answered
  - is waiting on a reply, a decision, a file, a payment or a date
  - chases something asked about before
Group chats only appear here when {me_name} was mentioned or someone replied to
them, so read those as directed at {me_name} — but general chatter that merely
happens to name them is still not a request.
Each bullet: who, what they want, and the deadline if one was given. Quote the
few words that make the ask concrete. Be specific — "Akbar wants the beta
account set up for Shiwani" beats "Akbar had a request".

EVERYTHING ELSE
One short line per remaining conversation, grouped by person. Skip chats with
nothing worth reporting entirely.

No preamble, no sign-off, no restating these instructions.

Messages since the last summary:
{body}"""

IMPORTANT_CLAUSE = """\
Call these out FIRST and explicitly if they appear anywhere below — they are
the reason this summary exists, and missing one is the only real failure:
{items}
"""


def destination(s, self_jid: str) -> str:
    route = s.summary.route
    if route == "me":
        return J.normalise(self_jid or "")
    if route == "number":
        return J.to_jid(s.summary.jid) if s.summary.jid else ""
    if route == "chat":
        return J.normalise(self_jid or "")
    return ""


PER_CHAT = 12
PER_MESSAGE = 300
TOTAL_CHARS = 12000


def _trim(text: str) -> str:
    t = " ".join((text or "").split())
    return t if len(t) <= PER_MESSAGE else t[:PER_MESSAGE] + "…"


async def addressed_to_me(rt, m, self_user: str) -> bool:
    if self_user and f"@{self_user}" in (m.text or ""):
        return True
    if m.quoted_id:
        quoted = await rt.store.get_message(m.quoted_id)
        if quoted is not None and quoted.is_from_me:
            return True
    return False


async def collect(rt, since_ms: int, s) -> tuple[str, int]:
    book = rt.contacts
    self_user = (getattr(rt.wa, "self_jid", "") or "").split("@")[0].split(":")[0]
    lines, total, size = [], 0, 0
    for chat in await rt.store.list_chats(limit=s.summary.max_chats * 5):
        if chat.is_group and not s.summary.include_groups:
            continue
        rows = [m for m in await rt.store.get_messages(chat.chat_jid, limit=60)
                if not m.is_from_me and m.text and m.ts > since_ms]
        if chat.is_group:
            keep = []
            for m in rows:
                if await addressed_to_me(rt, m, self_user):
                    keep.append(m)
            rows = keep
        rows = rows[:PER_CHAT]
        if not rows:
            continue
        name = book.display_name(chat.chat_jid, chat_name=chat.name)
        body = "\n".join(f"  {_trim(m.text)}" for m in reversed(rows))
        block = f"{name}:\n{body}"
        if size + len(block) > TOTAL_CHARS:
            break
        lines.append(block)
        size += len(block)
        total += len(rows)
        if len(lines) >= s.summary.max_chats:
            break
    return "\n\n".join(lines), total


async def build(rt, since_ms: int) -> tuple[str, int]:
    s = rt.trigger.settings
    body, count = await collect(rt, since_ms, s)
    if not count:
        return "", 0

    important = ""
    if s.summary.important:
        important = IMPORTANT_CLAUSE.format(
            items="\n".join(f"- {i}" for i in s.summary.important))

    prompt = PROMPT.format(
        me_name=getattr(rt.wa, "push_name", "") or "you",
        important=important, body=body)

    ctx = Context(message=prompt, chat_name="", chat_jid="", sender_name="",
                  sender_jid="", me_name=getattr(rt.wa, "push_name", "") or "you",
                  message_id="", timestamp=str(int(time.time())), history=[])
    ctx.system = (
        "You write short, factual summaries of WhatsApp activity for someone "
        "catching up. Plain text — no markdown, no bold, no nested bullets; "
        "this is read in WhatsApp. Never invent a request that is not in the "
        "messages, and never soften one that is: the whole value of this is "
        "that nothing asked of them gets missed.")
    return await reply_via_model(s.model, ctx), count


async def run_once(rt) -> str | None:
    s = rt.trigger.settings
    if not s.summary.configured or not s.model.configured:
        return None
    target = destination(s, getattr(rt.wa, "self_jid", ""))
    if not target:
        return None

    state = await rt.store.get_kv(STATE_KEY) or {}
    now = time.time()
    since = float(state.get("at") or (now - s.summary.every_minutes * 60))

    try:
        text, count = await build(rt, int(since * 1000))
    except BackendError as exc:
        log.warning("summary not built: %s", exc)
        return None
    await rt.store.put_kv(STATE_KEY, {"at": now})
    if not count or not text.strip():
        return None

    header = f"Summary — {count} message(s) since the last one\n\n"
    try:
        sent = await rt.wa.send_text(target, header + text.strip())
        if sent.get("message_id"):
            rt.trigger.note_generated(sent["message_id"])
        log.info("summary of %d message(s) sent to %s", count, target)
        return target
    except Exception as exc:
        log.warning("summary send failed: %s", exc)
        return None


async def loop(rt) -> None:
    while True:
        try:
            s = rt.trigger.settings if rt.trigger else None
            if s and s.summary.configured and rt.wa and rt.wa.sync.state.ready:
                state = await rt.store.get_kv(STATE_KEY) or {}
                last = float(state.get("at") or 0)
                if time.time() - last >= s.summary.every_minutes * 60:
                    await run_once(rt)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("summary loop: %s", exc)
        await asyncio.sleep(30)
