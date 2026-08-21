"""JID handling — normalisation, and the LID problem.

Two things make WhatsApp identifiers messier than they look, and both produce
duplicate chats if ignored.

**Device suffixes.** A JID's user part carries the linked-device number:
`919100828649:7@s.whatsapp.net` is device 7 of that account. The same human
appears with different suffixes depending on which of their devices sent a
message, so anything keyed on the raw string fragments one conversation into
several.

**LIDs.** WhatsApp is migrating to privacy identifiers — `2076…@lid` instead of
a phone number. Measured on a live account: 99.9% of senders and 57% of chats
already arrive as LIDs, and the same person appears under both forms. whatsmeow
maintains the mapping, so the fix is to resolve rather than to guess; this module
holds the pure string handling and the client supplies the resolver.
"""
from __future__ import annotations

from typing import Callable

GROUP_SERVER = "g.us"
LID_SERVER = "lid"
USER_SERVER = "s.whatsapp.net"
BROADCAST = "status@broadcast"
NEWSLETTER_SERVER = "newsletter"


def normalise(jid: str) -> str:
    """Strip the device suffix and lowercase the server.

    `919100828649:7@s.whatsapp.net` -> `919100828649@s.whatsapp.net`

    This is the form everything downstream keys on. Without it a chat splits
    per sending device and the same conversation shows up two or three times.
    """
    if not jid:
        return ""
    user, _, server = jid.partition("@")
    if not server:
        return jid.split(":", 1)[0]
    return f"{user.split(':', 1)[0]}@{server.lower()}"


def from_obj(jid_obj) -> str:
    """Render a neonize JID protobuf as `user@server`.

    Never `str(jid)` — JID is a protobuf message, so that yields a multi-line
    field dump rather than an address.
    """
    if jid_obj is None:
        return ""
    user = getattr(jid_obj, "User", None)
    if user is None:
        return ""
    server = getattr(jid_obj, "Server", "") or USER_SERVER
    return normalise(f"{user}@{server}")


def is_group(jid: str) -> bool:
    return jid.endswith("@" + GROUP_SERVER)


def is_lid(jid: str) -> bool:
    return jid.endswith("@" + LID_SERVER)


def is_ignorable(jid: str) -> bool:
    """Pseudo-chats WhatsApp delivers through the ordinary message path.

    `status@broadcast` carries Status/Story updates. Left alone it accumulates
    an unread count and sits at the top of the chat list as though someone had
    messaged — observed on a live account. Newsletters are channels, not
    conversations. Both are still forwarded as events; they are just not chats.
    """
    j = normalise(jid)
    return j == BROADCAST or j.endswith("@" + NEWSLETTER_SERVER)


def phone(jid: str) -> str:
    """The bare number, when there is one.

    Neither LIDs nor groups have one. Groups matter here because the modern id
    format is all digits — `120363228197508350@g.us` — so a naive isdigit()
    check hands back an 18-digit number as though it were a phone, and the chat
    list renders that instead of a name. Caught against live data; the older
    `919980982358-1479370608@g.us` form hid it because of the hyphen.
    """
    if is_group(jid) or is_lid(jid):
        return ""
    user = normalise(jid).split("@")[0]
    return user if user.isdigit() else ""


def to_jid(value: str) -> str:
    """Accept a phone number or a JID; return a JID.

    Tools take `to="919812345678"` as readily as a full address, because making
    a model construct `user@server` is a needless way for a send to fail.
    """
    value = (value or "").strip().lstrip("+")
    if "@" in value:
        return normalise(value)
    return f"{value}@{USER_SERVER}" if value else ""


class Resolver:
    """Canonicalises LIDs to phone JIDs, with a cache.

    The mapping lives in whatsmeow's own store and is exposed as
    `get_pn_from_lid`. Resolving through it rather than reading the table keeps
    us out of Go's schema — that table is not ours and its shape can change.

    A failed lookup returns the LID unchanged. That is deliberate: an
    unresolvable LID is still a stable identifier, so the conversation stays
    coherent even when we cannot name the person behind it.
    """

    def __init__(self, lookup: Callable[[str], object] | None = None):
        self._lookup = lookup
        self._cache: dict[str, str] = {}

    async def canonical(self, jid: str) -> str:
        jid = normalise(jid)
        if not is_lid(jid) or self._lookup is None:
            return jid
        if jid in self._cache:
            return self._cache[jid]
        resolved = jid
        try:
            result = self._lookup(jid)
            if hasattr(result, "__await__"):
                result = await result
            candidate = from_obj(result) if not isinstance(result, str) else normalise(result)
            if candidate and not is_lid(candidate):
                resolved = candidate
        except Exception:
            # Never let identifier resolution break message ingestion. An
            # unresolved LID costs a display name; a raised exception costs the
            # message.
            pass
        self._cache[jid] = resolved
        return resolved
