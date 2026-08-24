"""Guardrails, grounding, notification routing and image replies."""
from __future__ import annotations

import httpx
import pytest

from wa_mcp.store.sqlite import SQLiteStore
from wa_mcp.trigger.backends import extract_media, kind_for
from wa_mcp.trigger.engine import Inbound, TriggerEngine
from wa_mcp.trigger.settings import Guardrails, TriggerSettings
from wa_mcp.whatsapp.contacts import ContactBook
from wa_mcp.whatsapp.sync import SyncTracker


class FakeWA:
    def __init__(self):
        self.sync = SyncTracker()
        self.sync.connected(); self.sync.offline_completed(0)
        self.push_name = "Shop"
        self.sent: list[tuple[str, str]] = []
        self.media: list[tuple[str, str]] = []

    async def send_text(self, to, text, reply_to=None):
        self.sent.append((to, text))
        return {"message_id": f"g{len(self.sent)}"}

    async def send_media(self, to, b64, kind="image", caption=None, filename=None):
        self.media.append((to, kind))
        return {"message_id": f"i{len(self.media)}"}

    async def set_typing(self, chat, typing=True):
        return {"status": "ok"}


class FakeRT:
    def __init__(self, store):
        self.store = store
        self.wa = FakeWA()
        self.contacts = ContactBook("/nonexistent")


@pytest.fixture
async def rt(tmp_path):
    s = SQLiteStore(tmp_path / "a.db"); await s.connect()
    yield FakeRT(s)
    await s.close()


def settings(**over) -> TriggerSettings:
    base = {
        "enabled": True, "backend": "model",
        "model": {"base_url": "http://m", "model": "gpt"},
        "reply": {"personal": "all", "cooldown_seconds": 0},
        # Off unless a test is about it: these assert on the reply, and the
        # disclosure is a separate message that would shift every index.
        "disclosure": {"enabled": False},
    }
    base.update(over)
    return TriggerSettings.from_dict(base)


def inbound(text="hello", **kw):
    d = dict(chat_jid="911@s.whatsapp.net", message_id="m1",
             sender_jid="911@s.whatsapp.net", text=text,
             is_from_me=False, is_group=False)
    d.update(kw)
    return Inbound(**d)


def model(reply="ok", capture=None):
    async def handler(request):
        if capture is not None:
            import json
            capture.update(json.loads(request.read()))
        return httpx.Response(200, json={"choices": [{"message": {"content": reply}}]})
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


# ============================================================ grounding

def test_context_only_is_the_default():
    g = Guardrails()
    assert g.context_only is True
    assert g.allow_external_knowledge is False
    p = g.as_prompt()
    assert "only from this conversation" in p
    assert "do not invent" in p.lower()


def test_external_knowledge_is_stated_in_words_not_implied():
    """The flag has to be visible to the model, not just an absent restriction."""
    p = Guardrails(allow_external_knowledge=True).as_prompt()
    assert "general knowledge" in p
    assert "search" in p
    assert "only from this conversation" not in p


async def test_the_grounding_rule_reaches_the_system_message(rt):
    captured: dict = {}
    eng = TriggerEngine(rt); eng.settings = settings(); eng._http = model(capture=captured)
    await eng.consider(inbound())
    system = captured["messages"][0]
    assert system["role"] == "system"
    assert "only from this conversation" in system["content"]


async def test_turning_external_on_changes_what_the_model_is_told(rt):
    captured: dict = {}
    eng = TriggerEngine(rt)
    eng.settings = settings(guardrails={"allow_external_knowledge": True})
    eng._http = model(capture=captured)
    await eng.consider(inbound())
    assert "general knowledge" in captured["messages"][0]["content"]


# ============================================================ blocklist

async def test_a_blocked_keyword_never_reaches_the_model(rt):
    """Deterministic and pre-model — it cannot be talked around."""
    called = []

    async def handler(request):
        called.append(1)
        return httpx.Response(200, json={"choices": [{"message": {"content": "x"}}]})

    eng = TriggerEngine(rt)
    eng.settings = settings(guardrails={"blocked_keywords": ["refund"]})
    eng._http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    d = await eng.consider(inbound("can i get a REFUND please"))
    assert d.fired is False and "blocked keyword" in d.reason
    assert called == []


