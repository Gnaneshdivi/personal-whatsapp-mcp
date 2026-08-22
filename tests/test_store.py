"""Storage contract tests — run against every backend.

Written against the PORT, so the same assertions hold for SQLite, Postgres and
Mongo. That is the whole value of the abstraction: a difference in behaviour
between backends shows up here rather than in production.

Postgres and Mongo are skipped unless a server is reachable:

    WA_TEST_POSTGRES=postgresql://t:t@localhost:55433/t
    WA_TEST_MONGO=mongodb://localhost:57017/wa_test
"""
from __future__ import annotations

import os
import uuid

import pytest

from wa_mcp.store.base import Message, now_ms
from wa_mcp.store.sqlite import SQLiteStore

PG = os.getenv("WA_TEST_POSTGRES", "")
MONGO = os.getenv("WA_TEST_MONGO", "")


@pytest.fixture(params=["sqlite", "postgres", "mongo"])
async def store(request, tmp_path):
    kind = request.param

    if kind == "sqlite":
        s = SQLiteStore(tmp_path / "app.db")
        await s.connect()
        yield s
        await s.close()
        return

    if kind == "postgres":
        if not PG:
            pytest.skip("WA_TEST_POSTGRES not set")
        from wa_mcp.store.postgres import PostgresStore

        # A schema per test, so cases cannot see each other's rows and nothing
        # has to be torn down in the right order.
        schema = "t" + uuid.uuid4().hex[:12]
        s = PostgresStore(PG)
        await s.connect()
        async with s.pool.acquire() as c:
            await c.execute(f'CREATE SCHEMA "{schema}"')
            await c.execute(f'SET search_path TO "{schema}"')
        await s.close()
        s = PostgresStore(PG + f"?options=-csearch_path%3D{schema}")
        await s.connect()
        yield s
        async with s.pool.acquire() as c:
            await c.execute(f'DROP SCHEMA "{schema}" CASCADE')
        await s.close()
        return

    if not MONGO:
        pytest.skip("WA_TEST_MONGO not set")
    from wa_mcp.store.mongo import MongoStore

    s = MongoStore(MONGO.rstrip("/") + "_" + uuid.uuid4().hex[:12])
    await s.connect()
    yield s
    await s._client.drop_database(s.db_name)
    await s.close()


def msg(mid: str, chat: str = "1@s.whatsapp.net", text: str | None = "hi",
        ts: int | None = None, **kw) -> Message:
    return Message(message_id=mid, chat_jid=chat, ts=ts or now_ms(), text=text, **kw)


# ------------------------------------------------------------------- writes

async def test_insert_then_read_back(store):
    assert await store.upsert_message(msg("m1", text="hello world")) is True
    got = await store.get_message("m1")
    assert got is not None and got.text == "hello world"


async def test_duplicate_is_rejected_by_the_index(store):
    """WhatsApp redelivers on reconnect and history sync replays the same ids."""
    assert await store.upsert_message(msg("m1")) is True
    assert await store.upsert_message(msg("m1")) is False
    assert len(await store.get_messages("1@s.whatsapp.net")) == 1


async def test_edit_updates_text_and_marks_it(store):
    await store.upsert_message(msg("m1", text="teh cat"))
    await store.apply_edit("m1", "the cat", now_ms())
    got = await store.get_message("m1")
    assert got.text == "the cat" and got.edited_at is not None


async def test_revoke_tombstones_rather_than_deletes(store):
    """The row is history a model may already have cited."""
    await store.upsert_message(msg("m1", text="oops"))
    await store.apply_revoke("m1", now_ms())
    got = await store.get_message("m1")
    assert got is not None
    assert got.text is None and got.revoked_at is not None


# -------------------------------------------------------------------- chats

async def test_unread_climbs_for_them_not_for_us(store):
    await store.touch_chat("1@s.whatsapp.net", now_ms(), from_me=False, preview="a")
    await store.touch_chat("1@s.whatsapp.net", now_ms(), from_me=False, preview="b")
    await store.touch_chat("1@s.whatsapp.net", now_ms(), from_me=True, preview="c")
    assert await store.unread_count("1@s.whatsapp.net") == 2


