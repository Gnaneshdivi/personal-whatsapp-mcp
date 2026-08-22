"""Delivery tokens — the boundary that is not asked of the model.

Fire-and-forget hands a message written by a stranger to an agent holding this
connector, which can otherwise reach every conversation on the account. The
prompt tells that agent to reply only in the chat it came from; prompt
injection is the technique for talking a model out of exactly such an
instruction, so the sentence cannot be the control.

These pin the control that does not depend on the model's judgement: what a
delivery token can reach, and what a full token still can.
"""
from __future__ import annotations

import time

import pytest

from wa_mcp.delivery import REPLY_TOOLS, load, mint, refusal

CHAT = "918790892969@s.whatsapp.net"
OTHER = "919999999999@s.whatsapp.net"


@pytest.fixture
async def store(tmp_path):
    from wa_mcp.store.sqlite import SQLiteStore

    s = SQLiteStore(tmp_path / "app.db")
    await s.connect()
    yield s
    await s.close()


def rec(chat=CHAT):
    return {"delivery_chat": chat, "scopes": ["reply"]}


# ------------------------------------------------------------ the refusals

def test_it_can_reply_in_the_chat_it_was_issued_for():
    assert refusal(rec(), "tools/call", "wa_send", {"to": CHAT}) is None


def test_it_cannot_message_a_different_number():
    """The attack: an injected instruction to forward somewhere.

    It arrives as a well-formed call. The only thing separating it from a
    legitimate reply is the destination, so that is what gets checked.
    """
    why = refusal(rec(), "tools/call", "wa_send", {"to": OTHER})
    assert why and OTHER in why


def test_a_device_suffix_does_not_slip_past_the_check():
    """JIDs arrive as 9187…:9@s.whatsapp.net; comparing raw would let it by."""
    assert refusal(rec(), "tools/call", "wa_send",
                   {"to": "918790892969:9@s.whatsapp.net"}) is None


@pytest.mark.parametrize("tool", [
    "wa_list_chats", "wa_search", "wa_get_messages", "wa_get_thread",
    "wa_download_media", "wa_list_groups", "wa_group_info", "wa_unread",
    "wa_set_reply_settings", "wa_logout", "wa_pair",
])
def test_it_cannot_read_or_change_anything(tool):
    """Exfiltration needs a read. None of them are available."""
    assert refusal(rec(), "tools/call", tool, {}) is not None


def test_the_handshake_still_works():
    for method in ("initialize", "tools/list", "ping"):
        assert refusal(rec(), method, "", None) is None


def test_a_full_token_is_never_restricted():
    """Your own connector keeps every tool and every chat."""
    for tool in ("wa_list_chats", "wa_search", "wa_logout"):
        assert refusal({"scopes": []}, "tools/call", tool, {}) is None
    assert refusal(None, "tools/call", "wa_send", {"to": OTHER}) is None


def test_a_send_with_no_destination_is_refused():
    assert refusal(rec(), "tools/call", "wa_send", {}) is not None


def test_reply_tools_are_only_the_ones_that_name_a_destination():
    """Confinement is checkable because each of these takes `to`."""
    assert set(REPLY_TOOLS) == {"wa_send", "wa_send_media", "wa_typing"}


# ------------------------------------------------------------ the lifetime

async def test_a_minted_token_loads_and_is_scoped(store):
    token = await mint(store, CHAT, 300)
    got = await load(store, token)
    assert got["delivery_chat"] == CHAT
    assert refusal(got, "tools/call", "wa_send", {"to": OTHER}) is not None


async def test_an_expired_token_is_gone(store):
    token = await mint(store, CHAT, 300)
    raw = await store.get_kv(f"oauth.token.{token}")
    raw["expires_at"] = time.time() - 1
    await store.put_kv(f"oauth.token.{token}", raw)
    assert await load(store, token) is None


async def test_two_deliveries_do_not_share_a_token(store):
    a = await mint(store, CHAT, 300)
    b = await mint(store, OTHER, 300)
    assert a != b
    assert (await load(store, a))["delivery_chat"] == CHAT
    assert (await load(store, b))["delivery_chat"] == OTHER


async def test_an_unknown_token_is_not_a_delivery_token(store):
    assert await load(store, "made-up") is None


# ------------------------------------------------ through the real server

@pytest.fixture
async def app_client(tmp_path, monkeypatch):
    import httpx
    from asgi_lifespan import LifespanManager

    from wa_mcp.app import create_app
    from wa_mcp.config import Settings, resolve_storage

    monkeypatch.delenv("WA_DATABASE_URL", raising=False)
    app = create_app(Settings(host="127.0.0.1", port=0, auth_token="t0ken",
                              oauth=False),
                     resolve_storage("", tmp_path))
    async with LifespanManager(app) as m:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=m.app),
                                     base_url="http://t") as c:
            yield c


