"""Postgres backend.

Raw asyncpg, matching the SQLite adapter's shape so the two read alike. The one
real divergence is search: Postgres can index an expression in place, so there
is no separate index table and no triggers to keep it honest — the GIN index is
computed from `text` and cannot drift from it.

Chosen for people who already run Postgres, and for containerised deployments:
whatsmeow speaks Postgres too, so the session store rides along and the
container needs no volume at all.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from .base import Chat, Message, Store, split_query, status_rank

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS wa_messages (
    id          BIGSERIAL PRIMARY KEY,
    message_id  TEXT    NOT NULL UNIQUE,
    chat_jid    TEXT    NOT NULL,
    sender_jid  TEXT,
    sender_name TEXT,
    is_from_me  BOOLEAN NOT NULL DEFAULT FALSE,
    ts          BIGINT  NOT NULL,
    type        TEXT    NOT NULL DEFAULT 'text',
    text        TEXT,
    media_ref   TEXT,
    media_meta  JSONB,
    quoted_id   TEXT,
    edited_at   BIGINT,
    revoked_at  BIGINT,
    status      TEXT NOT NULL DEFAULT 'sent',
    status_at   BIGINT,
    raw_proto   BYTEA
);
CREATE INDEX IF NOT EXISTS ix_wa_messages_chat_ts ON wa_messages (chat_jid, ts DESC);
CREATE INDEX IF NOT EXISTS ix_wa_messages_ts      ON wa_messages (ts DESC);
-- Expression index, so nothing has to be kept in sync when a message is edited.
CREATE INDEX IF NOT EXISTS ix_wa_messages_fts ON wa_messages
    USING GIN (to_tsvector('english', coalesce(text, '')));

CREATE TABLE IF NOT EXISTS wa_chats (
    chat_jid          TEXT PRIMARY KEY,
    name              TEXT,
    is_group          BOOLEAN NOT NULL DEFAULT FALSE,
    last_message_ts   BIGINT,
    last_message_text TEXT,
    unread_count      INTEGER NOT NULL DEFAULT 0,
    archived          BOOLEAN NOT NULL DEFAULT FALSE,
    pinned            BOOLEAN NOT NULL DEFAULT FALSE
);
CREATE INDEX IF NOT EXISTS ix_wa_chats_last ON wa_chats (last_message_ts DESC);

CREATE TABLE IF NOT EXISTS wa_kv (
    key   TEXT PRIMARY KEY,
    value JSONB NOT NULL
);
"""

_COLS = ("message_id, chat_jid, sender_jid, sender_name, is_from_me, ts, type, text, "
         "media_ref, media_meta, quoted_id, edited_at, revoked_at, status, status_at")


