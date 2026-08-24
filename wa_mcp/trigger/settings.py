"""Auto-reply settings, and why every default is off.

This is the feature that gets phone numbers banned, and it is about to be handed
to strangers to run on their personal accounts. The defaults shipped here are
the ones almost everyone will keep, so a fresh install observes and stores and
says nothing until someone deliberately turns it on, per scope.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal

Scope = Literal["none", "all", "allowlist"]

SETTINGS_KEY = "trigger.settings"


@dataclass
class ModelBackend:
    """Any endpoint that speaks OpenAI's /chat/completions.

    One integration covers OpenRouter, OpenAI, Groq, Together, DeepInfra,
    Fireworks, vLLM, Ollama, LM Studio and LiteLLM.
    """

    base_url: str = ""
    api_key: str = ""
    model: str = ""
    # Persona and tone only. How the reply gets delivered is NOT here: it
    # differs by backend mode, and a user editing this must not be able to
    # leave the two contradicting each other.
    system_prompt: str = (
        "You are replying on WhatsApp as {{me_name}}, talking to {{chat_name}}.\n"
        "Keep replies short and natural — one or two sentences."
    )
    history_messages: int = 10
    temperature: float = 0.7
    max_tokens: int = 300
    timeout_seconds: float = 30.0

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.model)


@dataclass
class WebhookBackend:
    """For anyone whose reply logic is a workflow rather than a model."""

    url: str = ""
    method: str = "POST"
    headers: dict[str, str] = field(default_factory=dict)
    body: str = '{"text": "{{prompt}}", "session": "{{chat_jid}}"}'
    reply_path: str = "reply"
    # False = hand the message over and stop. Your endpoint decides whether to
    # answer and sends it itself through the API, which is the shape anything
    # queued, human-approved or slower than a request can be needs. True = this
    # server waits for the response and sends whatever comes back.
    expect_reply: bool = True
    # How long the scoped token in the payload stays usable. Short, because it
    # is handed to an agent reasoning about a message a stranger wrote; long
    # enough that a queue or a human approving the reply still fits.
    token_ttl_seconds: int = 300
    history_messages: int = 10
    timeout_seconds: float = 30.0

    @property
    def configured(self) -> bool:
        return bool(self.url)


@dataclass
class Guardrails:
    """What the bot is allowed to talk about.

    Three layers, because they fail differently. Keywords are deterministic and
    run before any model is called, so a hard block costs nothing and cannot be
    talked around. Topics are injected into the system prompt, which steers well
    but is advice rather than enforcement. `require_allowed_topic` adds a cheap
    keyword pre-filter for the "only answer about X" case.

    A model can be argued out of a prompt instruction. It cannot be argued out
    of a keyword check that runs before it is invoked — so put anything that
    genuinely must not happen in `blocked_keywords`.
    """

    # Only reply when the message looks related to one of these. Empty = any.
    allowed_topics: list[str] = field(default_factory=list)
    require_allowed_topic: bool = False
    # Never reply about these; also injected as an instruction.
    blocked_topics: list[str] = field(default_factory=list)
    # Hard, pre-model, case-insensitive substring match.
    blocked_keywords: list[str] = field(default_factory=list)
    # Free text appended to the system prompt — tone, persona, house rules.
    policy_note: str = ""

    # Grounding. On by default: an assistant answering from a WhatsApp thread
    # should work from what is actually in that thread. A model left to its own
    # knowledge will confidently invent an order number, a price or an address,
    # and on a business line that is worse than silence.
    context_only: bool = True
    # The deliberate escape hatch. Turning this on is stated to the model in
    # plain words rather than implied by the absence of a restriction, so the
    # behaviour is explicit on both sides of the boundary.
    allow_external_knowledge: bool = False

    fallback_message: str = (
        "Sorry, I can't help with that here. Someone will get back to you."
    )
    send_fallback_when_blocked: bool = True
    send_fallback_on_error: bool = False

    def as_prompt(self) -> str:
        """The policy, rendered for the model. Empty when nothing is set."""
        parts = []
        if self.allow_external_knowledge:
            parts.append(
                "You may use your general knowledge, and any search or tools "
                "available to you, to answer beyond this conversation."
            )
        elif self.context_only:
            parts.append(
                "Answer only from this conversation and the information in it. "
                "Do not use outside knowledge, do not search, and do not invent "
                "details such as prices, dates, addresses or order numbers. "
                "If the answer is not in this conversation, say that you do not "
                "have it here."
            )
        if self.allowed_topics:
            parts.append(
                "Only help with these topics: "
                + ", ".join(self.allowed_topics)
                + ". If the message is about anything else, reply exactly: "
                + self.fallback_message
            )
        if self.blocked_topics:
            parts.append(
                "Never discuss: " + ", ".join(self.blocked_topics)
                + ". If asked, reply exactly: " + self.fallback_message
            )
        if self.policy_note.strip():
            parts.append(self.policy_note.strip())
        return "\n".join(parts)

    def blocked_reason(self, text: str) -> str | None:
        """Deterministic pre-model check. None means allowed."""
        low = (text or "").lower()
        for word in self.blocked_keywords:
            w = word.strip().lower()
            if w and w in low:
                return f"blocked keyword {word.strip()!r}"
        if self.require_allowed_topic and self.allowed_topics:
            if not any(t.strip().lower() in low for t in self.allowed_topics if t.strip()):
                return "message does not mention an allowed topic"
        return None


@dataclass
class Notify:
    """Where a human gets told something needs them.

    The business case: the number answering customers is not the number the
    owner reads. `jid` empty means notify in the same conversation, which is the
    sensible default for a personal number.
    """

    # Where alerts go. A single free-text box could not express this: blank
    # once meant "the chat it came from", which delivered internal wording to
    # the customer, and blank then meant "off", which silently stopped alerts
    # someone had deliberately configured. Neither is guessable from an empty
    # field, so the destination is now stated.
    route: str = "off"                # off | me | chat | number
    jid: str = ""                     # only read when route == "number"
    on_handoff: bool = True           # the model asked for a human
    on_blocked: bool = False          # a guardrail refused
    on_error: bool = False            # the backend failed

    # "Tell me when…" — these fire independently of replying, so they work with
    # auto-reply switched off entirely. Watching a number without answering on
    # it is a legitimate and common way to use this.
    on_keywords: list[str] = field(default_factory=list)
    vip_contacts: list[str] = field(default_factory=list)   # always tell me
    watch_groups: bool = False        # keyword watching inside groups too

    def watch_reason(self, text: str, sender: str, chat: str,
                     is_group: bool) -> str | None:
        """Why this message deserves a human's attention. None = it does not."""
        from ..whatsapp import jid as J

        if is_group and not self.watch_groups:
            return None

        vips = {J.normalise(v) for v in self.vip_contacts or []}
        if J.normalise(chat) in vips or J.normalise(sender) in vips:
            return "message from a watched contact"

        low = (text or "").lower()
        for word in self.on_keywords or []:
            w = word.strip().lower()
            if w and w in low:
                return f"matched watched word {word.strip()!r}"
        return None

    @property
    def watching(self) -> bool:
        return bool(self.on_keywords or self.vip_contacts)
    # A reply containing this marker is treated as "get a human", and the marker
    # is stripped before anything is sent to the contact.
    handoff_marker: str = "[[NOTIFY]]"
    # The link is the point of the last line: WhatsApp turns a wa.me URL into
    # a tap that opens the conversation, where the bare address it used to
    # print was something you had to copy out by hand.
    template: str = (
        "Needs you: {{chat_name}} ({{sender_jid}})\n"
        "Their message: {{message}}\n"
        "Reason: {{reason}}\n"
        # On its own line and unlabelled, so a sender with no phone number
        # (a LID) leaves a blank the send strips, rather than a stranded
        # "Open:" with nothing after it. WhatsApp links a bare URL anyway.
        "{{chat_link}}"
    )


