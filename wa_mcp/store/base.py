"""The storage port: domain types and the interface every backend implements.

Deliberately abstracted at the DOMAIN level rather than over SQLAlchemy. An ORM
abstraction would still leak — Mongo has no ORM — and it would force the SQLite
path, which is what almost everyone runs, through an indirection it does not
need. A dozen honest methods are easier to implement three times than one clever
abstraction is to implement twice.

No `connection_id` anywhere. This product is one number per instance, and
threading a constant through every signature and every WHERE clause is noise in
a codebase people are meant to read. Multi-number is a different product; when
it happens it is a column and a predicate, not a redesign — the hard part was
always device selection, which `ClientFactory` solves.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Protocol, runtime_checkable


def now_ms() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)


def to_ms(value: datetime | int | float | None) -> int:
    """Epoch milliseconds, UTC.

    Timestamps are stored as integers rather than ISO strings: they sort
    correctly in every backend without a format convention, they cannot carry a
    timezone that is wrong, and they are half the bytes.
    """
    if value is None:
        return now_ms()
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return int(value.timestamp() * 1000)
    value = int(value)
    # WhatsApp hands out both seconds and milliseconds depending on the field.
    return value * 1000 if value < 10_000_000_000 else value


def from_ms(ms: int | None) -> datetime | None:
    return None if ms is None else datetime.fromtimestamp(ms / 1000, tz=timezone.utc)


@dataclass
class Message:
    message_id: str
    chat_jid: str
    ts: int                       # epoch ms
    sender_jid: str | None = None
    sender_name: str | None = None   # PushName — the only name we get for strangers
    is_from_me: bool = False
    type: str = "text"
    text: str | None = None
    media_ref: str | None = None     # local cache path, filled on demand
    media_meta: dict[str, Any] = field(default_factory=dict)
    quoted_id: str | None = None
    edited_at: int | None = None
    revoked_at: int | None = None
    # sent -> delivered -> read -> played. Only meaningful for our own messages.
    status: str = "sent"
    status_at: int | None = None
    raw_proto: bytes | None = None

    def public(self) -> dict[str, Any]:
        """The shape tools and the UI see. Never includes raw_proto."""
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
    # Status of the newest message, and whether we sent it. Derived from the
    # messages table rather than stored on the chat: a receipt changes status
    # long after the message landed, and a cached copy here would sit stale
    # showing one grey tick beside a conversation that has been read.
    last_from_me: bool = False
    last_status: str | None = None

    def public(self) -> dict[str, Any]:
        ts = from_ms(self.last_message_ts)
        return {
            "chat_jid": self.chat_jid,
            # Never leave the caller staring at a JID. display_name falls back
            # through name -> the phone number -> the raw jid.
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
    """What every backend must provide. Implementations: sqlite, postgres, mongo."""

    async def connect(self) -> None: ...
    async def close(self) -> None: ...

    # ---- writes, called from the socket's event loop ----
    async def upsert_message(self, m: Message) -> bool:
        """Insert. False means it was already stored — WhatsApp redelivers."""

    async def apply_edit(self, message_id: str, text: str | None, ts: int) -> None: ...
    async def apply_revoke(self, message_id: str, ts: int) -> None: ...
    async def set_media_ref(self, message_id: str, ref: str) -> None: ...
    async def set_status(self, message_ids: list[str], status: str,
                         ts: int) -> list[str]:
        """Advance delivery status. Returns the ids that actually moved.

        Never goes backwards: receipts arrive out of order, and a DELIVERED
        landing after a READ would otherwise un-read a message. Returning only
        the ids that changed is what lets callers emit an event per real
        transition rather than per receipt.
        """
    async def touch_chat(self, chat_jid: str, ts: int, from_me: bool,
                         preview: str | None) -> None: ...
    async def upsert_chat_meta(self, chat_jid: str, *, name: str | None = None,
                               is_group: bool | None = None,
                               archived: bool | None = None,
                               pinned: bool | None = None) -> None: ...
    async def set_unread(self, chat_jid: str, count: int) -> None:
        """Set an absolute count, for read state arriving from the phone."""

    async def rebuild_rollups(self) -> int:
        """Recompute last_message_ts and preview from the messages themselves.

        `chats.last_message_ts` is a denormalised summary, and any summary can
        drift from what it summarises. It did: 35 chats held today's messages
        while their rollup was NULL, so the most recently active conversation
        sorted to the bottom of the list. Rather than trust every write path to
        maintain it perfectly, the truth is recomputed from the rows.
        """

    # ---- reads ----
    async def list_chats(self, *, limit: int = 30, archived: bool = False,
                         query: str | None = None,
                         kind: str = "all") -> list[Chat]:
        """`kind` is one of all | groups | direct | unread.

        Filtering belongs in the query, not after it. Filtering a page of
        results is a bug that hides itself: with 295 groups but only 4 of them
        recent enough to be in the top 80 rows, a "Groups" tab showed 4 and
        looked like the data was missing.
        """
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

    # ---- key/value: settings, sync state, trigger config ----
    async def get_kv(self, key: str) -> dict[str, Any] | None: ...
    async def put_kv(self, key: str, value: dict[str, Any]) -> None: ...

    async def purge(self) -> dict[str, int]:
        """Delete every message, chat and stored value. Returns what went.

        Unlinking leaves a full copy of somebody's conversations on disk for an
        account this server can no longer reach — stale, unreachable, and still
        readable by anyone who gets the file. Logging out should leave nothing
        behind.
        """
        ...

    async def list_kv(self, prefix: str) -> list[str]:
        """Keys beginning with `prefix`.

        Needed to revoke every issued credential at once: they are stored one
        row per token, and without enumeration the only way to sign a lost
        connector out is to wait thirty days for it to expire.
        """
        ...


# --------------------------------------------------------------- search syntax

_QUOTED = __import__("re").compile(r'"([^"]*)"')


def split_query(query: str) -> tuple[list[str], list[str]]:
    """Split a search string into (include, exclude) parts.

    Every backend spells exclusion differently — FTS5 wants `NOT x`, Postgres'
    websearch_to_tsquery and Mongo's $text want `-x` — so a query written for
    one silently means something else on another. Rather than push that onto
    callers, both spellings are accepted here and each adapter renders its own
    dialect from the result.

    Quoted phrases survive intact, since all three support them.
    """
    query = (query or "").strip()
    include: list[str] = []
    exclude: list[str] = []

    def take(chunk: str, negated: bool) -> None:
        chunk = chunk.strip()
        if chunk:
            (exclude if negated else include).append(chunk)

    # Pull phrases out first so their internal spaces and hyphens survive.
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


# Delivery status, in order. Comparing by index is what makes the transition
# one-way — see Store.set_status.
STATUS_ORDER = ("sent", "delivered", "read", "played")


def status_rank(value: str) -> int:
    try:
        return STATUS_ORDER.index(value)
    except ValueError:
        return 0
