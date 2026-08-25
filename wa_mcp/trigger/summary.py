"""Periodic digests of what came in.

The point is not to read everything — it is to not miss the few things that
matter. So the digest is built around the "important" list from settings: those
are called out first and by name, and the rest is a short account of what else
happened.

Two rules keep a digest worth reading. Nothing is sent when nothing happened,
because one that arrives saying "no activity" teaches you to ignore the ones
that do not. And the window is exactly the time since the last digest, so
messages are neither repeated nor skipped when an interval is changed or the
process restarts mid-cycle.
"""
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
    """Where a digest goes. Same vocabulary as alerts."""
    route = s.summary.route
    if route == "me":
        return J.normalise(self_jid or "")
    if route == "number":
        return J.to_jid(s.summary.jid) if s.summary.jid else ""
    if route == "chat":
        # No single chat makes sense for a digest spanning many, so this is
        # treated as the owner's own thread rather than silently doing nothing.
        return J.normalise(self_jid or "")
    return ""


# Bounds, so one noisy group cannot drown the summary — or bankrupt it. A
# single association group produced 89 messages of forwarded ads and market
# tips in one window, which is both the bulk of the cost and none of the value.
PER_CHAT = 12          # newest N inbound messages from any one conversation
PER_MESSAGE = 300      # a forwarded brochure says nothing more in its 900th char
TOTAL_CHARS = 12000    # ceiling on the whole prompt body


def _trim(text: str) -> str:
    t = " ".join((text or "").split())
    return t if len(t) <= PER_MESSAGE else t[:PER_MESSAGE] + "…"


async def collect(rt, since_ms: int, s) -> tuple[str, int]:
    """Inbound messages since `since_ms`, grouped by chat. Returns (text, count).

    Newest first within a chat, then reversed for reading order, so when a
    conversation is truncated it is the OLD end that is dropped — the recent
    turns are the ones an ask is likely to be in.
    """
    book = rt.contacts
    lines, total, size = [], 0, 0
    for chat in await rt.store.list_chats(limit=s.summary.max_chats * 5):
        if chat.is_group and not s.summary.include_groups:
            continue
        rows = [m for m in await rt.store.get_messages(chat.chat_jid, limit=60)
                if not m.is_from_me and m.text and m.ts > since_ms][:PER_CHAT]
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
    """The digest text, or ("", 0) when there is nothing to say."""
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

    # The digest is built by the model backend even when replies run through a
    # webhook: summarising is this server's own job, not something to hand to
    # someone else's endpoint, and it needs no tools.
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
    """Build and send one digest. Returns where it went, or None."""
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
    # The clock advances even with nothing to report, so a quiet hour does not
    # make the next digest cover two.
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
    """Wake on the shortest sensible tick and run when due.

    Polls rather than sleeping for the whole interval so that changing it in
    settings takes effect now, instead of after the old interval finishes.
    """
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