async def test_a_blocked_message_still_gets_the_fallback(rt):
    eng = TriggerEngine(rt)
    eng.settings = settings(guardrails={"blocked_keywords": ["refund"],
                                        "fallback_message": "A human will reply."})
    eng._http = model()
    await eng.consider(inbound("refund now"))
    assert rt.wa.sent and rt.wa.sent[0][1] == "A human will reply."


async def test_repeating_a_blocked_word_does_not_spam_the_fallback(rt):
    eng = TriggerEngine(rt)
    eng.settings = settings(reply={"personal": "all", "cooldown_seconds": 60},
                            guardrails={"blocked_keywords": ["refund"]})
    eng._http = model()
    await eng.consider(inbound("refund", message_id="m1"))
    await eng.consider(inbound("refund again", message_id="m2"))
    assert len(rt.wa.sent) == 1


async def test_fallback_can_be_switched_off(rt):
    eng = TriggerEngine(rt)
    eng.settings = settings(guardrails={"blocked_keywords": ["refund"],
                                        "send_fallback_when_blocked": False})
    eng._http = model()
    d = await eng.consider(inbound("refund"))
    assert d.fired is False and rt.wa.sent == []


# ============================================================ allowlist

async def test_require_allowed_topic_rejects_everything_else(rt):
    eng = TriggerEngine(rt)
    eng.settings = settings(guardrails={"allowed_topics": ["order", "delivery"],
                                        "require_allowed_topic": True})
    eng._http = model()
    d = await eng.consider(inbound("what do you think about politics"))
    assert d.fired is False and "allowed topic" in d.reason

    d = await eng.consider(inbound("where is my order", message_id="m2"))
    assert d.fired is True


async def test_allowed_topics_are_also_told_to_the_model(rt):
    captured: dict = {}
    eng = TriggerEngine(rt)
    eng.settings = settings(guardrails={"allowed_topics": ["billing"]})
    eng._http = model(capture=captured)
    await eng.consider(inbound("about billing"))
    assert "billing" in captured["messages"][0]["content"]


# ============================================================ notify

async def test_handoff_marker_is_stripped_before_the_customer_sees_it(rt):
    eng = TriggerEngine(rt)
    eng.settings = settings(notify={"on_handoff": True, "jid": "919999999999"})
    eng._http = model(reply="I'll get someone. [[NOTIFY]]")
    d = await eng.consider(inbound())
    customer_msg = rt.wa.sent[0][1]
    assert "[[NOTIFY]]" not in customer_msg
    assert customer_msg == "I'll get someone."
    assert d.notified is not None


async def test_notification_goes_to_the_configured_number(rt):
    """The business case: customers write to one line, the owner reads another."""
    eng = TriggerEngine(rt)
    eng.settings = settings(notify={"jid": "919999999999", "on_handoff": True})
    eng._http = model(reply="one moment [[NOTIFY]]")
    d = await eng.consider(inbound())
    assert d.notified == "919999999999@s.whatsapp.net"
    targets = [to for to, _ in rt.wa.sent]
    assert "919999999999@s.whatsapp.net" in targets
    assert "911@s.whatsapp.net" in targets      # the customer still got a reply


async def test_with_no_number_configured_nothing_is_alerted(rt):
    """Blank means off, not "send it to whoever wrote in".

    The alert reads "Needs you: … Their message: … Reason: …". Falling back to
    the incoming chat delivered that to the person it is about — internal
    wording sent to a customer — and made clearing the field look like it
    disabled alerts while it silently redirected them.
    """
    eng = TriggerEngine(rt)
    eng.settings = settings(notify={"on_handoff": True})     # no jid
    eng._http = model(reply="hold on [[NOTIFY]]")
    d = await eng.consider(inbound())
    assert d.notified is None
    # The reply still goes out; only the alert is withheld.
    assert [to for to, _ in rt.wa.sent] == ["911@s.whatsapp.net"]
    assert "Needs you" not in rt.wa.sent[0][1]


async def test_notify_on_backend_error(rt):
    async def handler(request):
        return httpx.Response(500, text="down")

    eng = TriggerEngine(rt)
    eng.settings = settings(notify={"jid": "919999999999", "on_error": True})
    eng._http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    d = await eng.consider(inbound())
    assert d.fired is False
    assert any(to == "919999999999@s.whatsapp.net" for to, _ in rt.wa.sent)


