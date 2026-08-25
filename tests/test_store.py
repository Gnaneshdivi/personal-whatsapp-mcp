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


async def test_filters_run_in_the_query_not_on_the_page(store):
    """With 30 direct chats newer than every group, a groups filter applied
    AFTER a limit of 10 returns nothing — which reads as missing data rather
    than as a bug. Live: 295 groups existed and the Groups tab showed 4."""
    base = now_ms()
    for i in range(30):
        await store.touch_chat(f"d{i}@s.whatsapp.net", base + i * 1000, False, "hi")
    for i in range(5):
        await store.touch_chat(f"g{i}@g.us", base - (i + 1) * 100_000, False, "hi")
        await store.upsert_chat_meta(f"g{i}@g.us", is_group=True)

    groups = await store.list_chats(limit=10, kind="groups")
    assert len(groups) == 5
    assert all(c.is_group for c in groups)

    direct = await store.list_chats(limit=10, kind="direct")
    assert len(direct) == 10 and not any(c.is_group for c in direct)


async def test_unread_filter(store):
    await store.touch_chat("read@s.whatsapp.net", now_ms(), True, "mine")
    await store.touch_chat("unread@s.whatsapp.net", now_ms(), False, "theirs")
    got = await store.list_chats(kind="unread")
    assert [c.chat_jid for c in got] == ["unread@s.whatsapp.net"]


# ------------------------------------------------------- delivery status

async def test_status_advances_but_never_goes_backwards(store):
    """Receipts arrive out of order and WhatsApp resends them. A DELIVERED
    landing after a READ must not un-read the message."""
    await store.upsert_message(Message("s1", "1@s.whatsapp.net", now_ms(),
                                       is_from_me=True, text="hi"))
    assert await store.set_status(["s1"], "delivered", now_ms()) == ["s1"]
    assert (await store.get_message("s1")).status == "delivered"

    assert await store.set_status(["s1"], "read", now_ms()) == ["s1"]
    assert (await store.get_message("s1")).status == "read"

    # late DELIVERED — ignored, and reported as no change
    assert await store.set_status(["s1"], "delivered", now_ms()) == []
    assert (await store.get_message("s1")).status == "read"


async def test_only_changed_ids_are_returned(store):
    """Callers emit one event per real transition; a resent receipt must not
    produce a second 'they read it' for an agent to act on twice."""
    for i in range(3):
        await store.upsert_message(Message(f"m{i}", "1@s.whatsapp.net", now_ms(),
                                           is_from_me=True, text="x"))
    assert set(await store.set_status(["m0", "m1", "m2"], "read", now_ms())) == {"m0","m1","m2"}
    assert await store.set_status(["m0", "m1", "m2"], "read", now_ms()) == []


async def test_incoming_messages_have_no_status(store):
    """Status is about our own messages; theirs would be meaningless."""
    await store.upsert_message(Message("t1", "1@s.whatsapp.net", now_ms(), text="yo"))
    assert await store.set_status(["t1"], "read", now_ms()) == []
    assert (await store.get_message("t1")).public()["status"] is None


async def test_a_sent_message_starts_at_sent(store):
    await store.upsert_message(Message("s2", "1@s.whatsapp.net", now_ms(),
                                       is_from_me=True, text="hi"))
    m = await store.get_message("s2")
    assert m.status == "sent" and m.public()["status"] == "sent"


async def test_a_new_column_migrates_instead_of_a_rebuild(tmp_path):
    """The alternative was taken once and cost 6,225 messages: the database was
    deleted to pick up a new column, and history sync only runs at pair time so
    it could not be recovered. Additive migration is the difference between an
    upgrade and permanent data loss."""
    import aiosqlite
    from wa_mcp.store.sqlite import SQLiteStore

    path = tmp_path / "old.db"
    # A database from before `status` existed.
    async with aiosqlite.connect(path) as db:
        await db.executescript("""
            CREATE TABLE messages (
                id INTEGER PRIMARY KEY, message_id TEXT NOT NULL UNIQUE,
                chat_jid TEXT NOT NULL, sender_jid TEXT, sender_name TEXT,
                is_from_me INTEGER NOT NULL DEFAULT 0, ts INTEGER NOT NULL,
                type TEXT NOT NULL DEFAULT 'text', text TEXT, media_ref TEXT,
                quoted_id TEXT, edited_at INTEGER, revoked_at INTEGER,
                raw_proto BLOB);
            CREATE TABLE chats (
                chat_jid TEXT PRIMARY KEY, name TEXT,
                is_group INTEGER NOT NULL DEFAULT 0, last_message_ts INTEGER,
                last_message_text TEXT, unread_count INTEGER NOT NULL DEFAULT 0,
                archived INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            INSERT INTO messages (message_id, chat_jid, ts, text, is_from_me)
              VALUES ('old1', '1@s.whatsapp.net', 1000, 'precious', 1);
        """)
        await db.commit()

    store = SQLiteStore(path)
    await store.connect()
    try:
        kept = await store.get_message("old1")
        assert kept is not None and kept.text == "precious", "the row was lost"
        assert kept.status == "sent"
        assert await store.set_status(["old1"], "read", now_ms()) == ["old1"]
    finally:
        await store.close()


# ------------------------------------------------- ticks in the chat list

