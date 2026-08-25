"""SQLite backend — the default, and the one almost everyone will run.

Raw aiosqlite rather than an ORM. The schema is small, the queries are short,
and FTS5 needs hand-written DDL anyway; an ORM would add a layer to debug
without removing a line of SQL.

Two things carry the design:

  WAL mode          the web UI reads while the socket writes. Without it every
                    read blocks behind the writer and the chat list stutters.
  FTS5, external    full-text search over `messages.text`. Postgres can index an
                    expression in place; SQLite cannot, so the index is a
                    separate virtual table kept in sync by triggers. Verified:
                    phrases, NOT, prefix and bm25 ranking all behave.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import aiosqlite

from .base import Chat, Message, Store, split_query, status_rank

log = logging.getLogger(__name__)

SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA synchronous  = NORMAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS messages (
    id          INTEGER PRIMARY KEY,   -- rowid, joined to the FTS index
    message_id  TEXT    NOT NULL UNIQUE,
    chat_jid    TEXT    NOT NULL,
    sender_jid  TEXT,
    sender_name TEXT,
    is_from_me  INTEGER NOT NULL DEFAULT 0,
    ts          INTEGER NOT NULL,
    type        TEXT    NOT NULL DEFAULT 'text',
    text        TEXT,
    media_ref   TEXT,
    media_meta  TEXT,
    quoted_id   TEXT,
    edited_at   INTEGER,
    revoked_at  INTEGER,
    status      TEXT    NOT NULL DEFAULT 'sent',
    status_at   INTEGER,
    raw_proto   BLOB
);
CREATE INDEX IF NOT EXISTS ix_messages_chat_ts ON messages(chat_jid, ts DESC);
CREATE INDEX IF NOT EXISTS ix_messages_ts      ON messages(ts DESC);

CREATE TABLE IF NOT EXISTS chats (
    chat_jid          TEXT PRIMARY KEY,
    name              TEXT,
    is_group          INTEGER NOT NULL DEFAULT 0,
    last_message_ts   INTEGER,
    last_message_text TEXT,
    unread_count      INTEGER NOT NULL DEFAULT 0,
    archived          INTEGER NOT NULL DEFAULT 0,
    pinned            INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_chats_last ON chats(last_message_ts DESC);

CREATE TABLE IF NOT EXISTS kv (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- External-content FTS: the index stores no copy of the text, it points at
-- messages.rowid. Keeps the database roughly the size it would be anyway.
CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
    text,
    content='messages',
    content_rowid='id',
    tokenize='porter unicode61'
);

CREATE TRIGGER IF NOT EXISTS messages_ai AFTER INSERT ON messages BEGIN
    INSERT INTO messages_fts(rowid, text) VALUES (new.id, new.text);
END;
CREATE TRIGGER IF NOT EXISTS messages_ad AFTER DELETE ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, text) VALUES('delete', old.id, old.text);
END;
CREATE TRIGGER IF NOT EXISTS messages_au AFTER UPDATE OF text ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, text) VALUES('delete', old.id, old.text);
    INSERT INTO messages_fts(rowid, text) VALUES (new.id, new.text);
END;
"""

_COLS = (
    "message_id, chat_jid, sender_jid, sender_name, is_from_me, ts, type, text, "
    "media_ref, media_meta, quoted_id, edited_at, revoked_at, status, status_at"
)


