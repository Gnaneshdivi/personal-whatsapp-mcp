"""The settings page and its save path.

The page this replaced had a collector that pre-declared `{model, webhook,
reply}` and then wrote `o[parent][child]` for every dotted field name. The
first `guardrails.*` field therefore threw on `undefined`, the submit handler
died before calling fetch, and Save appeared to do nothing at all -- no
request, no error message, nothing in the log. It stayed that way because
every test posted JSON straight at the API, which worked fine.

So these test the two halves that were never connected: that the form actually
contains a control for everything the settings object persists, and that a
payload shaped like the one the form produces survives the round trip.
"""
from __future__ import annotations

import dataclasses
import json

import httpx
import pytest

from wa_mcp.config import Settings, resolve_storage


@pytest.fixture
async def client(tmp_path, monkeypatch):
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


K = "?k=t0ken"

# Persisted but deliberately not on the form: derived, or set by pairing.
NOT_ON_FORM = {"webhook.prompt_template_json"}


def _leaf_names(obj, prefix="") -> set[str]:
    """Every dotted field name the settings object persists."""
    out = set()
    for f in dataclasses.fields(obj):
        v = getattr(obj, f.name)
        name = f"{prefix}{f.name}"
        if dataclasses.is_dataclass(v):
            out |= _leaf_names(v, f"{name}.")
        else:
            out.add(name)
    return out


async def test_the_form_has_a_control_for_every_persisted_field(client):
    """A field added to the dataclass with no control is invisible and unsettable.

    Worse, it silently reverts to its default on every save, because the form
    posts a complete object and anything absent is defaulted.
    """
    from wa_mcp.trigger.settings import TriggerSettings

    page = (await client.get(f"/settings{K}")).text
    missing = sorted(n for n in _leaf_names(TriggerSettings())
                     if n not in NOT_ON_FORM and f'name="{n}"' not in page)
    assert not missing, f"no control on the settings form for: {missing}"


async def test_the_page_renders(client):
    r = await client.get(f"/settings{K}")
    assert r.status_code == 200
    assert "Traceback" not in r.text


FULL = {
    "enabled": True, "backend": "webhook", "show_typing": False,
    "send_media": True, "max_media_bytes": 1234567,
    "model": {"base_url": "https://o.test/v1", "api_key": "sk-live",
              "model": "m1", "system_prompt": "be brief",
              "history_messages": 7, "max_tokens": 222},
    "webhook": {"url": "https://e.test/hook", "method": "POST",
                "headers": {"Authorization": "Bearer abc"},
                "body": '{"text": "{{prompt}}"}', "reply_path": "data.reply",
                "prompt_template": "{{message}}", "history_messages": 5},
    "guardrails": {"context_only": True, "allow_external_knowledge": True,
                   "allowed_topics": ["orders", "delivery"],
                   "require_allowed_topic": True,
                   "blocked_topics": ["legal advice"],
                   "blocked_keywords": ["refund"], "policy_note": "no dates",
                   "fallback_message": "I will check.",
                   "send_fallback_when_blocked": False,
                   "send_fallback_on_error": True},
    "reply": {"personal": "allowlist", "personal_allowlist": ["1@s.whatsapp.net"],
              "groups": "none", "groups_allowlist": [],
              "require_mention_in_groups": True, "cooldown_seconds": 45,
              "max_replies_per_hour": 17, "max_reply_chars": 900},
    "notify": {"jid": "919100828649", "on_handoff": True, "on_blocked": True,
               "on_error": True, "on_keywords": ["urgent"],
               "vip_contacts": ["1@s.whatsapp.net"],
               "watch_groups": True, "handoff_marker": "[[NOTIFY]]"},
}


async def test_a_full_payload_round_trips(client):
    """Every field, including the guardrails and notify ones Save used to drop."""
    r = await client.post(f"/api/settings{K}", json=FULL)
    assert r.status_code == 200 and r.json()["ok"] is True

    page = (await client.get(f"/settings{K}")).text
    for probe in ('value="45"', 'value="17"', 'value="orders, delivery"',
                  'value="data.reply"', "Bearer abc", 'value="1234567"',
                  'value="webhook" selected', 'name="enabled" checked'):
        assert probe in page, f"{probe!r} did not survive the round trip"


async def test_guardrails_and_notify_are_persisted_not_defaulted(client):
    """The exact regression: these two branches silently never saved."""
    await client.post(f"/api/settings{K}", json=FULL)

    from wa_mcp.app import RT
    g, n = RT.trigger.settings.guardrails, RT.trigger.settings.notify
    assert g.blocked_keywords == ["refund"]
    assert g.allowed_topics == ["orders", "delivery"]
    assert g.send_fallback_when_blocked is False
    assert n.on_keywords == ["urgent"]
    assert n.watch_groups is True
    assert n.jid == "919100828649"


async def test_the_masked_key_does_not_wipe_the_real_one(client):
    """The form renders *** rather than the key, and posts it back unchanged."""
    await client.post(f"/api/settings{K}", json=FULL)
    masked = json.loads(json.dumps(FULL))
    masked["model"]["api_key"] = "***"
    await client.post(f"/api/settings{K}", json=masked)

    from wa_mcp.app import RT
    assert RT.trigger.settings.model.api_key == "sk-live"


async def test_the_key_is_never_rendered_into_the_page(client):
    await client.post(f"/api/settings{K}", json=FULL)
    assert "sk-live" not in (await client.get(f"/settings{K}")).text


async def test_documented_tags_match_what_the_engine_substitutes():
    """The page lists {{tokens}}; drift makes the documentation a lie."""
    from wa_mcp.settings_ui import TOKENS
    from wa_mcp.trigger.backends import Context

    ctx = Context(message="", chat_name="", chat_jid="", sender_name="",
                  sender_jid="", me_name="", message_id="", timestamp="",
                  history=[])
    real = set(ctx.tokens()) | {"prompt"}      # prompt is added for webhooks
    documented = {t.strip("{}") for t, _ in TOKENS}
    assert documented <= real, f"documented but not substituted: {documented - real}"
    assert real <= documented, f"substituted but undocumented: {real - documented}"


async def test_saving_during_a_sync_says_why_it_is_not_replying(client):
    """"Not firing: —" told nobody anything.

    Two separate things hold a reply back: settings that are not usable, and
    the sync gate. Only the first carried a reason, so a valid config saved
    while history was still arriving reported a blank one.
    """
    from wa_mcp.app import RT

    live = dict(FULL, enabled=True, backend="model")
    live["model"] = dict(FULL["model"], api_key="sk-live")
    live["reply"] = dict(FULL["reply"], personal="all")

    r = await client.post(f"/api/settings{K}", json=live)
    d = r.json()
    assert d["ok"] is True
    # Unpaired in tests, so the gate is shut and it must say so.
    assert d["would_fire"] is False
    assert d["blocked_by"], "not firing with no reason given"
    assert "syncing" in d["blocked_by"] or "off" in d["blocked_by"]


async def test_an_unusable_config_still_names_the_missing_piece(client):
    r = await client.post(f"/api/settings{K}", json=dict(FULL, enabled=True,
                                                         backend="webhook",
                                                         webhook={"url": ""}))
    assert "webhook url" in r.json()["blocked_by"]