async def test_chat_list_carries_the_newest_messages_status(store):
    """The sidebar shows the same ticks as the open thread.

    Derived from the messages table on every read rather than cached on the
    chat row. A receipt arrives long after the message it refers to, so a
    stored copy drifts: the thread would show two blue ticks while the list
    beside it still showed one grey one for the same message.
    """
    await store.upsert_message(msg("m1", ts=1000, is_from_me=True))
    await store.touch_chat("1@s.whatsapp.net", 1000, True, "hi")

    (chat,) = [c for c in await store.list_chats(limit=10)
               if c.chat_jid == "1@s.whatsapp.net"]
    assert chat.last_from_me is True
    assert chat.last_status == "sent"

    # The receipt lands. Nothing rewrites the chat row -- the list still moves.
    await store.set_status(["m1"], "read", 2000)
    (chat,) = [c for c in await store.list_chats(limit=10)
               if c.chat_jid == "1@s.whatsapp.net"]
    assert chat.last_status == "read", "the tick did not follow the receipt"


async def test_no_tick_when_the_last_word_was_theirs(store):
    """WhatsApp shows a tick only against your own message.

    Their reply landing after yours must clear it, or the list claims delivery
    of a message that is no longer the one being previewed.
    """
    await store.upsert_message(msg("mine", ts=1000, is_from_me=True))
    await store.upsert_message(msg("theirs", ts=2000, is_from_me=False))
    await store.touch_chat("1@s.whatsapp.net", 2000, False, "their reply")

    (chat,) = [c for c in await store.list_chats(limit=10)
               if c.chat_jid == "1@s.whatsapp.net"]
    assert chat.last_from_me is False
    assert chat.public()["last_status"] is None


async def test_a_chat_with_no_messages_has_no_tick(store):
    """Chats arrive from history sync before any message body does."""
    await store.upsert_chat_meta("2@s.whatsapp.net", name="Empty")
    (chat,) = [c for c in await store.list_chats(limit=10)
               if c.chat_jid == "2@s.whatsapp.net"]
    assert chat.last_from_me is False
    assert chat.last_status is None


async def test_two_messages_in_the_same_second_keep_their_order(store):
    """WhatsApp timestamps are second-resolution — every ts ends in 000.

    Anything sent inside one second compares equal, so without a tie-break the
    order is whatever the engine returns. The disclosure and the reply it
    precedes land in the same second, and the thread showed them reversed
    against WhatsApp itself.
    """
    ts = 1787637108000
    await store.upsert_message(msg("first", ts=ts, text="sent first"))
    await store.upsert_message(msg("second", ts=ts, text="sent second"))

    rows = await store.get_messages("1@s.whatsapp.net", limit=10)
    # Newest first, so the one sent second leads.
    assert [m.text for m in rows] == ["sent second", "sent first"]


async def test_kv_keys_can_be_listed_by_prefix(store):
    """Revoking every issued credential needs enumeration.

    Tokens are one row each, so without this the only way to sign a lost
    connector out is to wait for it to expire.
    """
    for key in ("token.aaa", "token.bbb", "other.ccc",
                "trigger.settings"):
        await store.put_kv(key, {"x": 1})

    assert sorted(await store.list_kv("token.")) == ["token.aaa", "token.bbb"]
    assert len(await store.list_kv("token")) == 2
    assert await store.list_kv("nothing.") == []


async def test_a_prefix_with_a_wildcard_is_not_a_pattern(store):
    """LIKE treats % and _ as wildcards; a key containing one must not match
    everything."""
    await store.put_kv("a%b", {"x": 1})
    await store.put_kv("azzb", {"x": 1})
    assert await store.list_kv("a%b") == ["a%b"]


async def test_purge_removes_everything_including_the_search_index(store):
    """Logging out must leave nothing readable behind.

    The FTS index is external-content, so rows deleted from `messages` can
    still be returned by a search until it is rebuilt — a purge that left
    message text findable would be worse than none, because it looks done.
    """
    for i in range(3):
        await store.upsert_message(msg(f"m{i}", text=f"secret {i}"))
    await store.upsert_chat_meta("1@s.whatsapp.net", name="Someone")
    await store.put_kv("trigger.settings", {"enabled": True})

    counts = await store.purge()
    assert counts and sum(counts.values()) >= 4

    assert await store.search("secret") == []
    assert await store.list_chats(limit=10) == []
    assert await store.get_kv("trigger.settings") is None
    assert await store.get_messages("1@s.whatsapp.net") == []


async def test_purge_leaves_the_store_usable(store):
    """It is not a teardown — the same process pairs again afterwards."""
    await store.upsert_message(msg("m1", text="before"))
    await store.purge()
    await store.upsert_message(msg("m2", text="after"))
    rows = await store.get_messages("1@s.whatsapp.net")
    assert [m.text for m in rows] == ["after"]


async def test_a_push_name_becomes_the_chat_name_when_there_is_none(store):
    """An unsaved number has no address-book entry, so the chat renders as a
    masked number for someone whose name arrives on every message they send."""
    from wa_mcp.whatsapp.contacts import is_placeholder

    await store.upsert_chat_meta("1@s.whatsapp.net", name="+91∙∙∙∙∙∙∙∙88")
    chat = await store.get_chat("1@s.whatsapp.net")
    assert is_placeholder(chat.name)

    await store.upsert_chat_meta("1@s.whatsapp.net", name="Asif")
    chat = await store.get_chat("1@s.whatsapp.net")
    assert chat.name == "Asif"

    # And it is findable by that name, which is the point.
    found = await store.list_chats(limit=10, query="asif")
    assert [c.chat_jid for c in found] == ["1@s.whatsapp.net"]
