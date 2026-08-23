"""Search: chats by name, and messages by content — both out of the database.

WhatsApp answers one query with two lists, and so does this. Typing a person's
name should find the conversation; typing a phrase should find where it was
said. Anything less makes the box feel broken.

The database is the single source of truth. Display names are written into
`chats.name` as the address book resolves them (see
`WhatsApp.persist_contact_names`) precisely so a query can be a WHERE clause
rather than a scan of an in-memory dictionary — otherwise the list shows one
answer and the search shows another.
"""
from __future__ import annotations

from .whatsapp import jid as J


async def find_chats(rt, *, query: str = "", kind: str = "all",
                     limit: int = 60, archived: bool = False) -> list:
    """Chats matching `query`, pinned first then newest.

    The database query is merged with a scan over resolved names, rather than
    the scan being a fallback for when the query returns nothing. A chat whose
    stored name is a masked number — 35 here — matches nothing in SQLite but is
    perfectly findable once the address book supplies the real name, and it was
    invisible whenever some other chat happened to match first. That is how
    "akbar" found only "Asif Akbar Brother" and sent to the wrong person, while
    "Akbar Ktr Srm" sat there stored as "+91∙∙∙∙∙∙∙∙88".
    """
    book = rt.contacts
    chats = await rt.store.list_chats(limit=limit, archived=archived,
                                      kind=kind, query=query or None)
    out = [(c, book.display_name(c.chat_jid, chat_name=c.name)) for c in chats]
    if not query:
        return out

    seen = {c.chat_jid for c, _ in out}
    pool = await rt.store.list_chats(limit=2000, archived=archived, kind=kind)
    for c in pool:
        if c.chat_jid in seen or len(out) >= limit:
            continue
        name = book.display_name(c.chat_jid, chat_name=c.name)
        if _matches(query, name, c.chat_jid):
            out.append((c, name))
    return out


async def find_messages(rt, *, query: str, limit: int = 30,
                        chat_jid: str | None = None) -> list[dict]:
    """Messages containing `query`, best matches first, each with its chat.

    Straight through the full-text index — FTS5 on SQLite, tsvector on
    Postgres, $text on Mongo — so this scales with the index rather than with
    the size of the history.
    """
    if not (query or "").strip():
        return []
    msgs = await rt.store.search(query, limit=limit, chat_jid=chat_jid)
    book = rt.contacts
    out = []
    for m in msgs:
        chat = await rt.store.get_chat(m.chat_jid)
        d = m.public()
        d["chat_name"] = book.display_name(m.chat_jid,
                                           chat_name=chat.name if chat else None)
        d["sender_display"] = (
            book.display_name(m.sender_jid or "", push_name=m.sender_name)
            if m.sender_jid and not m.is_from_me else "You")
        out.append(d)
    return out


def _matches(needle: str, name: str, chat_jid: str) -> bool:
    """Case-insensitive substring over name, number and JID.

    The number is included because people search for unsaved contacts by typing
    the last few digits — it is the only handle they have for them.
    """
    n = (needle or "").strip().lower()
    if not n:
        return True
    if n in (name or "").lower():
        return True
    digits = "".join(ch for ch in n if ch.isdigit())
    if digits and digits in J.phone(chat_jid):
        return True
    return n in chat_jid.lower()


async def find_contacts(rt, *, query: str, limit: int = 20) -> list[tuple[str, str]]:
    """Address-book matches with no conversation yet, as (jid, name).

    Searching chats alone answers "who have I talked to about this", which is
    not the question someone asks when they type a name. Measured here: 8,518
    contacts against 1,082 chats, so most of the address book was invisible —
    "Akbar Ktr Srm" returned nothing while sitting in WhatsApp's own store.

    Anyone who already has a chat is left out, since they are in that list
    already and appearing twice reads as two different people.
    """
    if not (query or "").strip():
        return []
    from .whatsapp import jid as J

    have = {J.normalise(c.chat_jid)
            for c in await rt.store.list_chats(limit=5000)}
    out = []
    for jid, name in rt.contacts.search(query, limit=limit * 3):
        if J.normalise(jid) in have:
            continue
        out.append((jid, name))
        if len(out) >= limit:
            break
    return out
