"""Disclosure, active hours, and summaries."""
from __future__ import annotations

import datetime as dt
import time

import httpx
import pytest

from wa_mcp.trigger.engine import TriggerEngine
from wa_mcp.trigger.settings import ActiveHours, TriggerSettings

from test_trigger import enabled, inbound, mock_model, rt          # noqa: F401


def with_disclosure(**over):
    s = enabled(**over)
    s.disclosure.enabled = True
    return s


# --------------------------------------------------------------- disclosure

async def test_the_first_reply_is_preceded_by_saying_it_is_a_bot(rt):
    eng = TriggerEngine(rt)
    eng.settings = with_disclosure()
    eng._http = mock_model(reply="Sure, tomorrow works.")
    d = await eng.consider(inbound())

    assert d.fired is True
    assert len(rt.wa.sent) == 2, "the disclosure should be its own message"
    assert "AI assistant" in rt.wa.sent[0][1]
    assert rt.wa.sent[1][1] == "Sure, tomorrow works."


async def test_it_is_said_once_per_chat_not_once_per_message(rt):
    """Repeating it every message is what makes people stop reading it."""
    eng = TriggerEngine(rt)
    eng.settings = with_disclosure()
    eng._http = mock_model(reply="ok")

    await eng.consider(inbound(message_id="m1"))
    await eng.consider(inbound(message_id="m2"))

    disclosures = [t for _, t in rt.wa.sent if "AI assistant" in t]
    assert len(disclosures) == 1


async def test_it_survives_a_restart(rt):
    """Remembered in the store, or every restart re-announces to everyone."""
    eng = TriggerEngine(rt)
    eng.settings = with_disclosure()
    eng._http = mock_model(reply="ok")
    await eng.consider(inbound(message_id="m1"))

    fresh = TriggerEngine(rt)              # as if the process had restarted
    fresh.settings = with_disclosure()
    fresh._http = mock_model(reply="ok")
    await fresh.consider(inbound(message_id="m2"))

    assert len([t for _, t in rt.wa.sent if "AI assistant" in t]) == 1


async def test_each_chat_is_told_separately(rt):
    eng = TriggerEngine(rt)
    eng.settings = with_disclosure()
    eng._http = mock_model(reply="ok")

    await eng.consider(inbound(message_id="a", chat_jid="1@s.whatsapp.net",
                               sender_jid="1@s.whatsapp.net"))
    await eng.consider(inbound(message_id="b", chat_jid="2@s.whatsapp.net",
                               sender_jid="2@s.whatsapp.net"))

    assert len([t for _, t in rt.wa.sent if "AI assistant" in t]) == 2


async def test_it_names_you(rt):
    eng = TriggerEngine(rt)
    eng.settings = with_disclosure()
    eng._http = mock_model(reply="ok")
    await eng.consider(inbound())
    assert "Gnanesh" in rt.wa.sent[0][1]


# -------------------------------------------------------------- the window

@pytest.mark.parametrize("hour,open_", [(8, False), (9, True), (20, True),
                                        (21, False), (3, False)])
def test_a_daytime_window(hour, open_):
    h = ActiveHours(enabled=True, start="09:00", end="21:00")
    assert h.open_at(dt.datetime(2026, 1, 1, hour, 30)) is open_


@pytest.mark.parametrize("hour,open_", [(21, False), (22, True), (2, True),
                                        (6, False)])
def test_a_window_that_crosses_midnight(hour, open_):
    """22:00-06:00 is a normal thing to want and must not read as empty."""
    h = ActiveHours(enabled=True, start="22:00", end="06:00")
    assert h.open_at(dt.datetime(2026, 1, 1, hour, 0)) is open_


def test_disabled_hours_never_block():
    assert ActiveHours().open_at(dt.datetime(2026, 1, 1, 3, 0)) is True


def test_a_malformed_time_does_not_close_the_gate_forever():
    """A typo in a text field must not silently stop every reply."""
    h = ActiveHours(enabled=True, start="not a time", end="also not")
    assert h.open_at(dt.datetime(2026, 1, 1, 12, 0)) is True


async def test_out_of_hours_stops_the_reply_but_not_the_watching(rt):
    eng = TriggerEngine(rt)
    eng.settings = enabled(notify={"route": "number", "jid": "919999999999",
                                   "on_keywords": ["urgent"]})
    eng.settings.hours = ActiveHours(enabled=True, start="09:00", end="09:01",
                                     timezone="UTC")
    eng._http = mock_model(reply="ok")
    d = await eng.consider(inbound(text="urgent!!"))

    assert d.fired is False
    assert "outside active hours" in d.reason
    assert d.notified, "watch rules must still fire when replies are held"


# --------------------------------------------------------------- summaries

async def test_nothing_happened_means_nothing_is_sent(rt):
    from wa_mcp.trigger import summary

    rt.trigger = TriggerEngine(rt)
    rt.trigger.settings = enabled(summary={"enabled": True, "route": "me"})
    rt.wa.self_jid = "919100828649@s.whatsapp.net"
    assert await summary.run_once(rt) is None
    assert rt.wa.sent == []


