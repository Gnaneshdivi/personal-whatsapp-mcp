from __future__ import annotations

import pytest

from wa_mcp.config import Settings, resolve_storage


@pytest.fixture
async def rt(tmp_path, monkeypatch):
    monkeypatch.delenv("WA_DATABASE_URL", raising=False)
    from wa_mcp.runtime import Runtime

    r = Runtime(Settings(auth_token="t"), resolve_storage("", tmp_path))
    await r.store.connect()

    import wa_mcp.app as A

    monkeypatch.setattr(A, "RT", r)
    yield r
    await r.store.close()


async def add(rt, jid, name, is_group=False):
    await rt.store.upsert_chat_meta(jid, name=name, is_group=is_group)


async def test_an_exact_name_beats_chats_that_merely_contain_it(rt):
    from wa_mcp.app import _resolve_chat

    await add(rt, "1@s.whatsapp.net", "Dad")
    await add(rt, "2@s.whatsapp.net", "Dad's office")
    await add(rt, "3@s.whatsapp.net", "Grandad")

    assert await _resolve_chat("Dad") == "1@s.whatsapp.net"
    assert await _resolve_chat("dad") == "1@s.whatsapp.net"


async def test_two_people_with_the_same_name_must_be_asked_about(rt):
    from wa_mcp.app import ToolError, _resolve_chat

    await add(rt, "1@s.whatsapp.net", "Surya SRM")
    await add(rt, "2@s.whatsapp.net", "Surya SRM")

    with pytest.raises(ToolError) as e:
        await _resolve_chat("Surya SRM")
    msg = str(e.value)
    assert "1@s.whatsapp.net" in msg and "2@s.whatsapp.net" in msg
    assert "ASK which one is meant" in msg, "the model is not told to ask"


async def test_an_ambiguous_partial_name_still_refuses(rt):
    from wa_mcp.app import ToolError, _resolve_chat

    await add(rt, "1@s.whatsapp.net", "Ravi Kumar")
    await add(rt, "2@s.whatsapp.net", "Ravi Shankar")

    with pytest.raises(ToolError) as e:
        await _resolve_chat("Ravi")
    assert "2 chats or contacts match" in str(e.value)


async def test_a_duplicate_listing_is_narrowed_to_the_real_tie(rt):
    from wa_mcp.app import ToolError, _resolve_chat

    await add(rt, "1@s.whatsapp.net", "Trip")
    await add(rt, "2@s.whatsapp.net", "Trip")
    await add(rt, "3@s.whatsapp.net", "Trip to Goa")
    await add(rt, "4@s.whatsapp.net", "Office Trip 2026")

    with pytest.raises(ToolError) as e:
        await _resolve_chat("Trip")
    msg = str(e.value)
    assert "2 chats or contacts match" in msg, msg
    assert "Trip to Goa" not in msg


async def test_a_jid_or_number_is_never_name_matched(rt):
    from wa_mcp.app import _resolve_chat

    await add(rt, "1@s.whatsapp.net", "Dad")
    assert await _resolve_chat("919812345678") == "919812345678@s.whatsapp.net"
    assert await _resolve_chat("+91 98123 45678") == "919812345678@s.whatsapp.net"
    assert await _resolve_chat("5@s.whatsapp.net") == "5@s.whatsapp.net"


async def test_an_unknown_name_says_so(rt):
    from wa_mcp.app import ToolError, _resolve_chat

    await add(rt, "1@s.whatsapp.net", "Dad")
    with pytest.raises(ToolError) as e:
        await _resolve_chat("Nobody")
    assert "no chat or contact matching" in str(e.value)


async def test_someone_never_messaged_can_still_be_found(rt):
    from wa_mcp.app import _resolve_chat

    rt.contacts._names = {"918056088288@s.whatsapp.net": "Akbar Ktr Srm"}
    assert await _resolve_chat("Akbar Ktr Srm") == "918056088288@s.whatsapp.net"
    assert await _resolve_chat("akbar ktr srm") == "918056088288@s.whatsapp.net"


async def test_a_chat_match_does_not_hide_other_contacts(rt):
    from wa_mcp.app import ToolError, _resolve_chat

    await add(rt, "1@s.whatsapp.net", "Asif Akbar Brother")
    rt.contacts._names = {"2@s.whatsapp.net": "Akbar Ktr Srm",
                          "3@s.whatsapp.net": "Akbar Baig"}
    with pytest.raises(ToolError) as e:
        await _resolve_chat("akbar")
    msg = str(e.value)
    assert "3 chats or contacts match" in msg
    for name in ("Asif Akbar Brother", "Akbar Ktr Srm", "Akbar Baig"):
        assert name in msg


async def test_an_exact_contact_name_still_wins(rt):
    from wa_mcp.app import _resolve_chat

    await add(rt, "1@s.whatsapp.net", "Akbar Ktr Srm office")
    rt.contacts._names = {"2@s.whatsapp.net": "Akbar Ktr Srm"}
    assert await _resolve_chat("Akbar Ktr Srm") == "2@s.whatsapp.net"


async def test_a_contact_is_not_listed_twice_when_it_also_has_a_chat(rt):
    from wa_mcp.app import _resolve_chat

    await add(rt, "1@s.whatsapp.net", "Dad")
    rt.contacts._names = {"1@s.whatsapp.net": "Dad"}
    assert await _resolve_chat("Dad") == "1@s.whatsapp.net"


def test_a_masked_number_is_not_a_name():
    from wa_mcp.whatsapp.contacts import is_placeholder

    assert is_placeholder("+91∙∙∙∙∙∙∙∙88")
    assert is_placeholder("919812345678")
    assert is_placeholder("+91 98123 45678")
    assert is_placeholder("") and is_placeholder(None)
    assert not is_placeholder("Akbar Ktr Srm")


def test_the_address_book_beats_a_masked_chat_name():
    from wa_mcp.whatsapp.contacts import ContactBook

    b = ContactBook("/nonexistent")
    b._names = {"918056088288@s.whatsapp.net": "Akbar Ktr Srm"}
    masked = "+91∙∙∙∙∙∙∙∙88"
    assert b.display_name("918056088288@s.whatsapp.net",
                          chat_name=masked) == "Akbar Ktr Srm"


def test_a_masked_name_is_still_better_than_nothing():
    from wa_mcp.whatsapp.contacts import ContactBook

    b = ContactBook("/nonexistent")
    masked = "+91∙∙∙∙∙∙∙∙88"
    assert b.display_name("918056088288@s.whatsapp.net",
                          chat_name=masked) == masked


async def test_a_chat_named_only_by_a_mask_is_still_searchable(rt):
    from wa_mcp.app import ToolError, _resolve_chat

    await add(rt, "918056088288@s.whatsapp.net",
              "+91∙∙∙∙∙∙∙∙88")
    await add(rt, "919705179198@s.whatsapp.net", "Asif Akbar Brother")
    rt.contacts._names = {"918056088288@s.whatsapp.net": "Akbar Ktr Srm"}

    assert await _resolve_chat("Akbar Ktr Srm") == "918056088288@s.whatsapp.net"
    with pytest.raises(ToolError) as e:
        await _resolve_chat("akbar")
    assert "2 chats or contacts match" in str(e.value)
