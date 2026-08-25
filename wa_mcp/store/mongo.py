"""MongoDB backend.

For people whose platform is already on Mongo. Two things behave differently
here and both are worth knowing before choosing it:

**Search is weaker.** Mongo's `$text` handles quoted phrases and `-exclusion`,
but a collection may carry only one text index and `textScore` ranks more
crudely than `ts_rank` or `bm25`. Good enough to find a message; not as good at
putting the most relevant one first.

**The chat rollup is not transactional.** Multi-document transactions need a
replica set, and plenty of people run a standalone node. Rather than require
one, `touch_chat` is written so a torn write self-heals: the timestamp uses
`$max` and the preview is only overwritten by a newer message, so replaying the
same message is a no-op and losing the rollup update costs a stale preview
until the next message arrives — never a wrong unread count.
"""
from __future__ import annotations

import logging
from typing import Any
from urllib.parse import urlparse

from .base import Chat, Message, Store, split_query, status_rank

log = logging.getLogger(__name__)


class MongoStore(Store):
    def __init__(self, url: str):
        self.url = url
        name = (urlparse(url).path or "").lstrip("/")
        self.db_name = name or "suprai_whatsapp"
        self._client = None
        self.db = None

    async def connect(self) -> None:
        from motor.motor_asyncio import AsyncIOMotorClient

        self._client = AsyncIOMotorClient(self.url, serverSelectionTimeoutMS=5000)
        self.db = self._client[self.db_name]

        await self.db.messages.create_index("message_id", unique=True)
        await self.db.messages.create_index([("chat_jid", 1), ("ts", -1)])
        await self.db.messages.create_index([("ts", -1)])
        # One text index per collection, so it covers the only field worth
        # searching. Named explicitly to make it obvious in `getIndexes`.
        try:
            await self.db.messages.create_index([("text", "text")], name="text_search")
        except Exception as exc:               # already present with a different spec
            log.debug("text index: %s", exc)
        await self.db.chats.create_index("chat_jid", unique=True)
        await self.db.chats.create_index([("last_message_ts", -1)])
        await self.db.kv.create_index("key", unique=True)

    async def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    # ---------------------------------------------------------------- writes

    async def upsert_message(self, m: Message) -> bool:
        from pymongo.errors import DuplicateKeyError

        doc = {
            "message_id": m.message_id, "chat_jid": m.chat_jid,
            "sender_jid": m.sender_jid, "sender_name": m.sender_name,
            "is_from_me": bool(m.is_from_me), "ts": int(m.ts), "type": m.type,
            "text": m.text, "media_ref": m.media_ref, "media_meta": m.media_meta or {},
            "quoted_id": m.quoted_id, "edited_at": m.edited_at,
            "revoked_at": m.revoked_at, "status": m.status,
            "status_at": m.status_at, "raw_proto": m.raw_proto,
        }
        try:
            await self.db.messages.insert_one(doc)
            return True
        except DuplicateKeyError:
            # Idempotency is the unique index, exactly as on the SQL backends.
            return False

    async def apply_edit(self, message_id: str, text: str | None, ts: int) -> None:
        await self.db.messages.update_one(
            {"message_id": message_id}, {"$set": {"text": text, "edited_at": ts}})

    async def apply_revoke(self, message_id: str, ts: int) -> None:
        await self.db.messages.update_one(
            {"message_id": message_id},
            {"$set": {"revoked_at": ts, "text": None, "media_ref": None}})

    async def set_media_ref(self, message_id: str, ref: str) -> None:
        await self.db.messages.update_one(
            {"message_id": message_id}, {"$set": {"media_ref": ref}})

    async def set_status(self, message_ids: list[str], status: str,
                         ts: int) -> list[str]:
        if not message_ids:
            return []
        lower = ["sent", "delivered", "read", "played"][:status_rank(status)]
        moved = []
        for mid in message_ids:
            r = await self.db.messages.update_one(
                {"message_id": mid, "is_from_me": True,
                 "$or": [{"status": {"$in": lower}}, {"status": {"$exists": False}}]},
                {"$set": {"status": status, "status_at": ts}})
            if r.modified_count:
                moved.append(mid)
        return moved

    async def touch_chat(self, chat_jid: str, ts: int, from_me: bool,
                         preview: str | None) -> None:
        """Written to be safe without a transaction — see the module docstring."""
        update: dict[str, Any] = {
            "$max": {"last_message_ts": int(ts)},
            "$inc": {"unread_count": 0 if from_me else 1},
            "$setOnInsert": {"chat_jid": chat_jid, "is_group": False, "archived": False},
        }
        await self.db.chats.update_one({"chat_jid": chat_jid}, update, upsert=True)
        # Only a newer message may replace the preview, so an out-of-order
        # delivery cannot make the chat list show an older line.
        await self.db.chats.update_one(
            {"chat_jid": chat_jid, "last_message_ts": {"$lte": int(ts)}},
            {"$set": {"last_message_text": preview}})

    async def upsert_chat_meta(self, chat_jid: str, *, name: str | None = None,
                               is_group: bool | None = None,
                               archived: bool | None = None,
                               pinned: bool | None = None) -> None:
        sets: dict[str, Any] = {}
        if name is not None:
            sets["name"] = name
        if is_group is not None:
            sets["is_group"] = bool(is_group)
        if archived is not None:
            sets["archived"] = bool(archived)
        if pinned is not None:
            sets["pinned"] = bool(pinned)
        if not sets:
            return
        await self.db.chats.update_one(
            {"chat_jid": chat_jid},
            {"$set": sets, "$setOnInsert": {"chat_jid": chat_jid, "unread_count": 0}},
            upsert=True)

    async def rebuild_rollups(self) -> int:
        n = 0
        pipeline = [{"$sort": {"chat_jid": 1, "ts": -1}},
                    {"$group": {"_id": "$chat_jid", "mx": {"$first": "$ts"},
                                "txt": {"$first": "$text"},
                                "typ": {"$first": "$type"}}}]
        async for row in self.db.messages.aggregate(pipeline):
            r = await self.db.chats.update_one(
                {"chat_jid": row["_id"],
                 "$or": [{"last_message_ts": None},
                         {"last_message_ts": {"$lt": row["mx"]}},
                         {"last_message_ts": {"$exists": False}}]},
                {"$set": {"last_message_ts": row["mx"],
                          "last_message_text": row.get("txt") or f"[{row.get('typ')}]"},
                 "$setOnInsert": {"chat_jid": row["_id"]}}, upsert=True)
            n += r.modified_count
        return n

    async def set_unread(self, chat_jid: str, count: int) -> None:
        await self.db.chats.update_one(
            {"chat_jid": chat_jid},
            {"$set": {"unread_count": max(0, int(count))},
             "$setOnInsert": {"chat_jid": chat_jid}},
            upsert=True)

    # ----------------------------------------------------------------- reads

    async def list_chats(self, *, limit: int = 30, archived: bool = False,
                         query: str | None = None, kind: str = "all") -> list[Chat]:
        q: dict[str, Any] = {"archived": bool(archived)}
        if kind == "groups":
            q["is_group"] = True
        elif kind == "direct":
            q["is_group"] = False
        elif kind == "unread":
            q["unread_count"] = {"$gt": 0}
        if query:
            rx = {"$regex": query, "$options": "i"}
            q["$or"] = [{"name": rx}, {"chat_jid": rx}]
        cur = (self.db.chats.find(q)
               .sort([("pinned", -1), ("last_message_ts", -1)]).limit(limit))
        chats = [_chat(d) async for d in cur]
        await self._attach_last_status(chats)
        return chats

    async def _attach_last_status(self, chats: list[Chat]) -> None:
        """Fill in the newest message's status for a page of chats.

        The sidebar shows the same ticks as the thread, and this is derived
        rather than cached on the chat document because a receipt arrives long
        after the message did -- a stored copy would sit stale showing one grey
        tick beside a conversation that has been read.

        One aggregation for the whole page rather than a lookup per chat: the
        sort hits the same (chat_jid, ts) index the message list uses, and
        $first after it is just the newest row in each group.
        """
        if not chats:
            return
        jids = [c.chat_jid for c in chats]
        cur = self.db.messages.aggregate([
            {"$match": {"chat_jid": {"$in": jids}}},
            {"$sort": {"chat_jid": 1, "ts": -1}},
            {"$group": {"_id": "$chat_jid",
                        "is_from_me": {"$first": "$is_from_me"},
                        "status": {"$first": "$status"}}},
        ])
        newest = {d["_id"]: d async for d in cur}
        for c in chats:
            d = newest.get(c.chat_jid)
            if d:
                c.last_from_me = bool(d.get("is_from_me"))
                c.last_status = d.get("status")

    async def get_chat(self, chat_jid: str) -> Chat | None:
        d = await self.db.chats.find_one({"chat_jid": chat_jid})
        return _chat(d) if d else None

    async def get_messages(self, chat_jid: str, *, limit: int = 30,
                           before_id: str | None = None, from_ts: int | None = None,
                           to_ts: int | None = None) -> list[Message]:
        q: dict[str, Any] = {"chat_jid": chat_jid}
        ts: dict[str, Any] = {}
        if before_id:
            anchor = await self.db.messages.find_one({"message_id": before_id}, {"ts": 1})
            if anchor:
                ts["$lt"] = anchor["ts"]
        if from_ts is not None:
            ts["$gte"] = from_ts
        if to_ts is not None:
            ts["$lte"] = to_ts
        if ts:
            q["ts"] = ts
        # _id is monotonic per insert, so it breaks the second-resolution tie
        # the same way id does on the SQL backends.
        cur = self.db.messages.find(q).sort([("ts", -1), ("_id", -1)]).limit(limit)
        return [_msg(d) async for d in cur]

    async def get_message(self, message_id: str) -> Message | None:
        d = await self.db.messages.find_one({"message_id": message_id})
        return _msg(d) if d else None

    async def search(self, query: str, *, chat_jid: str | None = None, limit: int = 25,
                     from_ts: int | None = None, to_ts: int | None = None) -> list[Message]:
        include, exclude = split_query(query)
        query = " ".join(include) + "".join(f" -{e.strip(chr(34))}" for e in exclude)
        q: dict[str, Any] = {"$text": {"$search": query}, "revoked_at": None}
        if chat_jid:
            q["chat_jid"] = chat_jid
        ts: dict[str, Any] = {}
        if from_ts is not None:
            ts["$gte"] = from_ts
        if to_ts is not None:
            ts["$lte"] = to_ts
        if ts:
            q["ts"] = ts
        try:
            cur = (self.db.messages
                   .find(q, {"score": {"$meta": "textScore"}})
                   .sort([("score", {"$meta": "textScore"}), ("ts", -1)])
                   .limit(limit))
            return [_msg(d) async for d in cur]
        except Exception as exc:
            # A malformed $text query is an operational error here rather than a
            # parse error; return nothing rather than failing the tool call, as
            # the SQLite path does.
            log.debug("mongo search failed: %s", exc)
            return []

    async def thread_around(self, message_id: str, radius: int = 15) -> list[Message]:
        anchor = await self.db.messages.find_one({"message_id": message_id})
        if anchor is None:
            return []
        before = [d async for d in self.db.messages
                  .find({"chat_jid": anchor["chat_jid"], "ts": {"$lt": anchor["ts"]}})
                  .sort("ts", -1).limit(radius)]
        after = [d async for d in self.db.messages
                 .find({"chat_jid": anchor["chat_jid"], "ts": {"$gte": anchor["ts"]}})
                 .sort("ts", 1).limit(radius + 1)]
        return [_msg(d) for d in reversed(before)] + [_msg(d) for d in after]

    async def unread_count(self, chat_jid: str | None = None) -> int:
        if chat_jid:
            d = await self.db.chats.find_one({"chat_jid": chat_jid}, {"unread_count": 1})
            return int((d or {}).get("unread_count", 0))
        agg = self.db.chats.aggregate(
            [{"$group": {"_id": None, "n": {"$sum": "$unread_count"}}}])
        async for row in agg:
            return int(row.get("n", 0))
        return 0

    # -------------------------------------------------------------------- kv

    async def purge(self) -> dict[str, int]:
        counts = {}
        for name in ("messages", "chats", "kv"):
            coll = self.db[name]
            counts[name] = await coll.count_documents({})
            await coll.delete_many({})
        return counts

    async def list_kv(self, prefix: str) -> list[str]:
        import re as _re

        cur = self.db.kv.find({"_id": {"$regex": "^" + _re.escape(prefix)}},
                              {"_id": 1})
        return [d["_id"] async for d in cur]

    async def get_kv(self, key: str) -> dict[str, Any] | None:
        d = await self.db.kv.find_one({"key": key})
        return d.get("value") if d else None

    async def put_kv(self, key: str, value: dict[str, Any]) -> None:
        await self.db.kv.update_one({"key": key}, {"$set": {"value": value}}, upsert=True)


def _msg(d: dict) -> Message:
    return Message(
        message_id=d["message_id"], chat_jid=d["chat_jid"], sender_jid=d.get("sender_jid"),
        sender_name=d.get("sender_name"), is_from_me=bool(d.get("is_from_me")),
        ts=int(d["ts"]), type=d.get("type", "text"), text=d.get("text"),
        media_ref=d.get("media_ref"), media_meta=d.get("media_meta") or {},
        quoted_id=d.get("quoted_id"), edited_at=d.get("edited_at"),
        revoked_at=d.get("revoked_at"),
        status=d.get("status") or "sent", status_at=d.get("status_at"),
    )


def _chat(d: dict) -> Chat:
    return Chat(
        chat_jid=d["chat_jid"], name=d.get("name"), is_group=bool(d.get("is_group")),
        last_message_ts=d.get("last_message_ts"),
        last_message_text=d.get("last_message_text"),
        unread_count=int(d.get("unread_count", 0)), archived=bool(d.get("archived")),
        pinned=bool(d.get("pinned")),
    )
