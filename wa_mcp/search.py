from __future__ import annotations

from .whatsapp import jid as J


async def find_chats(rt, *, query: str = "", kind: str = "all",
                     limit: int = 60, archived: bool = False) -> list:
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
