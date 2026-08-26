from __future__ import annotations

from typing import Callable

GROUP_SERVER = "g.us"
LID_SERVER = "lid"
USER_SERVER = "s.whatsapp.net"
BROADCAST = "status@broadcast"
NEWSLETTER_SERVER = "newsletter"


def normalise(jid: str) -> str:
    if not jid:
        return ""
    user, _, server = jid.partition("@")
    if not server:
        return jid.split(":", 1)[0]
    return f"{user.split(':', 1)[0]}@{server.lower()}"


def from_obj(jid_obj) -> str:
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
    j = normalise(jid)
    return j == BROADCAST or j.endswith("@" + NEWSLETTER_SERVER)


def phone(jid: str) -> str:
    if is_group(jid) or is_lid(jid):
        return ""
    user = normalise(jid).split("@")[0]
    return user if user.isdigit() else ""


def to_jid(value: str) -> str:
    value = (value or "").strip().lstrip("+")
    if "@" in value:
        return normalise(value)
    return f"{value}@{USER_SERVER}" if value else ""


class Resolver:

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
            pass
        self._cache[jid] = resolved
        return resolved
