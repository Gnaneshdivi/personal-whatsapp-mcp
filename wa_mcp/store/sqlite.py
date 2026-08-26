from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import aiosqlite

from .base import Chat, Message, Store, split_query, status_rank

log = logging.getLogger(__name__)

def _json_meta(meta: dict | None) -> str | None:
    if not meta:
        return None
    try:
        return json.dumps(meta)
    except (TypeError, ValueError):
        return json.dumps(meta, default=repr)


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


    async def connect(self) -> None:
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(self.path)
        self._db.row_factory = aiosqlite.Row
        await self._db.executescript(SCHEMA)
        await self._migrate()
        await self._db.commit()

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

    async def purge(self) -> dict[str, int]:
        counts = {}
        for table in ("messages", "chats", "kv"):
            row = await (await self.db.execute(
                f"SELECT COUNT(*) AS n FROM {table}")).fetchone()
            counts[table] = int(row["n"])
            await self.db.execute(f"DELETE FROM {table}")
        try:
            await self.db.execute(
                "INSERT INTO messages_fts(messages_fts) VALUES('rebuild')")
        except Exception:
            pass
        await self.db.commit()
        return counts

    async def list_kv(self, prefix: str) -> list[str]:
        rows = await (await self.db.execute(
            "SELECT key FROM kv WHERE key LIKE ? ESCAPE '\\'",
            (prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%",)
        )).fetchall()
        return [r["key"] for r in rows]

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    @property
    def db(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("store used before connect()")
        return self._db


    async def upsert_message(self, m: Message) -> bool:
        cur = await self.db.execute(
            f"INSERT OR IGNORE INTO messages ({_COLS}, raw_proto) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                m.message_id, m.chat_jid, m.sender_jid, m.sender_name,
                int(m.is_from_me), m.ts, m.type, m.text, m.media_ref,
                _json_meta(m.media_meta),
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
        await self.db.execute(
            "INSERT INTO chats (chat_jid, unread_count) VALUES (?, ?) "
            "ON CONFLICT(chat_jid) DO UPDATE SET unread_count = excluded.unread_count",
            (chat_jid, max(0, count)),
        )
        await self.db.commit()


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
        include, exclude = split_query(query)
        if not include:
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
            args[0] = _as_literal(query)
            try:
                rows = await (await self.db.execute(sql, args)).fetchall()
            except aiosqlite.OperationalError:
                return []
        return [_msg(r) for r in rows]

    async def thread_around(self, message_id: str, radius: int = 15) -> list[Message]:
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
    return r[key] if key in r.keys() else None


def _as_literal(query: str) -> str:
    return '"' + query.replace('"', '""') + '"'