class PostgresStore(Store):
    def __init__(self, url: str):
        # The port hands us SQLAlchemy's form; asyncpg wants the plain one.
        self.dsn = url.replace("postgresql+asyncpg://", "postgresql://", 1)
        self._pool = None

    async def connect(self) -> None:
        import asyncpg

        self._pool = await asyncpg.create_pool(self.dsn, min_size=1, max_size=8)
        async with self._pool.acquire() as c:
            await c.execute(SCHEMA)

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    @property
    def pool(self):
        if self._pool is None:
            raise RuntimeError("store used before connect()")
        return self._pool

    # ---------------------------------------------------------------- writes

    async def upsert_message(self, m: Message) -> bool:
        async with self.pool.acquire() as c:
            row = await c.fetchrow(
                f"INSERT INTO wa_messages ({_COLS}, raw_proto) "
                "VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16) "
                "ON CONFLICT (message_id) DO NOTHING RETURNING id",
                m.message_id, m.chat_jid, m.sender_jid, m.sender_name,
                m.is_from_me, m.ts, m.type, m.text, m.media_ref,
                json.dumps(m.media_meta) if m.media_meta else None,
                m.quoted_id, m.edited_at, m.revoked_at, m.status, m.status_at,
                m.raw_proto,
            )
        return row is not None

    async def apply_edit(self, message_id: str, text: str | None, ts: int) -> None:
        async with self.pool.acquire() as c:
            await c.execute(
                "UPDATE wa_messages SET text=$1, edited_at=$2 WHERE message_id=$3",
                text, ts, message_id)

    async def apply_revoke(self, message_id: str, ts: int) -> None:
        async with self.pool.acquire() as c:
            await c.execute(
                "UPDATE wa_messages SET revoked_at=$1, text=NULL, media_ref=NULL "
                "WHERE message_id=$2", ts, message_id)

    async def set_media_ref(self, message_id: str, ref: str) -> None:
        async with self.pool.acquire() as c:
            await c.execute("UPDATE wa_messages SET media_ref=$1 WHERE message_id=$2",
                            ref, message_id)

    async def set_status(self, message_ids: list[str], status: str,
                         ts: int) -> list[str]:
        if not message_ids:
            return []
        order = ["sent", "delivered", "read", "played"][:status_rank(status)]
        async with self.pool.acquire() as c:
            rows = await c.fetch(
                "UPDATE wa_messages SET status=$1, status_at=$2 "
                "WHERE message_id = ANY($3::text[]) AND is_from_me "
                "AND status = ANY($4::text[]) RETURNING message_id",
                status, ts, list(message_ids), order)
        return [r["message_id"] for r in rows]

    async def touch_chat(self, chat_jid: str, ts: int, from_me: bool,
                         preview: str | None) -> None:
        bump = 0 if from_me else 1
        async with self.pool.acquire() as c:
            await c.execute(
                """INSERT INTO wa_chats (chat_jid, last_message_ts, last_message_text,
                                         unread_count)
                   VALUES ($1,$2,$3,$4)
                   ON CONFLICT (chat_jid) DO UPDATE SET
                     last_message_ts = GREATEST(
                       COALESCE(wa_chats.last_message_ts, 0), EXCLUDED.last_message_ts),
                     last_message_text = EXCLUDED.last_message_text,
                     unread_count = wa_chats.unread_count + $4""",
                chat_jid, ts, preview, bump)

    async def upsert_chat_meta(self, chat_jid: str, *, name: str | None = None,
                               is_group: bool | None = None,
                               archived: bool | None = None,
                               pinned: bool | None = None) -> None:
        sets = []
        if name is not None:
            sets.append("name = EXCLUDED.name")
        if is_group is not None:
            sets.append("is_group = EXCLUDED.is_group")
        if archived is not None:
            sets.append("archived = EXCLUDED.archived")
        if pinned is not None:
            sets.append("pinned = EXCLUDED.pinned")
        if not sets:
            return
        async with self.pool.acquire() as c:
            await c.execute(
                "INSERT INTO wa_chats (chat_jid, name, is_group, archived, pinned) "
                "VALUES ($1,$2,$3,$4,$5) ON CONFLICT (chat_jid) DO UPDATE SET "
                + ", ".join(sets),
                chat_jid, name, bool(is_group), bool(archived), bool(pinned))

    async def rebuild_rollups(self) -> int:
        async with self.pool.acquire() as c:
            res = await c.execute("""
                UPDATE wa_chats ch SET
                  last_message_ts = agg.mx,
                  last_message_text = agg.txt
                FROM (SELECT DISTINCT ON (chat_jid) chat_jid,
                             MAX(ts) OVER (PARTITION BY chat_jid) mx,
                             COALESCE(text, '[' || type || ']') txt
                      FROM wa_messages ORDER BY chat_jid, ts DESC) agg
                WHERE agg.chat_jid = ch.chat_jid
                  AND (ch.last_message_ts IS NULL OR ch.last_message_ts < agg.mx)
            """)
        return int(str(res).split()[-1]) if res else 0

    async def set_unread(self, chat_jid: str, count: int) -> None:
        async with self.pool.acquire() as c:
            await c.execute(
                "INSERT INTO wa_chats (chat_jid, unread_count) VALUES ($1,$2) "
                "ON CONFLICT (chat_jid) DO UPDATE SET unread_count = EXCLUDED.unread_count",
                chat_jid, max(0, count))

    # ----------------------------------------------------------------- reads

    async def list_chats(self, *, limit: int = 30, archived: bool = False,
                         query: str | None = None, kind: str = "all") -> list[Chat]:
        sql = _CHATS_SELECT + " WHERE archived = $1"
        args: list[Any] = [archived]
        if kind == "groups":
            sql += " AND is_group"
        elif kind == "direct":
            sql += " AND NOT is_group"
        elif kind == "unread":
            sql += " AND unread_count > 0"
        if query:
            args.append(f"%{query}%")
            sql += f" AND (name ILIKE ${len(args)} OR chat_jid ILIKE ${len(args)})"
        args.append(limit)
        sql += f" ORDER BY pinned DESC, last_message_ts DESC NULLS LAST LIMIT ${len(args)}"
        async with self.pool.acquire() as c:
            return [_chat(r) for r in await c.fetch(sql, *args)]

    async def get_chat(self, chat_jid: str) -> Chat | None:
        async with self.pool.acquire() as c:
            r = await c.fetchrow("SELECT * FROM wa_chats WHERE chat_jid=$1", chat_jid)
        return _chat(r) if r else None

    async def get_messages(self, chat_jid: str, *, limit: int = 30,
                           before_id: str | None = None, from_ts: int | None = None,
                           to_ts: int | None = None) -> list[Message]:
        sql = f"SELECT {_COLS} FROM wa_messages WHERE chat_jid = $1"
        args: list[Any] = [chat_jid]
        if before_id:
            args.append(before_id)
            sql += (f" AND ts < (SELECT ts FROM wa_messages WHERE message_id = ${len(args)})")
        if from_ts is not None:
            args.append(from_ts); sql += f" AND ts >= ${len(args)}"
        if to_ts is not None:
            args.append(to_ts); sql += f" AND ts <= ${len(args)}"
        args.append(limit)
        # Same second-resolution tie as SQLite; id is insertion order.
        sql += f" ORDER BY ts DESC, id DESC LIMIT ${len(args)}"
        async with self.pool.acquire() as c:
            return [_msg(r) for r in await c.fetch(sql, *args)]

    async def get_message(self, message_id: str) -> Message | None:
        async with self.pool.acquire() as c:
            r = await c.fetchrow(
                f"SELECT {_COLS} FROM wa_messages WHERE message_id=$1", message_id)
        return _msg(r) if r else None

    async def search(self, query: str, *, chat_jid: str | None = None, limit: int = 25,
                     from_ts: int | None = None, to_ts: int | None = None) -> list[Message]:
        """websearch_to_tsquery, so quoted phrases and -exclusion work the way a
        person types them without us parsing the string.

        It also never raises on malformed input, which is why there is no literal
        fallback here — SQLite's FTS5 needs one, Postgres does not.

        One behaviour worth knowing: the 'english' configuration removes stop
        words, so a phrase like "meet at" collapses to the single term 'meet'
        and will also match "meeting". SQLite's FTS5 keeps the phrase intact.
        That is a genuine difference in search semantics rather than a bug, and
        the port does not paper over it — stop-word removal and stemming are
        what make the results good.
        """
        include, exclude = split_query(query)
        query = " ".join(include) + "".join(f" -{e}" for e in exclude)
        args: list[Any] = [query, chat_jid]
        sql = (f"SELECT {_COLS} FROM wa_messages "
               "WHERE to_tsvector('english', coalesce(text,'')) @@ websearch_to_tsquery('english', $1) "
               "AND revoked_at IS NULL AND ($2::text IS NULL OR chat_jid = $2)")
        if from_ts is not None:
            args.append(from_ts); sql += f" AND ts >= ${len(args)}"
        if to_ts is not None:
            args.append(to_ts); sql += f" AND ts <= ${len(args)}"
        args.append(limit)
        sql += (" ORDER BY ts_rank(to_tsvector('english', coalesce(text,'')), "
                f"websearch_to_tsquery('english', $1)) DESC, ts DESC LIMIT ${len(args)}")
        async with self.pool.acquire() as c:
            return [_msg(r) for r in await c.fetch(sql, *args)]

    async def thread_around(self, message_id: str, radius: int = 15) -> list[Message]:
        async with self.pool.acquire() as c:
            anchor = await c.fetchrow(
                "SELECT chat_jid, ts FROM wa_messages WHERE message_id=$1", message_id)
            if anchor is None:
                return []
            before = await c.fetch(
                f"SELECT {_COLS} FROM wa_messages WHERE chat_jid=$1 AND ts < $2 "
                "ORDER BY ts DESC LIMIT $3", anchor["chat_jid"], anchor["ts"], radius)
            after = await c.fetch(
                f"SELECT {_COLS} FROM wa_messages WHERE chat_jid=$1 AND ts >= $2 "
                "ORDER BY ts ASC LIMIT $3", anchor["chat_jid"], anchor["ts"], radius + 1)
        return [_msg(r) for r in reversed(before)] + [_msg(r) for r in after]

    async def unread_count(self, chat_jid: str | None = None) -> int:
        async with self.pool.acquire() as c:
            if chat_jid:
                v = await c.fetchval(
                    "SELECT unread_count FROM wa_chats WHERE chat_jid=$1", chat_jid)
            else:
                v = await c.fetchval("SELECT COALESCE(SUM(unread_count),0) FROM wa_chats")
        return int(v or 0)

    # -------------------------------------------------------------------- kv

    async def get_kv(self, key: str) -> dict[str, Any] | None:
        async with self.pool.acquire() as c:
            v = await c.fetchval("SELECT value FROM wa_kv WHERE key=$1", key)
        return json.loads(v) if isinstance(v, str) else v

    async def put_kv(self, key: str, value: dict[str, Any]) -> None:
        async with self.pool.acquire() as c:
            await c.execute(
                "INSERT INTO wa_kv (key, value) VALUES ($1, $2::jsonb) "
                "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
                key, json.dumps(value))


