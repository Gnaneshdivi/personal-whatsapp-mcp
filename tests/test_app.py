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
    """The app, with its lifespan run.

    One fixture: there used to be two, differing only in whether OAuth was on.

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
async def test_discovery_404s_when_oauth_is_off(client, path):
    """With no OAuth server, a 401 here would send the client off to register
    with an authorization server that does not exist, and it would report
    "couldn't register with the sign-in service" — never trying the static
    token it already had. 404 ends the search."""
    r = await client.get(path)
    assert r.status_code == 404


async def test_401_does_not_advertise_bearer_without_oauth(client):
    """The WWW-Authenticate header is exactly what starts the OAuth dance, and
    with no OAuth server behind it that dance ends in a dead end."""
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
    # Names both places that were searched, so the answer is not just "no",
    # and points at the tool that can look rather than leaving it there.
    assert "no chat or contact matching" in sc["error"]
    assert "wa_search" in sc["error"]




# ------------------------------------------------------- the session cookie

async def test_a_browser_trades_the_url_token_for_a_cookie(client):
    """The URL nobody can remember gets pasted into a notes app.

    And every visit leaves the credential — which is the whole account — in
    history, in proxy logs, and in the referrer of anything the page loads.
    """
    r = await client.get("/?k=t0ken", headers={"Accept": "text/html"},
                                follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/"
    cookie = r.headers["set-cookie"]
    assert "wa_session=t0ken" in cookie
    assert "HttpOnly" in cookie and "SameSite=Lax" in cookie


async def test_the_bare_url_works_afterwards(client):
    """/settings rather than /, which redirects to the pairing page when the
    test store has no session — a detour that says nothing about the cookie."""
    r = await client.get("/settings", headers={"Accept": "text/html",
                                                      "Cookie": "wa_session=t0ken"})
    assert r.status_code == 200


async def test_no_cookie_still_means_no_access(client):
    r = await client.get("/settings", headers={"Accept": "text/html"})
    assert r.status_code == 401


async def test_other_query_parameters_survive_the_redirect(client):
    """/connect?flow=… must not lose the flow id on the way through."""
    r = await client.get("/?k=t0ken&tab=groups",
                                headers={"Accept": "text/html"},
                                follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/?tab=groups"


async def test_an_api_call_is_never_redirected(client):
    """Redirecting an MCP client or a curl would break it, and neither keeps
    cookies anyway. Only GETs that asked for HTML are traded."""
    r = await client.get("/api/status?k=t0ken", follow_redirects=False)
    assert r.status_code == 200

    r = await client.post("/api/settings?k=t0ken", json={"enabled": False},
                                 follow_redirects=False)
    assert r.status_code == 200


async def test_the_cookie_is_secure_behind_a_proxy(client):
    """Cloudflare terminates TLS, so the hop to us is plain http.

    Reading scope["scheme"] alone would mark the cookie insecure on every
    tunnelled deployment, which is all of them.
    """
    r = await client.get("/?k=t0ken",
                                headers={"Accept": "text/html",
                                         "X-Forwarded-Proto": "https"},
                                follow_redirects=False)
    assert "Secure" in r.headers["set-cookie"]


async def test_the_cookie_is_not_secure_on_plain_http(client):
    """A Secure cookie over http is dropped, and localhost is a normal setup."""
    r = await client.get("/?k=t0ken", headers={"Accept": "text/html"},
                                follow_redirects=False)
    assert "Secure" not in r.headers["set-cookie"]


# ------------------------------------------------------------- signing out

async def test_signing_out_clears_the_cookie(client):
    r = await client.get("/logout", headers={"Cookie": "wa_session=t0ken"})
    assert r.status_code == 200
    cookie = r.headers["set-cookie"]
    assert "wa_session=;" in cookie
    assert "Max-Age=0" in cookie


async def test_the_cleared_cookie_expires_rather_than_being_empty(client):
    """An empty cookie still presents itself.

    It would then fail auth on every request instead of falling back to asking
    for the token, so the browser looks broken rather than signed out.
    """
    r = await client.get("/logout", headers={"Cookie": "wa_session=t0ken"})
    assert "Max-Age=0" in r.headers["set-cookie"]

    after = await client.get("/settings", headers={"Accept": "text/html"})
    assert after.status_code == 401


async def test_logging_out_removes_the_archive(client):
    """One action, deliberately. Anything less left something behind that the
    person clicking it believed was gone."""
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
    """One control, and its label says what it does."""
    page = (await client.get("/settings?k=t0ken")).text
    assert 'id="signout"' in page
    assert page.count(">Log out</button>") == 1
    assert page.count(">Log out</button>") == 1
    assert "cannot be undone" in page


# ------------------------------------------------------ signing out everywhere

async def test_logging_out_expires_issued_credentials(client):
    """A browser sign-out leaves connectors untouched.

    An OAuth token lasts 30 days and a routine token never expires, so without
    this the only way to sign a lost client out is to wait — or to change
    WA_AUTH_TOKEN and restart, which signs out everything including you.
    """
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
    """It comes from the environment and is re-registered on every start.

    Revoking it would lock you out until a restart and do nothing after one.
    """
    await client.post("/logout?k=t0ken")
    r = await client.get("/api/status?k=t0ken")
    assert r.status_code == 200


async def test_logging_out_also_clears_this_browser(client):
    r = await client.post("/logout?k=t0ken")
    assert "Max-Age=0" in r.headers["set-cookie"]


async def test_logging_out_reports_how_many(client):
    """Silence would leave you wondering whether it did anything."""
    from wa_mcp.delivery import mint

    await mint(__import__("wa_mcp.app", fromlist=["RT"]).RT.store,
               "1@s.whatsapp.net", 300)
    r = await client.post("/logout?k=t0ken")
    assert r.json()["revoked"] >= 1


# ------------------------------------------------------------- logging out

async def test_logout_clears_the_archive(client):
    """An unlinked server holding somebody's conversations is stale,
    unreachable, and still readable by anyone with the file."""
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
    """Otherwise the data survives a logout the user believes succeeded."""
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
    """confirm() renders a system dialog showing the domain, styled like a
    security warning — too much for something that deletes nothing."""
    page = (await client.get("/settings?k=t0ken")).text
    # The call form, not the word — a comment explaining why it is gone
    # would otherwise fail this.
    assert 'confirm("' not in page and "confirm('" not in page
    assert "Click again to confirm" in page


async def test_logout_is_not_intercepted_by_the_cookie_trade(client):
    """Issuing a session cookie on the way into the endpoint that revokes it
    would set and clear it in one click, and the redirect swallows the page
    saying what was deleted."""
    r = await client.get("/logout?k=t0ken", headers={"Accept": "text/html"},
                         follow_redirects=False)
    assert r.status_code == 200
    assert "Logged out" in r.text


# ------------------------------------------------------------- signing in

async def test_a_browser_without_a_token_gets_a_form(client):
    """A bare 401 is correct and useless — it looks broken, and the person
    seeing it has no idea the answer is a token from install."""
    r = await client.get("/", headers={"Accept": "text/html"},
                                follow_redirects=False)
    assert r.status_code == 401
    assert "Sign in" in r.text
    assert 'name="k"' in r.text
    # And says where to find it. A password box for a password nobody gave
    # you is not a sign-in page.
    assert "WA_AUTH_TOKEN" in r.text or "printed when the server starts" in r.text


async def test_the_form_posts_back_to_the_path_it_was_asked_for(client):
    """So signing in from /settings lands on /settings, not the chat list."""
    r = await client.get("/settings", headers={"Accept": "text/html"},
                                follow_redirects=False)
    assert 'action="/settings"' in r.text


async def test_the_form_reuses_the_token_path(client):
    """It submits ?k=, which the middleware already trades for a cookie —
    rather than adding a second way to authenticate."""
    r = await client.get("/?k=t0ken", headers={"Accept": "text/html"},
                                follow_redirects=False)
    assert r.status_code == 303
    assert "wa_session=t0ken" in r.headers["set-cookie"]


async def test_an_api_client_still_gets_a_plain_401(client):
    """A form would be nonsense to curl or an MCP client."""
    r = await client.get("/api/status", follow_redirects=False)
    assert r.status_code == 401
    assert "Sign in" not in r.text


async def test_the_sign_in_page_never_shows_the_pairing_qr(client):
    """The QR links a phone to this server.

    Shown to an unauthenticated visitor it would let anyone who knows the
    hostname claim an unpaired instance — including in the moments after a log
    out, when the host is already known.
    """
    r = await client.get("/", headers={"Accept": "text/html"},
                                follow_redirects=False)
    assert "<svg" not in r.text and "qr" not in r.text.lower()
