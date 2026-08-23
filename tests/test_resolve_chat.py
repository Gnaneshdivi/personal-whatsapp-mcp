"""Resolving a name to a chat.

Sending to the wrong person is not recoverable, so this is one of the few
places where refusing to act is clearly better than acting on a guess. Two
distinct failures show up on a real account: 12 names shared by more than one
chat, and 18 names that are a substring of some other name — "Dad" also
matching "Dad's office", which made it impossible to send to Dad at all.
"""
from __future__ import annotations

import pytest

from wa_mcp.config import Settings, resolve_storage


@pytest.fixture
async def rt(tmp_path, monkeypatch):
    monkeypatch.delenv("WA_DATABASE_URL", raising=False)
    from wa_mcp.runtime import Runtime

    r = Runtime(Settings(auth_token="t", oauth=False), resolve_storage("", tmp_path))
    await r.store.connect()

    import wa_mcp.app as A

    monkeypatch.setattr(A, "RT", r)
    yield r
    await r.store.close()


async def add(rt, jid, name, is_group=False):
    await rt.store.upsert_chat_meta(jid, name=name, is_group=is_group)


async def test_an_exact_name_beats_chats_that_merely_contain_it(rt):
    """The 'Dad' case: unambiguous to a person, ambiguous to a substring match."""
    from wa_mcp.app import _resolve_chat

    await add(rt, "1@s.whatsapp.net", "Dad")
    await add(rt, "2@s.whatsapp.net", "Dad's office")
    await add(rt, "3@s.whatsapp.net", "Grandad")

    assert await _resolve_chat("Dad") == "1@s.whatsapp.net"
    assert await _resolve_chat("dad") == "1@s.whatsapp.net"     # case-insensitive


async def test_two_people_with_the_same_name_must_be_asked_about(rt):
    """No exact match can break this tie, so it must not be broken silently."""
    from wa_mcp.app import ToolError, _resolve_chat

    await add(rt, "1@s.whatsapp.net", "Surya SRM")
    await add(rt, "2@s.whatsapp.net", "Surya SRM")

    with pytest.raises(ToolError) as e:
        await _resolve_chat("Surya SRM")
    msg = str(e.value)
    assert "1@s.whatsapp.net" in msg and "2@s.whatsapp.net" in msg
    assert "ASK which one" in msg, "the model is not told to ask"


async def test_an_ambiguous_partial_name_still_refuses(rt):
    from wa_mcp.app import ToolError, _resolve_chat

    await add(rt, "1@s.whatsapp.net", "Ravi Kumar")
    await add(rt, "2@s.whatsapp.net", "Ravi Shankar")

    with pytest.raises(ToolError) as e:
        await _resolve_chat("Ravi")
    assert "2 chats match" in str(e.value)


async def test_a_duplicate_listing_is_narrowed_to_the_real_tie(rt):
    """Exact duplicates should not be listed alongside irrelevant near-misses."""
    from wa_mcp.app import ToolError, _resolve_chat

    await add(rt, "1@s.whatsapp.net", "Trip")
    await add(rt, "2@s.whatsapp.net", "Trip")
    await add(rt, "3@s.whatsapp.net", "Trip to Goa")
    await add(rt, "4@s.whatsapp.net", "Office Trip 2026")

    with pytest.raises(ToolError) as e:
        await _resolve_chat("Trip")
    msg = str(e.value)
    assert "2 chats match" in msg, msg
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
    assert "no chat matching" in str(e.value)
