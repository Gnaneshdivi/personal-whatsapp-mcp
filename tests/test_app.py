from __future__ import annotations

import httpx
import pytest

from wa_mcp.config import Settings, resolve_storage


@pytest.fixture
async def client(tmp_path, monkeypatch):
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


def paired(monkeypatch):
    from wa_mcp.app import RT

    monkeypatch.setattr(RT.wa, "self_jid", "919100828649@s.whatsapp.net")


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


async def test_unauthenticated_is_rejected(client):
    assert (await client.get("/api/status")).status_code == 401


async def test_query_token_is_accepted(client):
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
async def test_discovery_404s_when_oauth_is_off(client, path):
    r = await client.get(path)
    assert r.status_code == 404


async def test_401_does_not_advertise_bearer_without_oauth(client):
    r = await client.get("/api/status")
    assert "www-authenticate" not in {k.lower() for k in r.headers}


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
    tools = {t["name"]: t for t in (await rpc(client, "tools/list"))["result"]["tools"]}
    for name in ("wa_search", "wa_get_messages"):
        props = set(tools[name]["inputSchema"]["properties"])
        assert {"since", "until"} <= props, f"{name} is missing the time window"


async def test_send_exposes_reply_to(client):
    props = {t["name"]: t for t in (await rpc(client, "tools/list"))["result"]["tools"]}
    assert "reply_to" in props["wa_send"]["inputSchema"]["properties"]


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
    assert "no chat or contact matching" in sc["error"]
    assert "wa_search" in sc["error"]