async def test_the_notification_carries_the_reason_and_who_it_was(rt):
    eng = TriggerEngine(rt)
    eng.settings = settings(notify={"jid": "919999999999", "on_handoff": True})
    eng._http = model(reply="[[NOTIFY]] wait")
    await eng.consider(inbound("I need to speak to a person"))
    alert = next(t for to, t in rt.wa.sent if to.startswith("919999999999"))
    assert "911@s.whatsapp.net" in alert
    assert "I need to speak to a person" in alert
    assert "human" in alert


# ============================================================= media

def test_media_extraction_handles_markdown_and_bare_urls():
    text, urls = extract_media("here you go ![cat](https://x.io/c.png) enjoy")
    assert urls == ["https://x.io/c.png"]
    assert text == "here you go enjoy"

    text, urls = extract_media("see https://y.io/a.jpg?v=2 ok")
    assert urls == ["https://y.io/a.jpg?v=2"]

    assert extract_media("plain text") == ("plain text", [])


async def test_generated_image_is_downloaded_and_sent_as_a_photo(rt):
    png = b"\x89PNG\r\n\x1a\n" + b"0" * 64

    async def handler(request):
        if "chat/completions" in str(request.url):
            return httpx.Response(200, json={"choices": [{"message": {
                "content": "Here it is ![](https://img.test/a.png)"}}]})
        return httpx.Response(200, content=png, headers={"content-type": "image/png"})

    eng = TriggerEngine(rt)
    eng.settings = settings(send_media=True)
    eng._http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    d = await eng.consider(inbound())
    assert d.fired is True and d.media == 1
    assert rt.wa.media == [("911@s.whatsapp.net", "image")]
    assert "https://img.test" not in rt.wa.sent[0][1]


async def test_images_are_left_as_links_when_the_flag_is_off(rt):
    eng = TriggerEngine(rt)
    eng.settings = settings(send_media=False)
    eng._http = model(reply="look ![](https://img.test/a.png)")
    d = await eng.consider(inbound())
    assert d.media == 0 and rt.wa.media == []
    assert "img.test" in rt.wa.sent[0][1]


async def test_a_web_page_is_refused(rt):
    async def handler(request):
        if "chat/completions" in str(request.url):
            return httpx.Response(200, json={"choices": [{"message": {
                "content": "file ![](https://img.test/a.png)"}}]})
        return httpx.Response(200, content=b"<html>", headers={"content-type": "text/html"})

    eng = TriggerEngine(rt)
    eng.settings = settings(send_media=True)
    eng._http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    d = await eng.consider(inbound())
    assert d.fired is True and d.media == 0     # text still went, file did not
    assert rt.wa.media == []


async def test_an_oversized_image_is_refused(rt):
    async def handler(request):
        if "chat/completions" in str(request.url):
            return httpx.Response(200, json={"choices": [{"message": {
                "content": "![](https://img.test/big.png)"}}]})
        return httpx.Response(200, content=b"0" * 5000,
                              headers={"content-type": "image/png"})

    eng = TriggerEngine(rt)
    eng.settings = settings(send_media=True, max_media_bytes=1000)
    eng._http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    d = await eng.consider(inbound())
    assert d.media == 0 and rt.wa.media == []


async def test_an_image_only_reply_still_sends(rt):
    png = b"\x89PNG\r\n\x1a\n" + b"0" * 32

    async def handler(request):
        if "chat/completions" in str(request.url):
            return httpx.Response(200, json={"choices": [{"message": {
                "content": "![](https://img.test/a.png)"}}]})
        return httpx.Response(200, content=png, headers={"content-type": "image/png"})

    eng = TriggerEngine(rt)
    eng.settings = settings(send_media=True)
    eng._http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    d = await eng.consider(inbound())
    assert d.fired is True and d.media == 1
    assert rt.wa.sent == []          # nothing to say, only a picture


