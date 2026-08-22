"""App surface: auth, the OAuth-discovery fix, and the tool list.

Runs the real ASGI app in-process against a temporary SQLite store. No socket is
opened — with an empty session store the client reports `unpaired` and never
connects — so this is safe to run anywhere.
"""
from __future__ import annotations

import httpx
import pytest

from wa_mcp.config import Settings, resolve_storage


@pytest.fixture
async def client(tmp_path, monkeypatch):
    """The real app, with its lifespan run.

    LifespanManager rather than `async with app.router.lifespan_context(...)`:
    fastmcp's lifespan opens an anyio task group, and a pytest async fixture
    runs setup and teardown in different tasks, so exiting the cancel scope
    raises "attempted to exit cancel scope in a different task".
    """
    monkeypatch.delenv("WA_DATABASE_URL", raising=False)
    settings = Settings(host="127.0.0.1", port=0, auth_token="t0ken")
    storage = resolve_storage("", tmp_path)

    from asgi_lifespan import LifespanManager
    from wa_mcp.app import create_app

    app = create_app(settings, storage)
    async with LifespanManager(app) as manager:
        transport = httpx.ASGITransport(app=manager.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            yield c


async def rpc(c: httpx.AsyncClient, method: str, params: dict | None = None, id_: int = 1):
    r = await c.post(
        "/mcp?k=t0ken",
        json={"jsonrpc": "2.0", "id": id_, "method": method, "params": params or {}},
        headers={"Accept": "application/json, text/event-stream"},
    )
    for line in r.text.splitlines():
        if line.startswith("data: "):
            import json
            return json.loads(line[6:])
    return {"raw": r.text, "status": r.status_code}


# ------------------------------------------------------------------ auth

async def test_unauthenticated_is_rejected(client):
    assert (await client.get("/api/status")).status_code == 401


async def test_query_token_is_accepted(client):
    """MCP connector dialogs take a URL and nothing else — a header-only
    scheme cannot be configured in them at all."""
    assert (await client.get("/api/status?k=t0ken")).status_code == 200


async def test_bearer_header_is_accepted(client):
    r = await client.get("/api/status", headers={"Authorization": "Bearer t0ken"})
    assert r.status_code == 200


async def test_wrong_token_is_rejected(client):
    assert (await client.get("/api/status?k=nope")).status_code == 401


@pytest.mark.parametrize("path", [
    "/.well-known/oauth-authorization-server",
    "/.well-known/oauth-protected-resource",
    "/register",
])
async def test_oauth_discovery_404s_rather_than_401s(client, path):
    """A 401 here tells the client OAuth exists; it then walks discovery, fails
    at dynamic registration, and reports 'couldn't register with the sign-in
    service' — never trying the token it already had."""
    r = await client.get(path)
    assert r.status_code == 404


async def test_401_does_not_advertise_bearer(client):
    """The WWW-Authenticate header is exactly what starts the OAuth dance."""
    r = await client.get("/api/status")
    assert "www-authenticate" not in {k.lower() for k in r.headers}


# ------------------------------------------------------------------ tools

async def test_the_expected_tools_are_registered(client):
    tools = {t["name"] for t in (await rpc(client, "tools/list"))["result"]["tools"]}
    expected = {
        "wa_status", "wa_pair", "wa_logout",
        "wa_list_chats", "wa_get_messages", "wa_search", "wa_get_thread", "wa_unread",
        "wa_send", "wa_send_media", "wa_react", "wa_mark_read", "wa_typing",
        "wa_check_number",
    }
    assert expected <= tools


async def test_read_tools_expose_the_time_window(client):
    """search and get_messages both support since/until in the store; the old
    MCP layer dropped both parameters."""
    tools = {t["name"]: t for t in (await rpc(client, "tools/list"))["result"]["tools"]}
    for name in ("wa_search", "wa_get_messages"):
        props = set(tools[name]["inputSchema"]["properties"])
        assert {"since", "until"} <= props, f"{name} is missing the time window"


async def test_send_exposes_reply_to(client):
    props = {t["name"]: t for t in (await rpc(client, "tools/list"))["result"]["tools"]}
    assert "reply_to" in props["wa_send"]["inputSchema"]["properties"]


# ------------------------------------------------------------ tool calls

async def test_list_chats_on_an_empty_store(client):
    out = (await rpc(client, "tools/call",
                     {"name": "wa_list_chats", "arguments": {}}))["result"]
    assert out["structuredContent"] == {"ok": True, "count": 0, "chats": []}


async def test_status_reports_unpaired_not_an_error(client):
    out = (await rpc(client, "tools/call",
                     {"name": "wa_status", "arguments": {}}))["result"]["structuredContent"]
    assert out["ok"] is True
    assert out["phase"] in ("unpaired", "connecting")
    assert out["ready"] is False


async def test_sending_without_a_session_fails_with_a_usable_message(client):
    out = (await rpc(client, "tools/call",
                     {"name": "wa_send",
                      "arguments": {"to": "919812345678", "text": "hi"}}))["result"]
    sc = out["structuredContent"]
    assert sc["ok"] is False
    assert sc["error"]


async def test_bad_timestamp_is_explained_not_swallowed(client):
    out = (await rpc(client, "tools/call",
                     {"name": "wa_search",
                      "arguments": {"query": "x", "since": "yesterday"}}))["result"]
    sc = out["structuredContent"]
    assert sc["ok"] is False
    assert "ISO-8601" in sc["error"]


async def test_unknown_contact_name_names_the_next_step(client):
    out = (await rpc(client, "tools/call",
                     {"name": "wa_send",
                      "arguments": {"to": "Nobody", "text": "hi"}}))["result"]
    sc = out["structuredContent"]
    assert sc["ok"] is False
    assert "wa_list_chats" in sc["error"]