class SQLiteStore(Store):
    def __init__(self, path: str | Path):
        self.path = str(path)
        self._db: aiosqlite.Connection | None = None

    # ------------------------------------------------------------- lifecycle

    async def connect(self) -> None:
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(self.path)
        self._db.row_factory = aiosqlite.Row
        await self._db.executescript(SCHEMA)
        await self._migrate()
        await self._db.commit()

    # Columns added after the first release. CREATE TABLE IF NOT EXISTS does
    # nothing to a table that already exists, so a new field is invisible to an
    # existing database and every query referencing it fails.
    #
    # This exists because the alternative was taken once: the database was
    # deleted to pick up a new column, and 6,225 messages went with it. History
    # sync only ever runs at pair time, so that data could not be recovered
    # without unlinking the number and scanning again. Additive migrations are
    # not a nicety here — losing the store means losing history permanently.
    MIGRATIONS = (
        ("messages", "status", "TEXT NOT NULL DEFAULT 'sent'"),
        ("messages", "status_at", "INTEGER"),
        ("messages", "media_meta", "TEXT"),
        ("chats", "pinned", "INTEGER NOT NULL DEFAULT 0"),
    )

    async def _migrate(self) -> None:
        for table, column, decl in self.MIGRATIONS:
            cols = {r["name"] for r in await (
                await self._db.execute(f"PRAGMA table_info({table})")).fetchall()}
            if not cols or column in cols:
                continue
            await self._db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
            log.info("migrated: added %s.%s", table, column)

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    @property
    def db(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("store used before connect()")
        return self._db

    # ---------------------------------------------------------------- writes

    async def upsert_message(self, m: Message) -> bool:
        """INSERT OR IGNORE on the unique message_id.

        Idempotency belongs in the index, not in a cache. WhatsApp redelivers on
        reconnect and history sync replays the same ids; an evictable marker
        would let duplicates through the moment memory pressure hit, whereas a
        unique constraint cannot be evicted.
        """
        cur = await self.db.execute(
            f"INSERT OR IGNORE INTO messages ({_COLS}, raw_proto) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                m.message_id, m.chat_jid, m.sender_jid, m.sender_name,
                int(m.is_from_me), m.ts, m.type, m.text, m.media_ref,
                json.dumps(m.media_meta) if m.media_meta else None,
                m.quoted_id, m.edited_at, m.revoked_at, m.status, m.status_at,
                m.raw_proto,
            ),
        )
        await self.db.commit()
        return cur.rowcount > 0

    async def apply_edit(self, message_id: str, text: str | None, ts: int) -> None:
        await self.db.execute(
            "UPDATE messages SET text = ?, edited_at = ? WHERE message_id = ?",
            (text, ts, message_id),
        )
        await self.db.commit()

    async def apply_revoke(self, message_id: str, ts: int) -> None:
        # Tombstone rather than delete: the row is history a model may already
        # have cited, and a vanished message reads as a bug.
        await self.db.execute(
            "UPDATE messages SET revoked_at = ?, text = NULL, media_ref = NULL "
            "WHERE message_id = ?",
            (ts, message_id),
        )
        await self.db.commit()

    async def set_media_ref(self, message_id: str, ref: str) -> None:
        await self.db.execute(
            "UPDATE messages SET media_ref = ? WHERE message_id = ?", (ref, message_id)
        )
        await self.db.commit()

    async def set_status(self, message_ids: list[str], status: str,
                         ts: int) -> list[str]:
        if not message_ids:
            return []
        rank = status_rank(status)
        moved = []
        for mid in message_ids:
            row = await (await self.db.execute(
                "SELECT status FROM messages WHERE message_id = ? AND is_from_me = 1",
                (mid,))).fetchone()
            if row is None or status_rank(row["status"]) >= rank:
                continue
            await self.db.execute(
                "UPDATE messages SET status = ?, status_at = ? WHERE message_id = ?",
                (status, ts, mid))
            moved.append(mid)
        if moved:
            await self.db.commit()
        return moved

    async def touch_chat(self, chat_jid: str, ts: int, from_me: bool,
                         preview: str | None) -> None:
        await self.db.execute(
            """INSERT INTO chats (chat_jid, last_message_ts, last_message_text, unread_count)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(chat_jid) DO UPDATE SET
                 last_message_ts   = MAX(COALESCE(chats.last_message_ts, 0), excluded.last_message_ts),
                 last_message_text = excluded.last_message_text,
                 unread_count      = chats.unread_count + ?""",
            (chat_jid, ts, preview, 0 if from_me else 1, 0 if from_me else 1),
        )
        await self.db.commit()

    async def upsert_chat_meta(self, chat_jid: str, *, name: str | None = None,
                               is_group: bool | None = None,
                               archived: bool | None = None,
                               pinned: bool | None = None) -> None:
        sets, args = [], []
        if name is not None:
            sets.append("name = ?"); args.append(name)
        if is_group is not None:
            sets.append("is_group = ?"); args.append(int(is_group))
        if archived is not None:
            sets.append("archived = ?"); args.append(int(archived))
        if pinned is not None:
            sets.append("pinned = ?"); args.append(int(pinned))
        if not sets:
            return
        await self.db.execute(
            "INSERT INTO chats (chat_jid, name, is_group, archived, pinned) "
            "VALUES (?,?,?,?,?) ON CONFLICT(chat_jid) DO UPDATE SET " + ", ".join(sets),
            (chat_jid, name, int(bool(is_group)), int(bool(archived)),
             int(bool(pinned)), *args),
        )
        await self.db.commit()

    async def rebuild_rollups(self) -> int:
        cur = await self.db.execute("""
            UPDATE chats SET
              last_message_ts = (SELECT MAX(m.ts) FROM messages m
                                 WHERE m.chat_jid = chats.chat_jid),
              last_message_text = (SELECT COALESCE(m.text, '[' || m.type || ']')
                                   FROM messages m WHERE m.chat_jid = chats.chat_jid
                                   ORDER BY m.ts DESC LIMIT 1)
            WHERE EXISTS (SELECT 1 FROM messages m WHERE m.chat_jid = chats.chat_jid)
              AND (chats.last_message_ts IS NULL
                   OR chats.last_message_ts < (SELECT MAX(m.ts) FROM messages m
                                               WHERE m.chat_jid = chats.chat_jid))
        """)
        await self.db.commit()
        return cur.rowcount

    async def set_unread(self, chat_jid: str, count: int) -> None:
        """Absolute, not incremental — this is read state arriving from the phone.

        Without it our count only ever climbs, because reading a chat on the
        handset is invisible to us otherwise, and the badges drift away from
        what the user sees on WhatsApp Web.
        """
        await self.db.execute(
            "INSERT INTO chats (chat_jid, unread_count) VALUES (?, ?) "
            "ON CONFLICT(chat_jid) DO UPDATE SET unread_count = excluded.unread_count",
            (chat_jid, max(0, count)),
        )
        await self.db.commit()

    # ----------------------------------------------------------------- reads

    async def list_chats(self, *, limit: int = 30, archived: bool = False,
                         query: str | None = None, kind: str = "all") -> list[Chat]:
        sql = _CHATS_SELECT + " WHERE archived = ?"
        args: list[Any] = [int(archived)]
        if kind == "groups":
            sql += " AND is_group = 1"
        elif kind == "direct":
            sql += " AND is_group = 0"
        elif kind == "unread":
            sql += " AND unread_count > 0"
        if query:
            sql += " AND (name LIKE ? OR chat_jid LIKE ?)"
            args += [f"%{query}%", f"%{query}%"]
        sql += " ORDER BY pinned DESC, last_message_ts DESC NULLS LAST LIMIT ?"
        args.append(limit)
        rows = await (await self.db.execute(sql, args)).fetchall()
        return [_chat(r) for r in rows]

    async def get_chat(self, chat_jid: str) -> Chat | None:
        row = await (await self.db.execute(
            "SELECT * FROM chats WHERE chat_jid = ?", (chat_jid,))).fetchone()
        return _chat(row) if row else None

    async def get_messages(self, chat_jid: str, *, limit: int = 30,
                           before_id: str | None = None,
                           from_ts: int | None = None,
                           to_ts: int | None = None) -> list[Message]:
        sql = f"SELECT {_COLS} FROM messages WHERE chat_jid = ?"
        args: list[Any] = [chat_jid]
        if before_id:
            sql += " AND ts < (SELECT ts FROM messages WHERE message_id = ?)"
            args.append(before_id)
        if from_ts is not None:
            sql += " AND ts >= ?"; args.append(from_ts)
        if to_ts is not None:
            sql += " AND ts <= ?"; args.append(to_ts)
        # id breaks the tie. WhatsApp timestamps are second-resolution — every
        # ts ends in 000 — so two messages sent in the same second compare
        # equal and come back in whatever order the engine feels like. The
        # disclosure and the reply it precedes land in the same second, and the
        # thread showed them the wrong way round against WhatsApp itself.
        sql += " ORDER BY ts DESC, id DESC LIMIT ?"
        args.append(limit)
        rows = await (await self.db.execute(sql, args)).fetchall()
        return [_msg(r) for r in rows]

    async def get_message(self, message_id: str) -> Message | None:
        row = await (await self.db.execute(
            f"SELECT {_COLS} FROM messages WHERE message_id = ?", (message_id,))).fetchone()
        return _msg(row) if row else None

    async def search(self, query: str, *, chat_jid: str | None = None,
                     limit: int = 25, from_ts: int | None = None,
                     to_ts: int | None = None) -> list[Message]:
        """FTS5 MATCH ranked by bm25.

        The query string is passed through, so callers get FTS5 syntax for free:
        "quoted phrases", NOT exclusion, prefix* and OR. That is close enough to
        what Postgres' websearch_to_tsquery accepts that a model writing one
        query for both backends generally gets what it meant.
        """
        include, exclude = split_query(query)
        if not include:
            # FTS5 has no way to say "everything except X"; without a positive
            # term there is nothing to rank against.
            return []
        query = " ".join(include) + ("".join(f" NOT {e}" for e in exclude))

        sql = ("SELECT m.message_id, m.chat_jid, m.sender_jid, m.sender_name, "
               "m.is_from_me, m.ts, m.type, m.text, m.media_ref, m.media_meta, "
               "m.quoted_id, m.edited_at, m.revoked_at "
               "FROM messages_fts f JOIN messages m ON m.id = f.rowid "
               "WHERE messages_fts MATCH ? AND m.revoked_at IS NULL")
        args: list[Any] = [query]
        if chat_jid:
            sql += " AND m.chat_jid = ?"; args.append(chat_jid)
        if from_ts is not None:
            sql += " AND m.ts >= ?"; args.append(from_ts)
        if to_ts is not None:
            sql += " AND m.ts <= ?"; args.append(to_ts)
        sql += " ORDER BY bm25(messages_fts), m.ts DESC LIMIT ?"
        args.append(limit)
        try:
            rows = await (await self.db.execute(sql, args)).fetchall()
        except aiosqlite.OperationalError:
            # FTS5 rejects malformed queries, and the failure modes are varied
            # enough ("fts5: syntax error", "unterminated string", "unknown
            # special query") that matching on the message is a losing game.
            #
            # So retry the whole thing as one quoted literal, which is always
            # valid syntax. A user typing  it's fine  or an unbalanced quote
            # then gets a plain substring search rather than an empty result
            # they cannot explain — degraded, but still an answer.
            args[0] = _as_literal(query)
            try:
                rows = await (await self.db.execute(sql, args)).fetchall()
            except aiosqlite.OperationalError:
                return []
        return [_msg(r) for r in rows]

    async def thread_around(self, message_id: str, radius: int = 15) -> list[Message]:
        """Two bounded scans outward from the anchor.

        Cost stays proportional to `radius` instead of to how busy the chat is,
        which one wide BETWEEN range would not give us.
        """
        anchor = await (await self.db.execute(
            "SELECT chat_jid, ts FROM messages WHERE message_id = ?", (message_id,)
        )).fetchone()
        if anchor is None:
            return []
        before = await (await self.db.execute(
            f"SELECT {_COLS} FROM messages WHERE chat_jid = ? AND ts < ? "
            "ORDER BY ts DESC LIMIT ?", (anchor["chat_jid"], anchor["ts"], radius))).fetchall()
        after = await (await self.db.execute(
            f"SELECT {_COLS} FROM messages WHERE chat_jid = ? AND ts >= ? "
            "ORDER BY ts ASC LIMIT ?", (anchor["chat_jid"], anchor["ts"], radius + 1))).fetchall()
        return [_msg(r) for r in reversed(before)] + [_msg(r) for r in after]

    async def unread_count(self, chat_jid: str | None = None) -> int:
        if chat_jid:
            row = await (await self.db.execute(
                "SELECT unread_count FROM chats WHERE chat_jid = ?", (chat_jid,))).fetchone()
            return int(row["unread_count"]) if row else 0
        row = await (await self.db.execute(
            "SELECT COALESCE(SUM(unread_count),0) AS n FROM chats")).fetchone()
        return int(row["n"])

    # -------------------------------------------------------------------- kv

    async def get_kv(self, key: str) -> dict[str, Any] | None:
        row = await (await self.db.execute(
            "SELECT value FROM kv WHERE key = ?", (key,))).fetchone()
        return json.loads(row["value"]) if row else None

    async def put_kv(self, key: str, value: dict[str, Any]) -> None:
        await self.db.execute(
            "INSERT INTO kv (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, json.dumps(value)),
        )
        await self.db.commit()