async def test_sent_images_are_registered_against_the_loop_guard(rt):
    png = b"\x89PNG\r\n\x1a\n"

    async def handler(request):
        if "chat/completions" in str(request.url):
            return httpx.Response(200, json={"choices": [{"message": {
                "content": "![](https://img.test/a.png)"}}]})
        return httpx.Response(200, content=png, headers={"content-type": "image/png"})

    eng = TriggerEngine(rt)
    eng.settings = settings(send_media=True)
    eng._http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    await eng.consider(inbound())
    assert "i1" in eng._generated


# ====================================================== notify me when

async def test_watch_keyword_alerts_even_with_replies_off(rt):
    """The point of watch rules: monitor a number without answering on it."""
    eng = TriggerEngine(rt)
    eng.settings = TriggerSettings.from_dict({
        "enabled": False,
        "notify": {"jid": "919999999999", "on_keywords": ["urgent"]},
    })
    d = await eng.consider(inbound("this is URGENT please help"))
    assert d.fired is False                      # no reply
    assert d.notified == "919999999999@s.whatsapp.net"
    assert any(to.startswith("919999999999") for to, _ in rt.wa.sent)


async def test_a_non_matching_message_alerts_nobody(rt):
    eng = TriggerEngine(rt)
    eng.settings = TriggerSettings.from_dict({
        "notify": {"jid": "919999999999", "on_keywords": ["urgent"]}})
    d = await eng.consider(inbound("just saying hello"))
    assert d.notified is None and rt.wa.sent == []


async def test_a_watched_contact_always_alerts(rt):
    eng = TriggerEngine(rt)
    eng.settings = TriggerSettings.from_dict({
        "notify": {"jid": "919999999999", "vip_contacts": ["911@s.whatsapp.net"]}})
    d = await eng.consider(inbound("morning"))
    assert d.notified is not None
    alert = next(t for to, t in rt.wa.sent if to.startswith("919999999999"))
    assert "watched contact" in alert


async def test_groups_are_not_watched_unless_asked(rt):
    eng = TriggerEngine(rt)
    eng.settings = TriggerSettings.from_dict({
        "notify": {"jid": "919999999999", "on_keywords": ["urgent"]}})
    d = await eng.consider(inbound("urgent", chat_jid="1-2@g.us", is_group=True))
    assert d.notified is None

    eng.settings = TriggerSettings.from_dict({
        "notify": {"jid": "919999999999", "on_keywords": ["urgent"],
                   "watch_groups": True}})
    d = await eng.consider(inbound("urgent", chat_jid="1-2@g.us",
                                   is_group=True, message_id="m2"))
    assert d.notified is not None


async def test_watching_is_held_during_sync_like_replies(rt):
    """History replay would otherwise alert for every old matching message."""
    from wa_mcp.whatsapp.sync import SyncTracker
    rt.wa.sync = SyncTracker()
    rt.wa.sync.connected(); rt.wa.sync.offline_preview(total=200)
    eng = TriggerEngine(rt)
    eng.settings = TriggerSettings.from_dict({
        "notify": {"jid": "919999999999", "on_keywords": ["urgent"]}})
    d = await eng.consider(inbound("urgent"))
    assert d.notified is None and rt.wa.sent == []


async def test_a_watched_message_can_also_be_replied_to(rt):
    eng = TriggerEngine(rt)
    eng.settings = settings(notify={"jid": "919999999999", "on_keywords": ["urgent"]})
    eng._http = model(reply="on it")
    d = await eng.consider(inbound("urgent problem"))
    assert d.fired is True and d.notified is not None
    targets = [to for to, _ in rt.wa.sent]
    assert "911@s.whatsapp.net" in targets           # the reply
    assert "919999999999@s.whatsapp.net" in targets  # the alert


async def test_our_own_messages_never_trigger_a_watch(rt):
    eng = TriggerEngine(rt)
    eng.settings = TriggerSettings.from_dict({
        "notify": {"jid": "919999999999", "on_keywords": ["urgent"]}})
    d = await eng.consider(inbound("urgent", is_from_me=True))
    assert d.notified is None


# --------------------------------------------------- media beyond images

def test_each_kind_is_routed_to_the_right_sender():
    """WhatsApp needs to be told what an attachment is.

    Sending a voice note as a photo does not degrade -- it fails. Anything
    unrecognised becomes a document, which always works.
    """
    assert kind_for("https://x.io/chart.png") == "image"
    assert kind_for("https://x.io/clip.mp4") == "video"
    assert kind_for("https://x.io/note.ogg") == "audio"
    assert kind_for("https://x.io/report.pdf") == "document"
    assert kind_for("https://x.io/thing.bin") == "document"


