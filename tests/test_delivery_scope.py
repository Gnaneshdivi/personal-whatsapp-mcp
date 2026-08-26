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


def test_it_can_reply_in_the_chat_it_was_issued_for():
    assert refusal(rec(), "tools/call", "wa_send", {"to": CHAT}) is None


def test_it_cannot_message_a_different_number():
    why = refusal(rec(), "tools/call", "wa_send", {"to": OTHER})
    assert why and OTHER in why


def test_a_device_suffix_does_not_slip_past_the_check():
    assert refusal(rec(), "tools/call", "wa_send",
                   {"to": "918790892969:9@s.whatsapp.net"}) is None


@pytest.mark.parametrize("tool", [
    "wa_list_chats", "wa_search", "wa_get_messages", "wa_get_thread",
    "wa_download_media", "wa_list_groups", "wa_group_info", "wa_unread",
    "wa_set_reply_settings", "wa_logout", "wa_pair",
])
def test_it_cannot_read_or_change_anything(tool):
    assert refusal(rec(), "tools/call", tool, {}) is not None


def test_the_handshake_still_works():
    for method in ("initialize", "tools/list", "ping"):
        assert refusal(rec(), method, "", None) is None


def test_a_full_token_is_never_restricted():
    for tool in ("wa_list_chats", "wa_search", "wa_logout"):
        assert refusal({"scopes": []}, "tools/call", tool, {}) is None
    assert refusal(None, "tools/call", "wa_send", {"to": OTHER}) is None


def test_a_send_with_no_destination_is_refused():
    assert refusal(rec(), "tools/call", "wa_send", {}) is not None


def test_reply_tools_are_only_the_ones_that_name_a_destination():
    assert set(REPLY_TOOLS) == {"wa_send", "wa_send_media", "wa_typing"}


async def test_a_minted_token_loads_and_is_scoped(store):
    token = await mint(store, CHAT, 300)
    got = await load(store, token)
    assert got["delivery_chat"] == CHAT
    assert refusal(got, "tools/call", "wa_send", {"to": OTHER}) is not None


async def test_an_expired_token_is_gone(store):
    token = await mint(store, CHAT, 300)
    raw = await store.get_kv(f"token.{token}")
    raw["expires_at"] = time.time() - 1
    await store.put_kv(f"token.{token}", raw)
    assert await load(store, token) is None


async def test_two_deliveries_do_not_share_a_token(store):
    a = await mint(store, CHAT, 300)
    b = await mint(store, OTHER, 300)
    assert a != b
    assert (await load(store, a))["delivery_chat"] == CHAT
    assert (await load(store, b))["delivery_chat"] == OTHER


async def test_an_unknown_token_is_not_a_delivery_token(store):
    assert await load(store, "made-up") is None


@pytest.fixture
async def app_client(tmp_path, monkeypatch):
    import httpx
    from asgi_lifespan import LifespanManager

    from wa_mcp.app import create_app
    from wa_mcp.config import Settings, resolve_storage

    monkeypatch.delenv("WA_DATABASE_URL", raising=False)
    app = create_app(Settings(host="127.0.0.1", port=0, auth_token="t0ken"),
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
    from wa_mcp.app import RT

    token = await _mint(RT.store)
    batch = [call("wa_send", {"to": CHAT, "text": "hi"}),
             call("wa_list_chats", {})]
    r = await app_client.post("/mcp", json=batch,
                              headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 403


async def test_the_full_token_still_reaches_everything(app_client):
    r = await app_client.post("/mcp", json=call("wa_list_chats", {}),
                              headers={"Authorization": "Bearer t0ken"})
    assert r.status_code != 403


async def test_a_routine_token_alone_cannot_send(store):
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
    from wa_mcp.delivery import mint_routine

    rec = await load(store, await mint_routine(store))
    tok = await mint(store, CHAT, 300)
    raw = await store.get_kv(f"token.{tok}")
    raw["expires_at"] = time.time() - 1
    await store.put_kv(f"token.{tok}", raw)
    assert await load(store, tok) is None
    assert refusal(rec, "tools/call", "wa_send", {"to": CHAT}, None) is not None


async def test_the_reply_tools_accept_the_argument():
    import inspect

    from wa_mcp import app as A

    for name in ("wa_send", "wa_send_media", "wa_typing"):
        fn = getattr(A, name)
        fn = getattr(fn, "fn", fn)
        assert "reply_token" in inspect.signature(fn).parameters, name


def test_the_cli_can_mint_a_routine_token(tmp_path, monkeypatch, capsys):
    import asyncio

    monkeypatch.delenv("WA_DATABASE_URL", raising=False)
    monkeypatch.setenv("WA_DATA_DIR", str(tmp_path))

    from wa_mcp.__main__ import main

    assert main(["--mint-routine-token"]) == 0
    token = capsys.readouterr().out.strip().splitlines()[0]
    assert len(token) > 20

    from wa_mcp.config import resolve_storage
    from wa_mcp.runtime import build_store

    async def read_back():
        store = build_store(resolve_storage("", tmp_path))
        await store.connect()
        try:
            return await load(store, token)
        finally:
            await store.close()

    rec = asyncio.run(read_back())
    assert rec and rec.get("routine") is True
    assert refusal(rec, "tools/call", "wa_list_chats", {}) is not None
    assert refusal(rec, "tools/call", "wa_send", {"to": CHAT}) is not None
