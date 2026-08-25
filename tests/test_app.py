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
async def noauth_client(tmp_path, monkeypatch):
    """The server with OAuth switched off — static token only."""
    monkeypatch.delenv("WA_DATABASE_URL", raising=False)
    settings = Settings(host="127.0.0.1", port=0, auth_token="t0ken", oauth=False)
    storage = resolve_storage("", tmp_path)

    from asgi_lifespan import LifespanManager
    from wa_mcp.app import create_app

    app = create_app(settings, storage)
    async with LifespanManager(app) as manager:
        transport = httpx.ASGITransport(app=manager.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            yield c


@pytest.fixture
async def client(tmp_path, monkeypatch):
    """The real app, with its lifespan run.

    LifespanManager rather than `async with app.router.lifespan_context(...)`:
    fastmcp's lifespan opens an anyio task group, and a pytest async fixture
    runs setup and teardown in different tasks, so exiting the cancel scope
    raises "attempted to exit cancel scope in a different task".
    """
    monkeypatch.delenv("WA_DATABASE_URL", raising=False)
    settings = Settings(host="127.0.0.1", port=0, auth_token="t0ken", oauth=True)
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
async def test_discovery_404s_when_oauth_is_off(noauth_client, path):
    """With no OAuth server, a 401 here would send the client off to register
    with an authorization server that does not exist, and it would report
    "couldn't register with the sign-in service" — never trying the static
    token it already had. 404 ends the search."""
    r = await noauth_client.get(path)
    assert r.status_code == 404


async def test_discovery_is_served_when_oauth_is_on(client):
    """With OAuth on, the same paths must answer — that is how a connector
    finds the authorize and token endpoints."""
    r = await client.get("/.well-known/oauth-authorization-server")
    assert r.status_code == 200
    meta = r.json()
    for field in ("issuer", "authorization_endpoint", "token_endpoint",
                  "registration_endpoint"):
        assert meta.get(field), f"{field} missing from discovery metadata"
    assert "S256" in meta.get("code_challenge_methods_supported", [])


async def test_protected_resource_metadata_points_at_the_auth_server(client):
    """Path-suffixed with the resource, per the MCP auth spec — the resource
    being protected is the /mcp endpoint, not the whole origin."""
    r = await client.get("/.well-known/oauth-protected-resource/mcp")
    assert r.status_code == 200
    assert r.json().get("authorization_servers")


async def test_401_does_not_advertise_bearer_without_oauth(noauth_client):
    """The WWW-Authenticate header is exactly what starts the OAuth dance, and
    with no OAuth server behind it that dance ends in a dead end."""
    r = await noauth_client.get("/api/status")
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



# --------------------------------------------------------- the OAuth flow

async def test_a_client_can_register_itself(client):
    """Dynamic registration is not optional: an MCP client has no way to be
    pre-registered, and requiring a hand-made client_id puts us back to copying
    secrets around."""
    r = await client.post("/register", json={
        "client_name": "test-connector",
        "redirect_uris": ["http://localhost:9999/callback"],
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",
    })
    assert r.status_code in (200, 201), r.text
    assert r.json()["client_id"]


async def test_authorize_sends_the_browser_to_the_pairing_page(client):
    reg = (await client.post("/register", json={
        "client_name": "t", "redirect_uris": ["http://localhost:9999/cb"],
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none"})).json()

    r = await client.get("/authorize", params={
        "response_type": "code", "client_id": reg["client_id"],
        "redirect_uri": "http://localhost:9999/cb", "state": "xyz",
        "code_challenge": "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM",
        "code_challenge_method": "S256",
    }, follow_redirects=False)

    assert r.status_code in (302, 303, 307)
    assert "/connect?flow=" in r.headers["location"]


async def test_the_pairing_page_is_reachable_without_a_token_during_a_flow(
        client, monkeypatch):
    """The browser arriving from /authorize has no token — the flow id is the
    capability, and it is a secret we minted for exactly this request.

    pair() is stubbed: loading this page really does open a WhatsApp socket,
    and a test suite must not dial a third party.
    """
    from wa_mcp import app as appmod

    async def no_socket():
        return None

    monkeypatch.setattr(appmod.RT.wa, "pair", no_socket)

    reg = (await client.post("/register", json={
        "client_name": "t", "redirect_uris": ["http://localhost:9999/cb"],
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none"})).json()
    r = await client.get("/authorize", params={
        "response_type": "code", "client_id": reg["client_id"],
        "redirect_uri": "http://localhost:9999/cb",
        "code_challenge": "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM",
        "code_challenge_method": "S256"}, follow_redirects=False)
    flow_url = r.headers["location"]

    page = await client.get(flow_url)
    assert page.status_code == 200          # not 401


async def test_an_unknown_flow_does_not_hand_out_a_redirect(client):
    r = await client.get("/api/flow/not-a-real-flow?flow=x")
    assert r.status_code == 200
    assert r.json()["redirect"] is None


async def test_the_static_token_still_works_alongside_oauth(client):
    """Registered as an ordinary non-expiring access token, so there is one
    validation path rather than two."""
    out = (await rpc(client, "tools/call",
                     {"name": "wa_status", "arguments": {}}))["result"]
    assert out["structuredContent"]["ok"] is True


async def test_mcp_rejects_a_bogus_bearer_token(client):
    r = await client.post("/mcp", json={"jsonrpc": "2.0", "id": 1,
                                        "method": "tools/list", "params": {}},
                          headers={"Accept": "application/json, text/event-stream",
                                   "Authorization": "Bearer not-a-real-token"})
    assert r.status_code == 401


def test_pkce_rejects_a_wrong_verifier():
    from wa_mcp.oauth import verify_pkce
    verifier = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
    challenge = "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"
    assert verify_pkce(verifier, challenge) is True
    assert verify_pkce("wrong-verifier", challenge) is False


async def test_registration_requires_refresh_token_grant(client):
    """fastmcp rejects a client that asks for authorization_code alone. Worth
    pinning: it is a 400 with a clear message rather than a silent failure, and
    a connector that omits it will not work."""
    r = await client.post("/register", json={
        "client_name": "t", "redirect_uris": ["http://localhost:9999/cb"],
        "grant_types": ["authorization_code"], "response_types": ["code"],
        "token_endpoint_auth_method": "none"})
    assert r.status_code == 400
    assert "refresh_token" in r.text


# ------------------------------------------------------- the session cookie

async def test_a_browser_trades_the_url_token_for_a_cookie(noauth_client):
    """The URL nobody can remember gets pasted into a notes app.

    And every visit leaves the credential — which is the whole account — in
    history, in proxy logs, and in the referrer of anything the page loads.
    """
    r = await noauth_client.get("/?k=t0ken", headers={"Accept": "text/html"},
                                follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/"
    cookie = r.headers["set-cookie"]
    assert "wa_session=t0ken" in cookie
    assert "HttpOnly" in cookie and "SameSite=Lax" in cookie


async def test_the_bare_url_works_afterwards(noauth_client):
    """/settings rather than /, which redirects to the pairing page when the
    test store has no session — a detour that says nothing about the cookie."""
    r = await noauth_client.get("/settings", headers={"Accept": "text/html",
                                                      "Cookie": "wa_session=t0ken"})
    assert r.status_code == 200


async def test_no_cookie_still_means_no_access(noauth_client):
    r = await noauth_client.get("/settings", headers={"Accept": "text/html"})
    assert r.status_code == 401


async def test_other_query_parameters_survive_the_redirect(noauth_client):
    """/connect?flow=… must not lose the flow id on the way through."""
    r = await noauth_client.get("/?k=t0ken&tab=groups",
                                headers={"Accept": "text/html"},
                                follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/?tab=groups"


async def test_an_api_call_is_never_redirected(noauth_client):
    """Redirecting an MCP client or a curl would break it, and neither keeps
    cookies anyway. Only GETs that asked for HTML are traded."""
    r = await noauth_client.get("/api/status?k=t0ken", follow_redirects=False)
    assert r.status_code == 200

    r = await noauth_client.post("/api/settings?k=t0ken", json={"enabled": False},
                                 follow_redirects=False)
    assert r.status_code == 200


async def test_the_cookie_is_secure_behind_a_proxy(noauth_client):
    """Cloudflare terminates TLS, so the hop to us is plain http.

    Reading scope["scheme"] alone would mark the cookie insecure on every
    tunnelled deployment, which is all of them.
    """
    r = await noauth_client.get("/?k=t0ken",
                                headers={"Accept": "text/html",
                                         "X-Forwarded-Proto": "https"},
                                follow_redirects=False)
    assert "Secure" in r.headers["set-cookie"]


async def test_the_cookie_is_not_secure_on_plain_http(noauth_client):
    """A Secure cookie over http is dropped, and localhost is a normal setup."""
    r = await noauth_client.get("/?k=t0ken", headers={"Accept": "text/html"},
                                follow_redirects=False)
    assert "Secure" not in r.headers["set-cookie"]


# ------------------------------------------------------------- signing out

async def test_signing_out_clears_the_cookie(noauth_client):
    r = await noauth_client.get("/logout", headers={"Cookie": "wa_session=t0ken"})
    assert r.status_code == 200
    cookie = r.headers["set-cookie"]
    assert "wa_session=;" in cookie
    assert "Max-Age=0" in cookie


async def test_the_cleared_cookie_expires_rather_than_being_empty(noauth_client):
    """An empty cookie still presents itself.

    It would then fail auth on every request instead of falling back to asking
    for the token, so the browser looks broken rather than signed out.
    """
    r = await noauth_client.get("/logout", headers={"Cookie": "wa_session=t0ken"})
    assert "Max-Age=0" in r.headers["set-cookie"]

    after = await noauth_client.get("/settings", headers={"Accept": "text/html"})
    assert after.status_code == 401


async def test_signing_out_does_not_unlink_whatsapp(noauth_client):
    """History syncs once, at pair time — a control that could cost the
    archive must not be the one labelled "sign out"."""
    body = (await noauth_client.get("/logout",
                                    headers={"Cookie": "wa_session=t0ken",
                                             "Accept": "text/html"})).text
    assert "still linked" in body
    assert "nothing has been deleted" in body


async def test_the_settings_page_offers_one_sign_out(client):
    """One control. Two read as "the browser one is the safe option", and the
    safe option left connectors with full access for thirty days."""
    page = (await client.get("/settings?k=t0ken")).text
    assert 'id="signout"' in page
    assert page.count(">Sign out</button>") == 1
    assert "Sign out of everything" in page


# ------------------------------------------------------ signing out everywhere

async def test_signing_out_expires_issued_credentials(client):
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


async def test_signing_out_spares_the_configured_token(client):
    """It comes from the environment and is re-registered on every start.

    Revoking it would lock you out until a restart and do nothing after one.
    """
    await client.post("/logout?k=t0ken")
    r = await client.get("/api/status?k=t0ken")
    assert r.status_code == 200


async def test_signing_out_also_clears_this_browser(client):
    r = await client.post("/logout?k=t0ken")
    assert "Max-Age=0" in r.headers["set-cookie"]


async def test_signing_out_reports_how_many(client):
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