async def test_read_state_from_the_phone_overrides_our_count(store):
    """Our count only ever climbs; the handset is the authority."""
    for _ in range(5):
        await store.touch_chat("1@s.whatsapp.net", now_ms(), from_me=False, preview="x")
    assert await store.unread_count("1@s.whatsapp.net") == 5
    await store.set_unread("1@s.whatsapp.net", 0)
    assert await store.unread_count("1@s.whatsapp.net") == 0


async def test_display_name_never_shows_a_bare_jid(store):
    await store.touch_chat("919812345678@s.whatsapp.net", now_ms(), False, "hi")
    chat = (await store.list_chats())[0]
    assert chat.public()["name"] == "919812345678"      # falls back to the number
    await store.upsert_chat_meta("919812345678@s.whatsapp.net", name="Asha")
    chat = (await store.list_chats())[0]
    assert chat.public()["name"] == "Asha"


async def test_chat_meta_does_not_clobber_the_rollup(store):
    ts = now_ms()
    await store.touch_chat("g@g.us", ts, False, "hello")
    await store.upsert_chat_meta("g@g.us", name="Team", is_group=True)
    chat = await store.get_chat("g@g.us")
    assert chat.name == "Team" and chat.is_group
    assert chat.last_message_ts == ts and chat.last_message_text == "hello"


# ------------------------------------------------------------------- search

async def test_search_finds_phrases_and_negation(store):
    await store.upsert_message(msg("m1", text="lets meet at the cafe tomorrow"))
    await store.upsert_message(msg("m2", text="meeting cancelled, no cafe"))
    await store.upsert_message(msg("m3", text="invoice sent yesterday"))

    assert {m.message_id for m in await store.search("cafe")} == {"m1", "m2"}
    # A phrase with no stop words behaves the same everywhere. `"meet at"` does
    # NOT: Postgres drops "at" as an English stop word, collapsing the phrase to
    # one stemmed term that also matches "meeting". That is correct for search
    # quality, so the port does not try to hide it.
    assert [m.message_id for m in await store.search('"cafe tomorrow"')] == ["m1"]
    # Both spellings of exclusion work on every backend — FTS5 wants "NOT x",
    # Postgres and Mongo want "-x", so the port accepts either and translates.
    assert [m.message_id for m in await store.search("cafe NOT cancelled")] == ["m1"]
    assert [m.message_id for m in await store.search("cafe -cancelled")] == ["m1"]


async def test_stemming_is_available_on_every_backend(store):
    """All three stem, so a search for the root finds the inflected form."""
    await store.upsert_message(msg("s1", text="the meeting was long"))
    assert [m.message_id for m in await store.search("meet")] == ["s1"]


async def test_search_index_follows_edits_and_deletes(store):
    """The FTS table is external-content — the triggers are what keep it true."""
    await store.upsert_message(msg("m1", text="original wording"))
    assert len(await store.search("original")) == 1
    await store.apply_edit("m1", "replacement wording", now_ms())
    assert await store.search("original") == []
    assert len(await store.search("replacement")) == 1


async def test_revoked_messages_drop_out_of_search(store):
    await store.upsert_message(msg("m1", text="secret plan"))
    await store.apply_revoke("m1", now_ms())
    assert await store.search("secret") == []


async def test_malformed_query_degrades_to_a_literal_search(store):
    """A stray quote must not crash the tool call — and should still answer."""
    await store.upsert_message(msg("m1", text="hello"))
    assert await store.search('unbalanced "quote') == []       # no match, no crash

    # An apostrophe is the common real case, and it should still find the row.
    await store.upsert_message(msg("m2", text="it's fine"))
    assert [m.message_id for m in await store.search("it's fine")] == ["m2"]


async def test_search_scopes_to_a_chat_and_a_window(store):
    old, new = now_ms() - 90_000_000, now_ms()
    await store.upsert_message(msg("m1", chat="a@s.whatsapp.net", text="budget", ts=old))
    await store.upsert_message(msg("m2", chat="b@s.whatsapp.net", text="budget", ts=new))
    assert [m.message_id for m in await store.search("budget", chat_jid="b@s.whatsapp.net")] == ["m2"]
    assert [m.message_id for m in await store.search("budget", from_ts=new - 1000)] == ["m2"]
    assert [m.message_id for m in await store.search("budget", to_ts=old + 1000)] == ["m1"]


