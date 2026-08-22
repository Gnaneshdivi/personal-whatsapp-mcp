"""The reply engine. This is the feature that gets numbers banned, so the
safety gates get more tests than anything else in the project."""
from __future__ import annotations

import httpx
import pytest

from wa_mcp.store.base import Message, now_ms
from wa_mcp.store.sqlite import SQLiteStore
from wa_mcp.trigger.backends import Context, dig, render
from wa_mcp.trigger.engine import Inbound, TriggerEngine
from wa_mcp.trigger.settings import TriggerSettings
from wa_mcp.whatsapp.contacts import ContactBook
from wa_mcp.whatsapp.sync import SyncTracker


class FakeWA:
    def __init__(self, ready=True):
        self.sync = SyncTracker()
        if ready:
            self.sync.connected()
            self.sync.offline_completed(0)
        self.push_name = "Gnanesh"
        self.sent: list[tuple[str, str]] = []
        self.typing: list[bool] = []
        self.fail = False

    async def send_text(self, to, text, reply_to=None):
        if self.fail:
            raise RuntimeError("socket down")
        self.sent.append((to, text))
        return {"message_id": f"gen{len(self.sent)}", "status": "sent"}

    async def set_typing(self, chat, typing=True):
        self.typing.append(typing)
        return {"status": "ok"}


class FakeRT:
    def __init__(self, store, wa):
        self.store = store
        self.wa = wa
        self.contacts = ContactBook("/nonexistent")


@pytest.fixture
async def rt(tmp_path):
    s = SQLiteStore(tmp_path / "app.db")
    await s.connect()
    yield FakeRT(s, FakeWA())
    await s.close()


def enabled(**over) -> TriggerSettings:
    s = TriggerSettings.from_dict({
        "enabled": True, "backend": "model",
        "model": {"base_url": "http://m", "model": "gpt", "api_key": "k"},
        "reply": {"personal": "all", "cooldown_seconds": 0, **over.pop("reply", {})},
        **over,
    })
    return s


def inbound(**kw) -> Inbound:
    base = dict(chat_jid="919812345678@s.whatsapp.net", message_id="m1",
                sender_jid="919812345678@s.whatsapp.net", text="hello",
                is_from_me=False, is_group=False)
    base.update(kw)
    return Inbound(**base)


def mock_model(reply="hi there", status=200):
    async def handler(request):
        if status >= 400:
            return httpx.Response(status, text="boom")
        return httpx.Response(200, json={"choices": [{"message": {"content": reply}}]})
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


# =================================================== defaults are off

async def test_a_fresh_install_says_nothing(rt):
    """The most important test in the file."""
    eng = TriggerEngine(rt)
    d = await eng.consider(inbound())
    assert d.fired is False
    assert "switched off" in d.reason
    assert rt.wa.sent == []


async def test_enabling_without_scope_still_says_nothing(rt):
    eng = TriggerEngine(rt)
    eng.settings = TriggerSettings.from_dict({
        "enabled": True, "model": {"base_url": "http://m", "model": "gpt"}})
    d = await eng.consider(inbound())
    assert d.fired is False
    assert "no chats are in scope" in d.reason


# =================================================== the hard safety gates

async def test_never_replies_to_our_own_message(rt):
    eng = TriggerEngine(rt); eng.settings = enabled(); eng._http = mock_model()
    d = await eng.consider(inbound(is_from_me=True))
    assert d.fired is False and "our own" in d.reason


async def test_loop_guard_blocks_our_own_generated_ids(rt):
    """A webhook that echoes text otherwise puts two bots in a forever loop."""
    eng = TriggerEngine(rt); eng.settings = enabled(); eng._http = mock_model()
    eng.note_generated("m1")
    d = await eng.consider(inbound(message_id="m1"))
    assert d.fired is False and "loop guard" in d.reason


async def test_a_sent_reply_is_registered_against_the_loop_guard(rt):
    eng = TriggerEngine(rt); eng.settings = enabled(); eng._http = mock_model()
    d = await eng.consider(inbound())
    assert d.fired is True
    assert "gen1" in eng._generated


async def test_nothing_fires_while_history_is_syncing(rt):
    """Sync replays OLD messages through the live path — the whole reason the
    gate exists."""
    rt.wa = FakeWA(ready=False)
    rt.wa.sync.connected()
    rt.wa.sync.offline_preview(total=500)
    eng = TriggerEngine(rt); eng.settings = enabled(); eng._http = mock_model()
    d = await eng.consider(inbound())
    assert d.fired is False and "syncing" in d.reason
    assert rt.wa.sent == []