async def test_a_browser_trades_the_url_token_for_a_cookie(client):
    r = await client.get("/?k=t0ken", headers={"Accept": "text/html"},
                                follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/"
    cookie = r.headers["set-cookie"]
    assert "wa_session=t0ken" in cookie
    assert "HttpOnly" in cookie and "SameSite=Lax" in cookie


async def test_the_bare_url_works_afterwards(client):
    r = await client.get("/settings", headers={"Accept": "text/html",
                                                      "Cookie": "wa_session=t0ken"})
    assert r.status_code == 200


async def test_no_cookie_still_means_no_access(client, monkeypatch):
    paired(monkeypatch)
    r = await client.get("/settings", headers={"Accept": "text/html"})
    assert r.status_code == 401


async def test_other_query_parameters_survive_the_redirect(client):
    r = await client.get("/?k=t0ken&tab=groups",
                                headers={"Accept": "text/html"},
                                follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/?tab=groups"


async def test_an_api_call_is_never_redirected(client):
    r = await client.get("/api/status?k=t0ken", follow_redirects=False)
    assert r.status_code == 200

    r = await client.post("/api/settings?k=t0ken", json={"enabled": False},
                                 follow_redirects=False)
    assert r.status_code == 200


async def test_the_cookie_is_secure_behind_a_proxy(client):
    r = await client.get("/?k=t0ken",
                                headers={"Accept": "text/html",
                                         "X-Forwarded-Proto": "https"},
                                follow_redirects=False)
    assert "Secure" in r.headers["set-cookie"]


async def test_the_cookie_is_not_secure_on_plain_http(client):
    r = await client.get("/?k=t0ken", headers={"Accept": "text/html"},
                                follow_redirects=False)
    assert "Secure" not in r.headers["set-cookie"]


async def test_signing_out_clears_the_cookie(client):
    r = await client.get("/logout", headers={"Cookie": "wa_session=t0ken"})
    assert r.status_code == 200
    cookie = r.headers["set-cookie"]
    assert "wa_session=;" in cookie
    assert "Max-Age=0" in cookie


async def test_the_cleared_cookie_expires_rather_than_being_empty(client, monkeypatch):
    r = await client.get("/logout", headers={"Cookie": "wa_session=t0ken"})
    assert "Max-Age=0" in r.headers["set-cookie"]

    paired(monkeypatch)
    after = await client.get("/settings", headers={"Accept": "text/html"})
    assert after.status_code == 401


async def test_logging_out_removes_the_archive(client):
    from wa_mcp.app import RT
    from wa_mcp.store.base import Message

    await RT.store.upsert_message(Message(
        message_id="w1", chat_jid="1@s.whatsapp.net", ts=1, text="private",
        is_from_me=False))
    await RT.store.put_kv("trigger.settings", {"enabled": True})

    r = await client.post("/logout?k=t0ken")
    assert r.json()["ok"] is True

    assert await RT.store.search("private") == []
    assert await RT.store.get_kv("trigger.settings") is None


async def test_the_logged_out_page_says_what_it_did(client):
    body = (await client.get("/logout?k=t0ken",
                             headers={"Accept": "text/html"})).text
    assert "unlinked" in body
    assert "cannot be" in body or "starts with an empty" in body or "not what was here" in body


async def test_the_settings_page_offers_one_log_out(client):
    page = (await client.get("/settings?k=t0ken")).text
    assert 'id="signout"' in page
    assert page.count(">Log out</button>") == 1
    assert page.count(">Log out</button>") == 1
    assert "cannot be undone" in page


async def test_logging_out_expires_issued_credentials(client):
    from wa_mcp.app import RT
    from wa_mcp.delivery import load, mint, mint_routine

    a = await mint(RT.store, "1@s.whatsapp.net", 300)
    b = await mint_routine(RT.store)
    assert await load(RT.store, a) and await load(RT.store, b)

    r = await client.post("/logout?k=t0ken")
    assert r.json()["ok"] is True and r.json()["revoked"] >= 2

    assert await load(RT.store, a) is None
    assert await load(RT.store, b) is None


async def test_logging_out_spares_the_configured_token(client):
    await client.post("/logout?k=t0ken")
    r = await client.get("/api/status?k=t0ken")
    assert r.status_code == 200


async def test_logging_out_also_clears_this_browser(client):
    r = await client.post("/logout?k=t0ken")
    assert "Max-Age=0" in r.headers["set-cookie"]


async def test_logging_out_reports_how_many(client):
    from wa_mcp.delivery import mint

    await mint(__import__("wa_mcp.app", fromlist=["RT"]).RT.store,
               "1@s.whatsapp.net", 300)
    r = await client.post("/logout?k=t0ken")
    assert r.json()["revoked"] >= 1


async def test_logout_clears_the_archive(client):
    from wa_mcp.app import RT
    from wa_mcp.store.base import Message

    await RT.store.upsert_message(Message(
        message_id="m1", chat_jid="1@s.whatsapp.net", ts=1, text="private",
        is_from_me=False))
    await RT.store.put_kv("trigger.settings", {"enabled": True})

    out = (await rpc(client, "tools/call",
                     {"name": "wa_logout", "arguments": {}}))["result"]
    assert out["structuredContent"]["ok"] is True

    assert await RT.store.search("private") == []
    assert await RT.store.get_kv("trigger.settings") is None


async def test_logout_can_keep_the_archive_if_asked(client):
    from wa_mcp.app import RT
    from wa_mcp.store.base import Message

    await RT.store.upsert_message(Message(
        message_id="m2", chat_jid="1@s.whatsapp.net", ts=1, text="kept",
        is_from_me=False))

    await rpc(client, "tools/call",
              {"name": "wa_logout", "arguments": {"keep_history": True}})
    assert await RT.store.search("kept") != []


async def test_a_failed_unlink_still_clears_local_state(client, monkeypatch):
    from wa_mcp.app import RT
    from wa_mcp.store.base import Message

    await RT.store.upsert_message(Message(
        message_id="m3", chat_jid="1@s.whatsapp.net", ts=1, text="private",
        is_from_me=False))

    class Boom:
        async def logout(self):
            raise RuntimeError("socket gone")

    monkeypatch.setattr(RT.wa, "_client", Boom())
    out = (await rpc(client, "tools/call",
                     {"name": "wa_logout", "arguments": {}}))["result"]
    assert out["structuredContent"]["ok"] is True
    assert "unlink_error" in out["structuredContent"]
    assert await RT.store.search("private") == []


async def test_sign_out_confirms_in_the_page_not_in_a_system_dialog(client):
    page = (await client.get("/settings?k=t0ken")).text
    assert 'confirm("' not in page and "confirm('" not in page
    assert "Click again to confirm" in page


async def test_logout_is_not_intercepted_by_the_cookie_trade(client):
    r = await client.get("/logout?k=t0ken", headers={"Accept": "text/html"},
                         follow_redirects=False)
    assert r.status_code == 200
    assert "Logged out" in r.text


async def test_a_browser_without_a_token_gets_a_form(client, monkeypatch):
    paired(monkeypatch)
    r = await client.get("/", headers={"Accept": "text/html"},
                                follow_redirects=False)
    assert r.status_code == 401
    assert "Sign in" in r.text
    assert 'name="k"' in r.text
    assert "WA_AUTH_TOKEN" in r.text or "printed when the server starts" in r.text
    assert "MCP client" in r.text


async def test_the_form_posts_back_to_the_path_it_was_asked_for(client, monkeypatch):
    paired(monkeypatch)
    r = await client.get("/settings", headers={"Accept": "text/html"},
                                follow_redirects=False)
    assert 'action="/settings"' in r.text


async def test_the_form_reuses_the_token_path(client):
    r = await client.get("/?k=t0ken", headers={"Accept": "text/html"},
                                follow_redirects=False)
    assert r.status_code == 303
    assert "wa_session=t0ken" in r.headers["set-cookie"]


async def test_an_api_client_still_gets_a_plain_401(client):
    r = await client.get("/api/status", follow_redirects=False)
    assert r.status_code == 401
    assert "Sign in" not in r.text


async def test_the_sign_in_page_never_shows_the_qr_once_paired(client, monkeypatch):
    paired(monkeypatch)
    r = await client.get("/", headers={"Accept": "text/html"},
                                follow_redirects=False)
    assert "<svg" not in r.text and "qr" not in r.text.lower()


async def test_the_connector_url_is_available_in_the_app(client):
    d = (await client.get("/api/connect-info?k=t0ken")).json()
    assert d["mcp_url"].endswith("/mcp?k=t0ken")
    assert d["needs_token"] is True


async def test_the_connector_url_needs_a_signed_in_session(client):
    r = await client.get("/api/connect-info")
    assert r.status_code == 401


async def test_the_settings_page_shows_it(client):
    page = (await client.get("/settings?k=t0ken")).text
    assert 'id="mcpurl"' in page
    assert "/api/connect-info" in page
    assert "Connect an AI client" in page


def offer_qr(monkeypatch):
    from wa_mcp.app import RT

    async def no_op():
        return None

    monkeypatch.setattr(RT.wa, "pair", no_op)
    monkeypatch.setattr(RT.wa, "qr", "2@fake,code,for,a,test")


async def test_an_unpaired_server_shows_the_qr_not_a_token_form(client, monkeypatch):
    offer_qr(monkeypatch)
    r = await client.get("/", headers={"Accept": "text/html"},
                         follow_redirects=False)
    assert r.status_code == 307
    assert "/connect" in r.headers["location"]


async def test_the_connect_page_is_reachable_unpaired_without_a_token(client, monkeypatch):
    offer_qr(monkeypatch)
    r = await client.get("/connect", headers={"Accept": "text/html"})
    assert r.status_code == 200
    assert "Sign in" not in r.text
    assert "<svg" in r.text


async def test_scanning_signs_the_browser_in(client, monkeypatch):
    offer_qr(monkeypatch)

    first = await client.get("/connect", headers={"Accept": "text/html"})
    assert first.status_code == 200
    cookie = first.headers.get("set-cookie", "")
    assert "wa_pairing=" in cookie
    ticket = cookie.split("wa_pairing=")[1].split(";")[0]

    paired(monkeypatch)

    second = await client.get("/connect", headers={"Accept": "text/html",
                                                   "Cookie": f"wa_pairing={ticket}"},
                              follow_redirects=False)
    assert second.status_code == 303
    assert "wa_session=t0ken" in second.headers.get("set-cookie", "")


async def test_a_ticket_is_not_a_session(client, monkeypatch):
    offer_qr(monkeypatch)
    first = await client.get("/connect", headers={"Accept": "text/html"})
    ticket = first.headers["set-cookie"].split("wa_pairing=")[1].split(";")[0]

    paired(monkeypatch)
    for path in ("/", "/settings"):
        r = await client.get(path, headers={"Accept": "text/html",
                                            "Cookie": f"wa_pairing={ticket}"},
                             follow_redirects=False)
        assert r.status_code == 401, path

    r = await client.get("/api/status", headers={"Cookie": f"wa_pairing={ticket}"})
    assert r.status_code == 401


async def test_a_stale_ticket_gets_nothing(client, monkeypatch):
    paired(monkeypatch)
    r = await client.get("/connect", headers={"Accept": "text/html",
                                              "Cookie": "wa_pairing=made-up"},
                         follow_redirects=False)
    assert r.status_code == 401


async def test_an_api_client_is_never_let_through_unpaired(client, monkeypatch):
    offer_qr(monkeypatch)
    r = await client.post("/mcp", json={"jsonrpc": "2.0", "id": 1,
                                        "method": "tools/list"},
                          headers={"Accept": "application/json, text/event-stream"})
    assert r.status_code == 401

    r = await client.get("/api/status")
    assert r.status_code == 401


async def test_the_profile_tool_is_registered(client):
    out = await rpc(client, "tools/list")
    names = [t["name"] for t in out["result"]["tools"]]
    assert "wa_profile" in names


async def test_the_ui_can_read_a_contact_profile(client, monkeypatch):
    from wa_mcp.app import RT

    async def fake(jid):
        return {"chat_jid": jid, "name": "Asif", "devices": 2,
                "picture_url": "", "about": ""}

    monkeypatch.setattr(RT.wa, "profile", fake)
    d = (await client.get("/api/profile/1@s.whatsapp.net?k=t0ken")).json()
    assert d["ok"] is True and d["devices"] == 2


async def test_a_profile_failure_is_reported_not_swallowed(client, monkeypatch):
    from wa_mcp.app import RT

    async def boom(jid):
        raise RuntimeError("no session")

    monkeypatch.setattr(RT.wa, "profile", boom)
    r = await client.get("/api/profile/1@s.whatsapp.net?k=t0ken")
    assert r.status_code == 502 and "no session" in r.json()["error"]