# ---------------------------------------------------------------- paging etc

async def test_get_messages_pages_backwards(store):
    base = now_ms()
    for i in range(5):
        await store.upsert_message(msg(f"m{i}", ts=base + i * 1000, text=f"n{i}"))
    page1 = await store.get_messages("1@s.whatsapp.net", limit=2)
    assert [m.message_id for m in page1] == ["m4", "m3"]
    page2 = await store.get_messages("1@s.whatsapp.net", limit=2, before_id="m3")
    assert [m.message_id for m in page2] == ["m2", "m1"]


async def test_thread_around_returns_both_sides(store):
    base = now_ms()
    for i in range(11):
        await store.upsert_message(msg(f"m{i}", ts=base + i * 1000, text=f"n{i}"))
    thread = await store.thread_around("m5", radius=2)
    assert [m.message_id for m in thread] == ["m3", "m4", "m5", "m6", "m7"]


async def test_time_window_on_get_messages(store):
    base = now_ms()
    for i in range(5):
        await store.upsert_message(msg(f"m{i}", ts=base + i * 1000))
    got = await store.get_messages("1@s.whatsapp.net",
                                   from_ts=base + 1000, to_ts=base + 3000)
    assert [m.message_id for m in got] == ["m3", "m2", "m1"]


async def test_kv_roundtrip(store):
    assert await store.get_kv("settings") is None
    await store.put_kv("settings", {"reply": {"groups": "none"}})
    assert (await store.get_kv("settings"))["reply"]["groups"] == "none"
    await store.put_kv("settings", {"reply": {"groups": "all"}})
    assert (await store.get_kv("settings"))["reply"]["groups"] == "all"


async def test_raw_proto_never_leaks_into_public_output(store):
    await store.upsert_message(msg("m1", raw_proto=b"\x08\x01binary"))
    got = await store.get_message("m1")
    assert "raw_proto" not in got.public()


# ------------------------------------------------------- media round trip

async def test_media_metadata_and_ref_survive_every_backend(store):
    """The media path depends on media_meta surviving storage — it is a dict on
    SQL backends and a subdocument on Mongo, so it gets asserted here."""
    await store.upsert_message(Message(
        "med1", "1@s.whatsapp.net", now_ms(), type="image",
        media_meta={"mime_type": "image/jpeg", "kind": "image", "length": 1234},
        raw_proto=b"\x0a\x04test",
    ))
    got = await store.get_message("med1")
    assert got.media_meta["mime_type"] == "image/jpeg"
    assert got.public()["has_media"] is True
    assert got.public()["media_downloaded"] is False

    await store.set_media_ref("med1", "/tmp/med1.jpg")
    got = await store.get_message("med1")
    assert got.media_ref == "/tmp/med1.jpg"
    assert got.public()["media_downloaded"] is True


async def test_pinned_chats_sort_first(store):
    """As WhatsApp does — pinned above everything, then recency."""
    base = now_ms()
    await store.touch_chat("old@s.whatsapp.net", base, False, "old")
    await store.touch_chat("new@s.whatsapp.net", base + 60_000, False, "new")
    await store.touch_chat("pin@s.whatsapp.net", base - 60_000, False, "pinned but old")
    await store.upsert_chat_meta("pin@s.whatsapp.net", pinned=True)

    order = [c.chat_jid for c in await store.list_chats()]
    assert order[0] == "pin@s.whatsapp.net"
    assert order[1] == "new@s.whatsapp.net"


async def test_pinned_and_archived_round_trip(store):
    await store.touch_chat("c@s.whatsapp.net", now_ms(), False, "hi")
    await store.upsert_chat_meta("c@s.whatsapp.net", pinned=True)
    c = await store.get_chat("c@s.whatsapp.net")
    assert c.pinned is True and c.archived is False
    assert c.public()["pinned"] is True