async def test_status_broadcast_is_ignored(rt):
    eng = TriggerEngine(rt); eng.settings = enabled(); eng._http = mock_model()
    d = await eng.consider(inbound(chat_jid="status@broadcast"))
    assert d.fired is False and "pseudo-chat" in d.reason


async def test_empty_text_is_ignored(rt):
    eng = TriggerEngine(rt); eng.settings = enabled(); eng._http = mock_model()
    assert (await eng.consider(inbound(text="   "))).fired is False


# =================================================== scope

async def test_groups_are_off_by_default_even_when_personal_is_on(rt):
    eng = TriggerEngine(rt); eng.settings = enabled(); eng._http = mock_model()
    d = await eng.consider(inbound(chat_jid="1234-5678@g.us", is_group=True))
    assert d.fired is False and "group replies are off" in d.reason


async def test_enabled_groups_still_require_a_mention(rt):
    eng = TriggerEngine(rt)
    eng.settings = enabled(reply={"groups": "all"})
    eng._http = mock_model()
    d = await eng.consider(inbound(chat_jid="1234-5678@g.us", is_group=True))
    assert d.fired is False and "mentioned" in d.reason

    d = await eng.consider(inbound(chat_jid="1234-5678@g.us", is_group=True,
                                   mentioned_me=True, message_id="m2"))
    assert d.fired is True


async def test_personal_allowlist(rt):
    eng = TriggerEngine(rt)
    eng.settings = enabled(reply={"personal": "allowlist",
                                  "personal_allowlist": ["919999999999@s.whatsapp.net"]})
    eng._http = mock_model()
    assert (await eng.consider(inbound())).fired is False
    d = await eng.consider(inbound(chat_jid="919999999999@s.whatsapp.net", message_id="m2"))
    assert d.fired is True


async def test_allowlist_matches_despite_a_device_suffix(rt):
    eng = TriggerEngine(rt)
    eng.settings = enabled(reply={"personal": "allowlist",
                                  "personal_allowlist": ["919999999999@s.whatsapp.net"]})
    eng._http = mock_model()
    d = await eng.consider(inbound(chat_jid="919999999999:7@s.whatsapp.net"))
    assert d.fired is True


# =================================================== rate control

async def test_cooldown_collapses_a_burst_into_one_reply(rt):
    eng = TriggerEngine(rt)
    eng.settings = enabled(reply={"personal": "all", "cooldown_seconds": 30})
    eng._http = mock_model()
    assert (await eng.consider(inbound(message_id="m1"))).fired is True
    d = await eng.consider(inbound(message_id="m2"))
    assert d.fired is False and "cooldown" in d.reason
    assert len(rt.wa.sent) == 1


async def test_hourly_cap(rt):
    eng = TriggerEngine(rt)
    eng.settings = enabled(reply={"personal": "all", "cooldown_seconds": 0,
                                  "max_replies_per_hour": 2})
    eng._http = mock_model()
    for i in range(2):
        assert (await eng.consider(inbound(message_id=f"m{i}"))).fired is True
    d = await eng.consider(inbound(message_id="m9"))
    assert d.fired is False and "hourly cap" in d.reason


async def test_a_failing_send_still_burns_the_cooldown(rt):
    """Otherwise a broken backend retries as fast as messages arrive."""
    eng = TriggerEngine(rt)
    eng.settings = enabled(reply={"personal": "all", "cooldown_seconds": 30})
    eng._http = mock_model()
    rt.wa.fail = True
    d = await eng.consider(inbound(message_id="m1"))
    assert d.fired is False and "send failed" in d.reason
    d2 = await eng.consider(inbound(message_id="m2"))
    assert "cooldown" in d2.reason


# =================================================== backends

async def test_model_backend_sends_real_conversation_turns(rt):
    """History as roles, not a blob pasted into the prompt."""
    captured = {}

    async def handler(request):
        captured.update(request.read() and __import__("json").loads(request.read()))
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    await rt.store.upsert_message(Message("h1", "919812345678@s.whatsapp.net",
                                          now_ms() - 3000, text="are you free?"))
    await rt.store.upsert_message(Message("h2", "919812345678@s.whatsapp.net",
                                          now_ms() - 2000, text="yes, when?",
                                          is_from_me=True))
    eng = TriggerEngine(rt); eng.settings = enabled()
    eng._http = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    assert (await eng.consider(inbound(text="tomorrow"))).fired is True
    roles = [m["role"] for m in captured["messages"]]
    assert roles[0] == "system"
    assert "user" in roles and "assistant" in roles
    assert captured["messages"][-1]["content"] == "tomorrow"


