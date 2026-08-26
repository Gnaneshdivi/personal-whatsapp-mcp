from __future__ import annotations

import httpx

from wa_mcp.config import Settings, resolve_storage


async def _app(monkeypatch, tmp_path, **env):
    from asgi_lifespan import LifespanManager

    from wa_mcp.app import create_app

    for k in ("WA_AUTH_TOKEN", "WA_ALLOW_OPEN", "PUBLIC_BASE_URL",
              "WA_DATABASE_URL", "WA_OAUTH"):
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)

    settings = Settings(host=env.get("_host", "127.0.0.1"), port=0,
                        auth_token="",
                        allow_open=env.get("WA_ALLOW_OPEN") == "1",
                        public_base_url=env.get("PUBLIC_BASE_URL", ""))
    app = create_app(settings, resolve_storage("", tmp_path))
    return LifespanManager(app), settings


async def test_loopback_with_no_token_is_open(monkeypatch, tmp_path):
    mgr, _ = await _app(monkeypatch, tmp_path)
    async with mgr as m:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=m.app),
                                     base_url="http://t") as c:
            r = await c.post("/mcp", json={"jsonrpc": "2.0", "id": 1,
                                           "method": "tools/list"},
                             headers={"Accept": "application/json, text/event-stream"})
            assert r.status_code == 200


async def test_a_public_base_url_gets_a_token_even_on_loopback(monkeypatch, tmp_path):
    mgr, settings = await _app(monkeypatch, tmp_path,
                               PUBLIC_BASE_URL="https://x.ngrok.io")
    async with mgr as m:
        assert settings.auth_token, "no token was generated for a tunnelled server"
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=m.app),
                                     base_url="http://t") as c:
            r = await c.post("/mcp", json={"jsonrpc": "2.0", "id": 1,
                                           "method": "tools/list"},
                             headers={"Accept": "application/json, text/event-stream"})
            assert r.status_code == 401

            r = await c.post(f"/mcp?k={settings.auth_token}",
                             json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                             headers={"Accept": "application/json, text/event-stream"})
            assert r.status_code == 200


async def test_binding_to_all_interfaces_gets_a_token(monkeypatch, tmp_path):
    mgr, settings = await _app(monkeypatch, tmp_path, _host="0.0.0.0")
    async with mgr:
        assert settings.auth_token


async def test_open_can_be_asked_for_explicitly(monkeypatch, tmp_path):
    mgr, settings = await _app(monkeypatch, tmp_path, _host="0.0.0.0",
                               WA_ALLOW_OPEN="1")
    async with mgr as m:
        assert not settings.auth_token
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=m.app),
                                     base_url="http://t") as c:
            r = await c.post("/mcp", json={"jsonrpc": "2.0", "id": 1,
                                           "method": "tools/list"},
                             headers={"Accept": "application/json, text/event-stream"})
            assert r.status_code == 200


async def test_a_configured_token_is_never_replaced(monkeypatch, tmp_path):
    from asgi_lifespan import LifespanManager

    from wa_mcp.app import create_app

    monkeypatch.delenv("WA_ALLOW_OPEN", raising=False)
    settings = Settings(host="0.0.0.0", port=0, auth_token="mine")
    app = create_app(settings, resolve_storage("", tmp_path))
    async with LifespanManager(app):
        assert settings.auth_token == "mine"


async def test_the_generated_token_is_kept_in_the_store(tmp_path, monkeypatch):
    monkeypatch.delenv("WA_DATABASE_URL", raising=False)
    from wa_mcp.app import TOKEN_KEY, _stored_token
    from wa_mcp.runtime import build_store

    store = build_store(resolve_storage("", tmp_path))
    await store.connect()
    try:
        first = await _stored_token(store)
        assert first
        assert await _stored_token(store) == first, "a new one on every start"
        row = await store.get_kv(TOKEN_KEY)
        assert row["token"] == first
    finally:
        await store.close()

    assert not (tmp_path / "access-token").exists(), "left a credential on disk"


async def test_a_tunnelled_server_has_a_token_by_the_time_it_serves(
        monkeypatch, tmp_path):
    mgr, settings = await _app(monkeypatch, tmp_path,
                               PUBLIC_BASE_URL="https://x.ngrok.io")
    async with mgr as m:
        assert settings.auth_token
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=m.app),
                                     base_url="http://t") as c:
            r = await c.get("/api/status")
            assert r.status_code == 401