def _msg(r: aiosqlite.Row) -> Message:
    return Message(
        message_id=r["message_id"], chat_jid=r["chat_jid"],
        sender_jid=r["sender_jid"], sender_name=r["sender_name"],
        is_from_me=bool(r["is_from_me"]), ts=r["ts"], type=r["type"],
        text=r["text"], media_ref=r["media_ref"],
        media_meta=json.loads(r["media_meta"]) if r["media_meta"] else {},
        quoted_id=r["quoted_id"], edited_at=r["edited_at"], revoked_at=r["revoked_at"],
        status=(r["status"] if "status" in r.keys() else "sent") or "sent",
        status_at=r["status_at"] if "status_at" in r.keys() else None,
    )


# The chat list needs the newest message's status so the sidebar can show the
# same ticks as the thread. Derived here rather than cached on the chat row:
# a receipt lands long after the message did, and the two would drift.
# Both subqueries seek ix_messages_chat_ts(chat_jid, ts DESC), so this costs
# two index lookups per returned row -- a page of chats, never a scan.
_LAST_MESSAGE = """
    (SELECT m.is_from_me FROM messages m
      WHERE m.chat_jid = chats.chat_jid ORDER BY m.ts DESC LIMIT 1) AS last_from_me,
    (SELECT m.status FROM messages m
      WHERE m.chat_jid = chats.chat_jid ORDER BY m.ts DESC LIMIT 1) AS last_status
"""
_CHATS_SELECT = f"SELECT chats.*, {_LAST_MESSAGE} FROM chats"


def _chat(r: aiosqlite.Row) -> Chat:
    return Chat(
        chat_jid=r["chat_jid"], name=r["name"], is_group=bool(r["is_group"]),
        last_message_ts=r["last_message_ts"], last_message_text=r["last_message_text"],
        unread_count=int(r["unread_count"]), archived=bool(r["archived"]),
        pinned=bool(r["pinned"] if "pinned" in r.keys() else 0),
        last_from_me=bool(_opt(r, "last_from_me")),
        last_status=_opt(r, "last_status"),
    )


def _opt(r: aiosqlite.Row, key: str):
    """Columns only the chat-list query selects; other callers pass plain rows."""
    return r[key] if key in r.keys() else None


def _as_literal(query: str) -> str:
    """The query as one FTS5 phrase, which is always parseable.

    FTS5 escapes a double quote by doubling it, so this cannot itself be
    malformed no matter what the user typed.
    """
    return '"' + query.replace('"', '""') + '"'