def _msg(r) -> Message:
    meta = r["media_meta"]
    if isinstance(meta, str):
        meta = json.loads(meta)
    return Message(
        message_id=r["message_id"], chat_jid=r["chat_jid"], sender_jid=r["sender_jid"],
        sender_name=r["sender_name"], is_from_me=bool(r["is_from_me"]), ts=int(r["ts"]),
        type=r["type"], text=r["text"], media_ref=r["media_ref"], media_meta=meta or {},
        quoted_id=r["quoted_id"], edited_at=r["edited_at"], revoked_at=r["revoked_at"],
        status=r["status"] or "sent", status_at=r["status_at"],
    )


# The sidebar shows the same ticks as the thread, so the chat list carries the
# newest message's status. Derived, not cached on the chat row: a receipt lands
# long after the message did and a stored copy would drift. LATERAL rather than
# two correlated subqueries so the newest row is read once; it seeks the same
# (chat_jid, ts DESC) index the message list uses.
_CHATS_SELECT = """
SELECT wa_chats.*, last.is_from_me AS last_from_me, last.status AS last_status
FROM wa_chats
LEFT JOIN LATERAL (
    SELECT m.is_from_me, m.status FROM wa_messages m
    WHERE m.chat_jid = wa_chats.chat_jid ORDER BY m.ts DESC LIMIT 1
) AS last ON TRUE
"""


def _chat(r) -> Chat:
    return Chat(
        chat_jid=r["chat_jid"], name=r["name"], is_group=bool(r["is_group"]),
        last_message_ts=r["last_message_ts"], last_message_text=r["last_message_text"],
        unread_count=int(r["unread_count"]), archived=bool(r["archived"]),
        pinned=bool(r["pinned"]),
        last_from_me=bool(_opt(r, "last_from_me")),
        last_status=_opt(r, "last_status"),
    )


def _opt(r, key: str):
    """Only the chat-list query selects these; get_chat passes a plain row."""
    try:
        return r[key]
    except KeyError:
        return None