def test_the_mime_type_breaks_ties_when_there_is_no_extension():
    """Signed URLs and CDN redirects routinely arrive with no extension."""
    assert kind_for("https://x.io/download?id=9", "image/png") == "image"
    assert kind_for("https://x.io/download?id=9", "audio/mpeg") == "audio"
    assert kind_for("https://x.io/download?id=9", "") == "document"


def test_a_query_string_does_not_hide_the_extension():
    assert kind_for("https://x.io/a.mp4?token=abc&x=1") == "video"


def test_extraction_picks_up_documents_and_audio_not_just_pictures():
    text, urls = extract_media("the report https://x.io/q3.pdf and a note "
                               "[voice](https://x.io/v.ogg)")
    assert urls == ["https://x.io/q3.pdf", "https://x.io/v.ogg"]
    assert "x.io" not in text


async def test_a_pdf_is_sent_as_a_document(rt):
    """The end-to-end shape of the change: a non-image lands as an attachment."""
    async def handler(request):
        if "chat/completions" in str(request.url):
            return httpx.Response(200, json={"choices": [{"message": {
                "content": "here [report](https://img.test/q3.pdf)"}}]})
        return httpx.Response(200, content=b"%PDF-1.4 ...",
                              headers={"content-type": "application/pdf"})

    eng = TriggerEngine(rt)
    eng.settings = settings(send_media=True)
    eng._http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    d = await eng.consider(inbound())
    assert d.fired is True and d.media == 1
    assert rt.wa.media == [("911@s.whatsapp.net", "document")]


async def test_a_saved_config_from_before_the_mute_list_still_loads(rt):
    """`Never alert me about` was removed. Anyone who saved one has the key.

    from_dict drops what it does not recognise, so this is only a guard against
    someone later making it strict and breaking every existing install.
    """
    eng = TriggerEngine(rt)
    eng.settings = TriggerSettings.from_dict({
        "notify": {"jid": "919999999999", "on_keywords": ["urgent"],
                   "mute_contacts": ["911@s.whatsapp.net"]}})
    d = await eng.consider(inbound("urgent!!"))
    assert d.notified is not None, "the stale key should be ignored, not obeyed"


# ------------------------------------------------------- where alerts go

async def test_alerts_go_to_your_own_chat_when_route_is_me(rt):
    """The sensible setting on a personal number: your Message-yourself chat."""
    rt.wa.self_jid = "919100828649@s.whatsapp.net"
    eng = TriggerEngine(rt)
    eng.settings = TriggerSettings.from_dict({
        "notify": {"route": "me", "on_keywords": ["urgent"]}})
    d = await eng.consider(inbound("urgent!!"))
    assert d.notified == "919100828649@s.whatsapp.net"
    assert any(to.startswith("919100828649") and "Needs you" in t
               for to, t in rt.wa.sent)


async def test_route_chat_sends_the_alert_to_the_person_it_is_about(rt):
    """Allowed, but only on purpose — they read it.

    Kept as a choice because someone watching their own personal number may
    genuinely want the note in the thread. It is no longer what an empty field
    silently does.
    """
    eng = TriggerEngine(rt)
    eng.settings = TriggerSettings.from_dict({
        "notify": {"route": "chat", "on_keywords": ["urgent"]}})
    d = await eng.consider(inbound("urgent!!"))
    assert d.notified == "911@s.whatsapp.net"


async def test_route_number_uses_the_configured_phone(rt):
    eng = TriggerEngine(rt)
    eng.settings = TriggerSettings.from_dict({
        "notify": {"route": "number", "jid": "919999999999",
                   "on_keywords": ["urgent"]}})
    d = await eng.consider(inbound("urgent!!"))
    assert d.notified == "919999999999@s.whatsapp.net"


async def test_route_off_sends_nothing(rt):
    eng = TriggerEngine(rt)
    eng.settings = TriggerSettings.from_dict({
        "notify": {"route": "off", "jid": "919999999999",
                   "on_keywords": ["urgent"]}})
    d = await eng.consider(inbound("urgent!!"))
    assert d.notified is None and rt.wa.sent == []