@dataclass
class Disclosure:
    """Telling someone they are talking to a bot, once per conversation.

    Sent as its own message ahead of the first automated reply in a chat, not
    prepended to it: a greeting welded onto an answer reads as boilerplate and
    gets skimmed, and the disclosure is the part that must not be.

    Once per chat, because repeating it every message is what makes people stop
    reading it. Whether they were told is remembered in the store, so it
    survives a restart rather than starting over each time the process does.
    """

    enabled: bool = True
    message: str = (
        "Hi — I'm an AI assistant answering on behalf of {{me_name}}. "
        "I can help with most things right here. If it needs {{me_name}} "
        "personally, I'll flag it so they can get back to you."
    )


@dataclass
class ActiveHours:
    """When replying is allowed at all.

    An assistant answering at 3am on a personal number is not helpful, it is
    conspicuous. Outside the window nothing is sent, and the message is still
    recorded and still watched — this gates replying, not receiving.
    """

    enabled: bool = False
    start: str = "09:00"
    end: str = "21:00"
    # IANA name. Kept explicit rather than read from the host, because the
    # server may well not be in the same country as the phone.
    timezone: str = "Asia/Kolkata"
    # Optional single line sent instead of a real reply, once per chat per day,
    # so someone writing at midnight is not met with silence.
    after_hours_message: str = ""

    def window(self) -> tuple[int, int]:
        """start, end as minutes past midnight."""
        def mins(v: str, fallback: int) -> int:
            try:
                h, m = (v or "").split(":")
                return int(h) % 24 * 60 + int(m) % 60
            except Exception:
                return fallback
        return mins(self.start, 0), mins(self.end, 24 * 60 - 1)

    def open_at(self, when) -> bool:
        """Whether replying is allowed at `when` (a datetime in `timezone`)."""
        if not self.enabled:
            return True
        start, end = self.window()
        now = when.hour * 60 + when.minute
        if start == end:
            return True
        if start < end:
            return start <= now < end
        return now >= start or now < end        # a window over midnight


