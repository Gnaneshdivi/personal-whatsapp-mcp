from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Protocol, runtime_checkable


def now_ms() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)


def to_ms(value: datetime | int | float | None) -> int:
    if value is None:
        return now_ms()
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return int(value.timestamp() * 1000)
    value = int(value)
    return value * 1000 if value < 10_000_000_000 else value


def from_ms(ms: int | None) -> datetime | None:
    return None if ms is None else datetime.fromtimestamp(ms / 1000, tz=timezone.utc)


@dataclass
class Message:
    message_id: str
    chat_jid: str
    ts: int
    sender_jid: str | None = None
    sender_name: str | None = None
    is_from_me: bool = False
    type: str = "text"
    text: str | None = None
    media_ref: str | None = None
    media_meta: dict[str, Any] = field(default_factory=dict)
    quoted_id: str | None = None
    edited_at: int | None = None
    revoked_at: int | None = None
    status: str = "sent"
    status_at: int | None = None
    raw_proto: bytes | None = None

    def public(self) -> dict[str, Any]:
        return {
            "message_id": self.message_id,
            "chat_jid": self.chat_jid,
            "sender_jid": self.sender_jid,
            "sender_name": self.sender_name,
            "from_me": self.is_from_me,
            "timestamp": (from_ms(self.ts) or datetime.now(timezone.utc)).isoformat(),
            "type": self.type,
            "text": self.text,
            "has_media": bool(self.media_meta or self.media_ref),
            "media_downloaded": bool(self.media_ref),
            "quoted_id": self.quoted_id,
            "edited": self.edited_at is not None,
            "revoked": self.revoked_at is not None,
            "status": self.status if self.is_from_me else None,
            "status_at": self.status_at,
        }


@dataclass
class Chat:
    chat_jid: str
    name: str | None = None
    is_group: bool = False
    last_message_ts: int | None = None
    last_message_text: str | None = None
    unread_count: int = 0
    archived: bool = False
    pinned: bool = False
    last_from_me: bool = False
    last_status: str | None = None

    def public(self) -> dict[str, Any]:
        ts = from_ms(self.last_message_ts)
        return {
            "chat_jid": self.chat_jid,
            "name": self.display_name,
            "is_group": self.is_group,
            "last_message_at": ts.isoformat() if ts else None,
            "last_message": self.last_message_text,
            "unread": self.unread_count,
            "archived": self.archived,
            "pinned": self.pinned,
            "last_from_me": self.last_from_me,
            "last_status": self.last_status if self.last_from_me else None,
        }

    @property
    def display_name(self) -> str:
        if self.name:
            return self.name
        user = self.chat_jid.split("@")[0].split(":")[0]
        return user if user.isdigit() else self.chat_jid


@runtime_checkable
class Store(Protocol):

    async def connect(self) -> None: ...
    async def close(self) -> None: ...

    async def upsert_message(self, m: Message) -> bool:
        pass

    async def apply_edit(self, message_id: str, text: str | None, ts: int) -> None: ...
    async def apply_revoke(self, message_id: str, ts: int) -> None: ...
    async def set_media_ref(self, message_id: str, ref: str) -> None: ...
    async def set_status(self, message_ids: list[str], status: str,
                         ts: int) -> list[str]:
        pass
    async def touch_chat(self, chat_jid: str, ts: int, from_me: bool,
                         preview: str | None) -> None: ...
    async def upsert_chat_meta(self, chat_jid: str, *, name: str | None = None,
                               is_group: bool | None = None,
                               archived: bool | None = None,
                               pinned: bool | None = None) -> None: ...
    async def set_unread(self, chat_jid: str, count: int) -> None:
        pass

    async def rebuild_rollups(self) -> int:
        pass

    async def list_chats(self, *, limit: int = 30, archived: bool = False,
                         query: str | None = None,
                         kind: str = "all") -> list[Chat]:
        pass
    async def get_chat(self, chat_jid: str) -> Chat | None: ...
    async def get_messages(self, chat_jid: str, *, limit: int = 30,
                           before_id: str | None = None,
                           from_ts: int | None = None,
                           to_ts: int | None = None) -> list[Message]: ...
    async def get_message(self, message_id: str) -> Message | None: ...
    async def search(self, query: str, *, chat_jid: str | None = None,
                     limit: int = 25, from_ts: int | None = None,
                     to_ts: int | None = None) -> list[Message]: ...
    async def thread_around(self, message_id: str, radius: int = 15) -> list[Message]: ...
    async def unread_count(self, chat_jid: str | None = None) -> int: ...

    async def get_kv(self, key: str) -> dict[str, Any] | None: ...
    async def put_kv(self, key: str, value: dict[str, Any]) -> None: ...

    async def purge(self) -> dict[str, int]:
        ...

    async def list_kv(self, prefix: str) -> list[str]:
        ...


_QUOTED = __import__("re").compile(r'"([^"]*)"')


def split_query(query: str) -> tuple[list[str], list[str]]:
    query = (query or "").strip()
    include: list[str] = []
    exclude: list[str] = []

    def take(chunk: str, negated: bool) -> None:
        chunk = chunk.strip()
        if chunk:
            (exclude if negated else include).append(chunk)

    rest = _QUOTED.sub(lambda m: f"\x00{m.group(1)}\x00", query)
    tokens, negate_next = rest.split(), False
    for tok in tokens:
        if tok.upper() == "NOT":
            negate_next = True
            continue
        negated = negate_next or tok.startswith("-")
        negate_next = False
        if tok.startswith("-"):
            tok = tok[1:]
        if "\x00" in tok:
            take(f'"{tok.strip(chr(0))}"', negated)
        else:
            take(tok, negated)
    return include, exclude


STATUS_ORDER = ("sent", "delivered", "read", "played")


def status_rank(value: str) -> int:
    try:
        return STATUS_ORDER.index(value)
    except ValueError:
        return 0