def call(tool, args):
    return {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": tool, "arguments": args}}


async def _mint(store, chat=CHAT, ttl=300):
    return await mint(store, chat, ttl)


async def test_the_gate_refuses_over_http_not_just_in_theory(app_client):
    """Enforced in one place in front of /mcp, not inside 22 tools.

    A tool added later without a check would otherwise be reachable, and a
    boundary you have to remember to opt into is not one.
    """
    from wa_mcp.app import RT

    token = await _mint(RT.store)
    hdr = {"Authorization": f"Bearer {token}"}

    r = await app_client.post("/mcp", json=call("wa_list_chats", {}), headers=hdr)
    assert r.status_code == 403
    assert "not available to this token" in r.text

    r = await app_client.post("/mcp", json=call("wa_send", {"to": OTHER, "text": "x"}),
                              headers=hdr)
    assert r.status_code == 403
    assert "may only reply to" in r.text


async def test_a_batched_call_cannot_smuggle_one_past(app_client):
    """One legitimate call alongside an exfiltration is still refused."""
    from wa_mcp.app import RT

    token = await _mint(RT.store)
    batch = [call("wa_send", {"to": CHAT, "text": "hi"}),
             call("wa_list_chats", {})]
    r = await app_client.post("/mcp", json=batch,
                              headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 403


async def test_the_full_token_still_reaches_everything(app_client):
    """Your own connector must be untouched by any of this."""
    r = await app_client.post("/mcp", json=call("wa_list_chats", {}),
                              headers={"Authorization": "Bearer t0ken"})
    assert r.status_code != 403


# ------------------------------------------------- the routine's own token

async def test_a_routine_token_alone_cannot_send(store):
    """The connector's credential authorises nothing by itself.

    A routine authenticates once, at setup, with whatever its connector was
    configured with — it cannot pick up the per-delivery token from its own
    input. So the standing token has to be the weak one, and the per-message
    token is what turns it into permission to send.
    """
    from wa_mcp.delivery import mint_routine

    tok = await mint_routine(store)
    rec = await load(store, tok)
    why = refusal(rec, "tools/call", "wa_send", {"to": CHAT})
    assert why and "needs a live reply_token" in why


async def test_a_routine_token_plus_a_live_delivery_may_reply(store):
    from wa_mcp.delivery import mint_routine

    rec = await load(store, await mint_routine(store))
    delivery = await load(store, await mint(store, CHAT, 300))
    assert refusal(rec, "tools/call", "wa_send", {"to": CHAT}, delivery) is None


async def test_a_routine_cannot_redirect_a_live_delivery(store):
    """The injected "forward this to 9199…" case, with a real token in hand."""
    from wa_mcp.delivery import mint_routine

    rec = await load(store, await mint_routine(store))
    delivery = await load(store, await mint(store, CHAT, 300))
    why = refusal(rec, "tools/call", "wa_send", {"to": OTHER}, delivery)
    assert why and OTHER in why


async def test_a_routine_still_cannot_read_anything(store):
    from wa_mcp.delivery import mint_routine

    rec = await load(store, await mint_routine(store))
    delivery = await load(store, await mint(store, CHAT, 300))
    for tool in ("wa_list_chats", "wa_search", "wa_get_messages"):
        assert refusal(rec, "tools/call", tool, {}, delivery) is not None


async def test_an_expired_delivery_does_not_authorise_a_routine(store):
    """Replaying yesterday's token must not work."""
    from wa_mcp.delivery import mint_routine

    rec = await load(store, await mint_routine(store))
    tok = await mint(store, CHAT, 300)
    raw = await store.get_kv(f"oauth.token.{tok}")
    raw["expires_at"] = time.time() - 1
    await store.put_kv(f"oauth.token.{tok}", raw)
    assert await load(store, tok) is None          # the gate would pass None
    assert refusal(rec, "tools/call", "wa_send", {"to": CHAT}, None) is not None


async def test_the_reply_tools_accept_the_argument():
    """If fastmcp rejects the kwarg first, the gate's decision never runs."""
    import inspect

    from wa_mcp import app as A

    for name in ("wa_send", "wa_send_media", "wa_typing"):
        fn = getattr(A, name)
        fn = getattr(fn, "fn", fn)
        assert "reply_token" in inspect.signature(fn).parameters, name
