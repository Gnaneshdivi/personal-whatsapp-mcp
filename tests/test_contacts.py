"""Contact names — the fix for 0 of 100 chats rendering as raw JIDs."""
from __future__ import annotations

import sqlite3

import pytest

from wa_mcp.whatsapp.contacts import ContactBook


@pytest.fixture
def session_db(tmp_path):
    """A stand-in for whatsmeow's own session store."""
    path = tmp_path / "session.db"
    db = sqlite3.connect(path)
    db.executescript("""
        CREATE TABLE whatsmeow_contacts (
            our_jid TEXT, their_jid TEXT,
            first_name TEXT, full_name TEXT, push_name TEXT, business_name TEXT
        );
        INSERT INTO whatsmeow_contacts VALUES
          ('me', '919812345678@s.whatsapp.net', 'Asha', 'Asha Menon', 'ash', ''),
          ('me', '919887654321:3@s.whatsapp.net', '', '', 'Ravi K', ''),
          ('me', '919000000001@s.whatsapp.net', 'Only', '', '', ''),
          ('me', '919000000002@s.whatsapp.net', '', '', '', 'Momo Cafe'),
          ('me', '919000000003@s.whatsapp.net', '', '', '', '');
    """)
    db.commit()
    db.close()
    return path


async def test_loads_names_from_whatsmeows_own_table(session_db):
    book = ContactBook(str(session_db))
    assert await book.load() == 4          # the all-blank row is skipped
    assert book.loaded is True
    assert book.get("919812345678@s.whatsapp.net") == "Asha Menon"


async def test_name_preference_order(session_db):
    book = ContactBook(str(session_db))
    await book.load()
    assert book.get("919812345678@s.whatsapp.net") == "Asha Menon"   # full_name wins
    assert book.get("919887654321@s.whatsapp.net") == "Ravi K"       # push_name
    assert book.get("919000000001@s.whatsapp.net") == "Only"         # first_name
    assert book.get("919000000002@s.whatsapp.net") == "Momo Cafe"    # business_name


async def test_device_suffix_in_the_contact_row_still_matches(session_db):
    """Contacts are stored per-device; lookups come in as plain chat JIDs."""
    book = ContactBook(str(session_db))
    await book.load()
    assert book.get("919887654321@s.whatsapp.net") == "Ravi K"
    assert book.get("919887654321:9@s.whatsapp.net") == "Ravi K"


# ------------------------------------------------------------ fallback chain

async def test_display_name_prefers_an_explicit_chat_name(session_db):
    """Group names and user overrides beat the address book."""
    book = ContactBook(str(session_db))
    await book.load()
    assert book.display_name("919812345678@s.whatsapp.net", chat_name="Work") == "Work"


async def test_display_name_falls_back_to_push_name_for_strangers(session_db):
    book = ContactBook(str(session_db))
    await book.load()
    name = book.display_name("919999999999@s.whatsapp.net", push_name="Unknown Caller")
    assert name == "Unknown Caller"


async def test_display_name_falls_back_to_the_number(session_db):
    book = ContactBook(str(session_db))
    await book.load()
    assert book.display_name("919999999999@s.whatsapp.net") == "919999999999"


async def test_a_lid_with_no_name_shows_the_jid_not_a_blank(session_db):
    """LIDs carry no phone number, so the identifier is all we have."""
    book = ContactBook(str(session_db))
    await book.load()
    assert book.display_name("207696196305131@lid") == "207696196305131@lid"


# --------------------------------------------------------------- robustness

async def test_a_missing_session_db_is_survivable(tmp_path):
    """Fresh install: the table does not exist until whatsmeow has connected."""
    book = ContactBook(str(tmp_path / "nope.db"))
    assert await book.load() == 0
    assert book.loaded is False
    assert book.error is not None
    # and it still names people from message push_names
    assert book.display_name("919812345678@s.whatsapp.net", push_name="Asha") == "Asha"
    assert book.display_name("919812345678@s.whatsapp.net") == "919812345678"


async def test_a_renamed_table_degrades_instead_of_raising(tmp_path):
    """If a whatsmeow upgrade changes the schema, chats must still work."""
    path = tmp_path / "session.db"
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE something_else (x TEXT)")
    db.commit(); db.close()

    book = ContactBook(str(path))
    assert await book.load() == 0
    assert book.error is not None
    assert book.display_name("1@s.whatsapp.net", push_name="Live Name") == "Live Name"


async def test_load_is_repeatable(session_db):
    book = ContactBook(str(session_db))
    await book.load()
    assert await book.load() == 4


async def test_an_empty_book_is_not_mistaken_for_a_missing_one(tmp_path):
    """ContactBook defines __len__, so an empty one is falsy. Any caller using
    `contacts or ContactBook(...)` silently builds a SECOND book — the client
    then loads names into an object nobody else holds, and every chat renders
    as a phone number while the log reports the contacts loaded fine."""
    from wa_mcp.config import Settings
    from wa_mcp.store.sqlite import SQLiteStore
    from wa_mcp.whatsapp.client import WhatsApp

    book = ContactBook(str(tmp_path / "session.db"))
    assert len(book) == 0
    assert not book                      # falsy — this is the trap

    wa = WhatsApp(session_dsn=str(tmp_path / "session.db"),
                  store=SQLiteStore(tmp_path / "app.db"),
                  settings=Settings(), contacts=book)
    assert wa.contacts is book, "the client built its own book instead"