async def test_model_http_error_is_reported_not_swallowed(rt):
    eng = TriggerEngine(rt); eng.settings = enabled()
    eng._http = mock_model(status=500)
    d = await eng.consider(inbound())
    assert d.fired is False and "HTTP 500" in d.reason


async def test_webhook_backend_reads_a_nested_reply_path(rt):
    async def handler(request):
        return httpx.Response(200, json={"data": {"answer": "from the webhook"}})

    eng = TriggerEngine(rt)
    eng.settings = TriggerSettings.from_dict({
        "enabled": True, "backend": "webhook",
        "webhook": {"url": "http://hook", "reply_path": "data.answer"},
        "reply": {"personal": "all", "cooldown_seconds": 0},
    })
    eng._http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    d = await eng.consider(inbound())
    assert d.fired is True and rt.wa.sent[0][1] == "from the webhook"


async def test_a_quote_in_the_message_does_not_break_the_json_body(rt):
    """Happens on day one, not eventually."""
    seen = {}

    async def handler(request):
        seen["body"] = __import__("json").loads(request.read())
        return httpx.Response(200, json={"reply": "ok"})

    eng = TriggerEngine(rt)
    eng.settings = TriggerSettings.from_dict({
        "enabled": True, "backend": "webhook",
        "webhook": {"url": "http://hook"},
        "reply": {"personal": "all", "cooldown_seconds": 0},
    })
    eng._http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    d = await eng.consider(inbound(text='he said "hello" \\ then left'))
    assert d.fired is True
    assert seen["body"]["text"]


# =================================================== output guards

async def test_an_over_long_reply_is_trimmed(rt):
    eng = TriggerEngine(rt)
    eng.settings = enabled(reply={"personal": "all", "cooldown_seconds": 0,
                                  "max_reply_chars": 40})
    eng._http = mock_model(reply="word " * 200)
    d = await eng.consider(inbound())
    assert d.fired is True and len(rt.wa.sent[0][1]) <= 41


async def test_empty_backend_reply_is_not_sent(rt):
    eng = TriggerEngine(rt); eng.settings = enabled(); eng._http = mock_model(reply="   ")
    d = await eng.consider(inbound())
    assert d.fired is False and "empty" in d.reason
    assert rt.wa.sent == []


async def test_typing_indicator_brackets_the_send(rt):
    eng = TriggerEngine(rt); eng.settings = enabled(); eng._http = mock_model()
    await eng.consider(inbound())
    assert rt.wa.typing == [True, False]


# =================================================== the decision log

async def test_every_decision_is_logged_with_a_reason(rt):
    eng = TriggerEngine(rt)
    await eng.consider(inbound())                       # off
    eng.settings = enabled(); eng._http = mock_model()
    await eng.consider(inbound(message_id="m2"))        # sent
    reasons = [e["reason"] for e in eng.log]
    assert "sent" in reasons
    assert any("switched off" in r for r in reasons)


# =================================================== settings

async def test_settings_round_trip_through_the_store(rt):
    eng = TriggerEngine(rt)
    await eng.save(enabled())
    fresh = TriggerEngine(rt)
    loaded = await fresh.load()
    assert loaded.enabled and loaded.model.model == "gpt"


async def test_secrets_are_redacted_for_display():
    s = TriggerSettings.from_dict({
        "model": {"api_key": "sk-secret"},
        "webhook": {"headers": {"Authorization": "Bearer x", "X-Trace": "keepme"}},
    })
    out = s.redacted()
    assert out["model"]["api_key"] == "***"
    assert out["webhook"]["headers"]["Authorization"] == "***"
    assert out["webhook"]["headers"]["X-Trace"] == "keepme"


def test_unknown_settings_keys_do_not_break_startup():
    s = TriggerSettings.from_dict({"enabled": True, "nonsense": 1,
                                   "model": {"model": "m", "future_field": 2}})
    assert s.enabled and s.model.model == "m"


# =================================================== helpers

def test_dig_walks_lists_and_dicts():
    assert dig({"choices": [{"message": {"content": "hi"}}]},
               "choices.0.message.content") == "hi"
    assert dig({"a": 1}, "b") is None
    assert dig({"a": {"b": None}}, "a.b") is None


def test_render_escapes_only_when_asked():
    ctx = Context(message='say "hi"', chat_name="A", chat_jid="j", sender_name="S",
                  sender_jid="sj", me_name="me", message_id="mid", timestamp="0",
                  history=[])
    assert render("{{message}}", ctx) == 'say "hi"'
    assert render("{{message}}", ctx, json_safe=True) == 'say \\"hi\\"'