@dataclass
class Summary:
    """Periodic digests, so nothing is missed without reading every chat.

    The interval is what makes this useful rather than noise: every ten minutes
    for a busy line, once a day for a quiet one. Nothing is sent when nothing
    happened, because a digest that arrives saying "no activity" trains you to
    ignore the ones that do not.
    """

    enabled: bool = False
    every_minutes: int = 60
    # Same vocabulary as alerts: off | me | chat | number.
    route: str = "me"
    jid: str = ""
    # Things that must be called out if they appear. These are what the digest
    # is FOR — the point is not to read everything, it is to not miss these.
    important: list[str] = field(default_factory=list)
    include_groups: bool = False
    max_chats: int = 20

    @property
    def configured(self) -> bool:
        return self.enabled and self.every_minutes > 0 and self.route != "off"


@dataclass
class ReplyScope:
    """Who gets replied to. Everything starts at none."""

    personal: Scope = "none"
    personal_allowlist: list[str] = field(default_factory=list)
    groups: Scope = "none"
    groups_allowlist: list[str] = field(default_factory=list)
    # Groups are both the ban risk and the annoyance risk, so even when they are
    # enabled the bot stays quiet unless spoken to.
    require_mention_in_groups: bool = True
    # Someone firing off five rapid messages should draw one reply, not five.
    cooldown_seconds: int = 30
    max_replies_per_hour: int = 60
    max_reply_chars: int = 1200


@dataclass
class TriggerSettings:
    enabled: bool = False
    backend: Literal["model", "webhook"] = "model"
    model: ModelBackend = field(default_factory=ModelBackend)
    webhook: WebhookBackend = field(default_factory=WebhookBackend)
    reply: ReplyScope = field(default_factory=ReplyScope)
    guardrails: Guardrails = field(default_factory=Guardrails)
    notify: Notify = field(default_factory=Notify)
    disclosure: Disclosure = field(default_factory=Disclosure)
    hours: ActiveHours = field(default_factory=ActiveHours)
    summary: Summary = field(default_factory=Summary)
    # A reply carrying an image URL is downloaded and sent as a photo rather
    # than as a link. Off by default: it fetches whatever URL a model emits.
    send_media: bool = False
    max_media_bytes: int = 8 * 1024 * 1024
    # Typing indicators cost nothing and are most of what makes an automated
    # reply read as a person rather than a bot posting instantly.
    show_typing: bool = True

    # ------------------------------------------------------------- storage

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None) -> "TriggerSettings":
        """Tolerant of missing and unknown keys.

        Settings are written by a UI that will grow fields over time and read by
        code that may be older or newer. Dropping unknown keys and defaulting
        absent ones means an upgrade never fails to start.
        """
        raw = raw or {}
        return cls(
            enabled=bool(raw.get("enabled", False)),
            backend=raw.get("backend", "model") if raw.get("backend") in ("model", "webhook") else "model",
            model=_build(ModelBackend, raw.get("model")),
            webhook=_build(WebhookBackend, raw.get("webhook")),
            reply=_build(ReplyScope, raw.get("reply")),
            guardrails=_build(Guardrails, raw.get("guardrails")),
            notify=_notify_from(raw.get("notify")),
            disclosure=_build(Disclosure, raw.get("disclosure")),
            hours=_build(ActiveHours, raw.get("hours")),
            summary=_build(Summary, raw.get("summary")),
            # send_images was the name before this covered video,
            # audio and documents; still read so an existing saved
            # config keeps working across the upgrade.
            send_media=bool(raw.get("send_media", raw.get("send_images", False))),
            max_media_bytes=int(raw.get("max_media_bytes",
                                        raw.get("max_image_bytes", 8 * 1024 * 1024))),
            show_typing=bool(raw.get("show_typing", True)),
        )

    def redacted(self) -> dict[str, Any]:
        """For anything a model or a browser can see. The key never leaves."""
        out = self.to_dict()
        if out["model"].get("api_key"):
            out["model"]["api_key"] = "***"
        out["webhook"]["headers"] = {
            k: ("***" if _secretish(k) else v)
            for k, v in (out["webhook"].get("headers") or {}).items()
        }
        return out

    def ready(self) -> tuple[bool, str]:
        """Whether replying could work at all, and what is missing if not."""
        if not self.enabled:
            return False, "auto-reply is switched off"
        if self.backend == "model":
            if not self.model.base_url:
                return False, "no model base_url configured"
            if not self.model.model:
                return False, "no model name configured"
        elif not self.webhook.url:
            return False, "no webhook url configured"
        if self.reply.personal == "none" and self.reply.groups == "none":
            return False, "no chats are in scope — enable personal or group replies"
        return True, ""


def _notify_from(raw) -> "Notify":
    """Notify, with `route` inferred for configs saved before it existed."""
    n = _build(Notify, raw)
    if isinstance(raw, dict) and not raw.get("route"):
        n.route = "number" if n.jid else "off"
    if n.route not in ("off", "me", "chat", "number"):
        n.route = "off"
    return n


def _build(cls, raw):
    if not isinstance(raw, dict):
        return cls()
    allowed = {f for f in cls.__dataclass_fields__}
    return cls(**{k: v for k, v in raw.items() if k in allowed})


def _secretish(key: str) -> bool:
    k = key.lower()
    return any(s in k for s in ("auth", "key", "token", "secret", "password"))