async def test_a_summary_names_the_important_things_first(rt):
    """The list is the point: a digest that buries them has failed."""
    from wa_mcp.trigger import summary

    captured = {}

    async def handler(request):
        import json
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"choices": [{"message": {
            "content": "Payment chased by Asif. Nothing else needs you."}}]})

    await rt.store.upsert_chat_meta("911@s.whatsapp.net", name="Asif")
    from wa_mcp.store.base import Message
    await rt.store.upsert_message(Message(
        message_id="x1", chat_jid="911@s.whatsapp.net", ts=int(time.time() * 1000),
        text="did the payment go out?", is_from_me=False))
    await rt.store.touch_chat("911@s.whatsapp.net", int(time.time() * 1000),
                              False, "did the payment go out?")

    rt.trigger = TriggerEngine(rt)
    rt.trigger.settings = enabled(summary={"enabled": True, "route": "me",
                                           "important": ["payment", "cancel"]})
    rt.trigger._http = None
    rt.wa.self_jid = "919100828649@s.whatsapp.net"

    import wa_mcp.trigger.summary as S
    orig = S.reply_via_model

    async def patched(cfg, ctx, client=None):
        return await orig(cfg, ctx,
                          httpx.AsyncClient(transport=httpx.MockTransport(handler)))

    S.reply_via_model = patched
    try:
        target = await summary.run_once(rt)
    finally:
        S.reply_via_model = orig

    assert target == "919100828649@s.whatsapp.net"
    prompt = captured["body"]["messages"][-1]["content"]
    assert "payment" in prompt and "cancel" in prompt
    assert "Call these out FIRST" in prompt
    assert any("Payment chased" in t for _, t in rt.wa.sent)


async def test_the_window_advances_even_on_a_quiet_run(rt):
    """Or a silent hour makes the next digest cover two."""
    from wa_mcp.trigger import summary

    rt.trigger = TriggerEngine(rt)
    rt.trigger.settings = enabled(summary={"enabled": True, "route": "me"})
    rt.wa.self_jid = "919100828649@s.whatsapp.net"
    await summary.run_once(rt)
    state = await rt.store.get_kv(summary.STATE_KEY)
    assert state and state["at"] > 0


def test_where_a_summary_goes():
    from wa_mcp.trigger.summary import destination

    s = TriggerSettings.from_dict({"summary": {"route": "me"}})
    assert destination(s, "919100828649@s.whatsapp.net") == "919100828649@s.whatsapp.net"

    s = TriggerSettings.from_dict({"summary": {"route": "number",
                                               "jid": "919999999999"}})
    assert destination(s, "x@s.whatsapp.net") == "919999999999@s.whatsapp.net"

    s = TriggerSettings.from_dict({"summary": {"route": "off"}})
    assert destination(s, "x@s.whatsapp.net") == ""


async def test_a_late_message_gets_the_out_of_hours_note_once_a_day(rt):
    """Silence at midnight sets no expectation; repeating it all night annoys."""
    eng = TriggerEngine(rt)
    eng.settings = enabled()
    eng.settings.hours = ActiveHours(enabled=True, start="09:00", end="09:01",
                                     timezone="UTC",
                                     after_hours_message="Back in the morning.")
    eng._http = mock_model(reply="ok")

    await eng.consider(inbound(message_id="m1"))
    await eng.consider(inbound(message_id="m2"))

    notes = [t for _, t in rt.wa.sent if t == "Back in the morning."]
    assert len(notes) == 1
    assert not any(t == "ok" for _, t in rt.wa.sent), "it still must not reply"


async def test_no_note_configured_means_silence(rt):
    eng = TriggerEngine(rt)
    eng.settings = enabled()
    eng.settings.hours = ActiveHours(enabled=True, start="09:00", end="09:01",
                                     timezone="UTC")
    eng._http = mock_model(reply="ok")
    await eng.consider(inbound())
    assert rt.wa.sent == []


def test_the_model_is_told_not_to_mirror_how_it_is_addressed():
    """It replied "Hi ganny bhai" to "Hi".

    "ganny bhai" is that contact's nickname for the ACCOUNT OWNER, so the bot
    greeted him with his own name for someone else. Not editable, because
    getting this wrong is not a matter of taste.
    """
    from wa_mcp.trigger.backends import Context, compose_instruction

    ctx = Context(message="Hi", chat_name="Akbar", chat_jid="1@s.whatsapp.net",
                  sender_name="Akbar", sender_jid="1@s.whatsapp.net",
                  me_name="Gnanesh", message_id="m", timestamp="0", history=[])
    ctx.system = "You are replying as Gnanesh."
    out = compose_instruction(ctx, "nonce")
    assert "their name for the account owner" in out
    assert "Never echo it back" in out


def test_the_disclosure_names_the_owner_not_the_word_me():
    """me_name was empty, so it said "on behalf of me" — meaning nothing."""
    from wa_mcp.trigger.backends import Context, render
    from wa_mcp.trigger.settings import Disclosure

    ctx = Context(message="Hi", chat_name="Akbar", chat_jid="1@s.whatsapp.net",
                  sender_name="Akbar", sender_jid="1@s.whatsapp.net",
                  me_name="Gnanesh", message_id="m", timestamp="0", history=[])
    out = render(Disclosure().message, ctx)
    assert "on behalf of Gnanesh" in out
    assert "on behalf of me" not in out


def test_the_summary_asks_for_attention_items_as_points():
    """"Check this" is the thing that must never be buried in prose."""
    from wa_mcp.trigger.summary import PROMPT

    out = PROMPT.format(me_name="Gnanesh", important="", body="…")
    assert "NEEDS YOU" in out
    for ask in ("check, look at, review, confirm or approve",
                "direct question that has not been answered",
                "waiting on a reply, a decision, a file, a payment or a date",
                "chases something asked about before"):
        assert ask in out, ask
    assert "who, what they want, and the deadline" in out