async def test_route_number_with_no_number_sends_nothing(rt):
    """Half-configured must fail closed, not fall back to the customer."""
    eng = TriggerEngine(rt)
    eng.settings = TriggerSettings.from_dict({
        "notify": {"route": "number", "jid": "", "on_keywords": ["urgent"]}})
    d = await eng.consider(inbound("urgent!!"))
    assert d.notified is None and rt.wa.sent == []


async def test_a_config_saved_before_routes_existed_keeps_alerting(rt):
    """Anyone who set a number did so to be alerted; that must survive."""
    eng = TriggerEngine(rt)
    eng.settings = TriggerSettings.from_dict({
        "notify": {"jid": "919999999999", "on_keywords": ["urgent"]}})
    assert eng.settings.notify.route == "number"
    d = await eng.consider(inbound("urgent!!"))
    assert d.notified == "919999999999@s.whatsapp.net"


async def test_the_alert_carries_a_tappable_link_to_the_chat(rt):
    """A bare address is not a link anywhere.

    Reading an alert used to mean copying the number out by hand to find the
    conversation it was about. WhatsApp turns a wa.me URL into a tap.
    """
    rt.wa.self_jid = "919100828649@s.whatsapp.net"
    eng = TriggerEngine(rt)
    eng.settings = TriggerSettings.from_dict({
        "notify": {"route": "me", "on_keywords": ["urgent"]}})
    await eng.consider(inbound("urgent!!"))
    alert = next(t for to, t in rt.wa.sent if "Needs you" in t)
    assert "https://wa.me/911" in alert


async def test_a_lid_sender_gets_no_link_rather_than_a_wrong_one(rt):
    """LIDs are all digits but are not phone numbers.

    Building wa.me from one produces a link to a number that is not theirs and
    may well be somebody else's, which is worse than no link at all.
    """
    from wa_mcp.trigger.backends import Context

    ctx = Context(message="hi", chat_name="C", chat_jid="207696196305131@lid",
                  sender_name="S", sender_jid="207696196305131@lid",
                  me_name="me", message_id="m", timestamp="0", history=[])
    assert ctx.chat_link() == ""
    assert "wa.me" not in ctx.tokens()["chat_link"]


# ------------------------------------------------------ backend endpoint

@pytest.mark.parametrize("base", [
    "https://openrouter.ai/api/v1",
    "https://openrouter.ai/api/v1/",
    "https://openrouter.ai/api/v1/chat/completions",     # what providers document
    "https://openrouter.ai/api/v1/chat/completions/",
])
async def test_the_endpoint_is_reached_however_the_base_url_was_written(base):
    """Providers document the full endpoint, so that is what gets pasted.

    Appending blindly produced .../chat/completions/chat/completions, a 404,
    and a silent no-reply with nothing to point at.
    """
    import httpx

    from wa_mcp.trigger.backends import Context, reply_via_model
    from wa_mcp.trigger.settings import ModelBackend

    seen = {}

    async def handler(request):
        seen["url"] = str(request.url)
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    ctx = Context(message="hi", chat_name="C", chat_jid="1@s.whatsapp.net",
                  sender_name="S", sender_jid="1@s.whatsapp.net", me_name="me",
                  message_id="m", timestamp="0", history=[])
    cfg = ModelBackend(base_url=base, api_key="k", model="m")
    await reply_via_model(cfg, ctx,
                          httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    assert seen["url"] == "https://openrouter.ai/api/v1/chat/completions"


async def test_a_dead_backend_is_logged_loudly_not_at_debug(rt, caplog):
    """A broken backend is not the same as a message that was out of scope.

    Both were logged at debug, so a misconfigured endpoint looked exactly like
    normal quiet operation — no reply, nothing in the log, nothing to chase.
    """
    import logging

    async def handler(request):
        return httpx.Response(404, text="No endpoint found")

    eng = TriggerEngine(rt)
    eng.settings = settings(reply={"personal": "all"})
    eng._http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    with caplog.at_level(logging.WARNING, logger="wa_mcp.trigger.engine"):
        d = await eng.consider(inbound())

    assert d.fired is False and d.error is True
    assert any("FAILED" in r.message for r in caplog.records), \
        "a dead backend produced no warning"
