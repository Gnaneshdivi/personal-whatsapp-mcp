from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal

Scope = Literal["none", "all", "allowlist"]

SETTINGS_KEY = "trigger.settings"


@dataclass
class ModelBackend:

    base_url: str = ""
    api_key: str = ""
    model: str = ""
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

    url: str = ""
    method: str = "POST"
    headers: dict[str, str] = field(default_factory=dict)
    body: str = '{"text": "{{prompt}}", "session": "{{chat_jid}}"}'
    reply_path: str = "reply"
    expect_reply: bool = True
    token_ttl_seconds: int = 300
    history_messages: int = 10
    timeout_seconds: float = 30.0

    @property
    def configured(self) -> bool:
        return bool(self.url)


@dataclass
class Guardrails:

    allowed_topics: list[str] = field(default_factory=list)
    require_allowed_topic: bool = False
    blocked_topics: list[str] = field(default_factory=list)
    blocked_keywords: list[str] = field(default_factory=list)
    policy_note: str = ""

    context_only: bool = True
    allow_external_knowledge: bool = False

    fallback_message: str = (
        "Sorry, I can't help with that here. Someone will get back to you."
    )
    send_fallback_when_blocked: bool = True
    send_fallback_on_error: bool = False

    def as_prompt(self) -> str:
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

    route: str = "off"
    jid: str = ""
    on_handoff: bool = True
    on_blocked: bool = False
    on_error: bool = False

    on_keywords: list[str] = field(default_factory=list)
    vip_contacts: list[str] = field(default_factory=list)
    watch_groups: bool = False

    def watch_reason(self, text: str, sender: str, chat: str,
                     is_group: bool) -> str | None:
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
    handoff_marker: str = "[[NOTIFY]]"
    template: str = (
        "Needs you: {{chat_name}} ({{sender_jid}})\n"
        "Their message: {{message}}\n"
        "Reason: {{reason}}\n"
        "{{chat_link}}"
    )


@dataclass
class Disclosure:

    enabled: bool = True
    message: str = (
        "Hi — I'm an AI assistant answering on behalf of {{me_name}}. "
        "I can help with most things right here. If it needs {{me_name}} "
        "personally, I'll flag it so they can get back to you."
    )


@dataclass
class ActiveHours:

    enabled: bool = False
    start: str = "09:00"
    end: str = "21:00"
    timezone: str = "Asia/Kolkata"
    after_hours_message: str = ""

    def window(self) -> tuple[int, int]:
        def mins(v: str, fallback: int) -> int:
            try:
                h, m = (v or "").split(":")
                return int(h) % 24 * 60 + int(m) % 60
            except Exception:
                return fallback
        return mins(self.start, 0), mins(self.end, 24 * 60 - 1)

    def open_at(self, when) -> bool:
        if not self.enabled:
            return True
        start, end = self.window()
        now = when.hour * 60 + when.minute
        if start == end:
            return True
        if start < end:
            return start <= now < end
        return now >= start or now < end


@dataclass
class Summary:

    enabled: bool = False
    every_minutes: int = 60
    route: str = "me"
    jid: str = ""
    important: list[str] = field(default_factory=list)
    include_groups: bool = False
    max_chats: int = 20

    @property
    def configured(self) -> bool:
        return self.enabled and self.every_minutes > 0 and self.route != "off"


@dataclass
class ReplyScope:

    personal: Scope = "none"
    personal_allowlist: list[str] = field(default_factory=list)
    groups: Scope = "none"
    groups_allowlist: list[str] = field(default_factory=list)
    require_mention_in_groups: bool = True
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
    send_media: bool = False
    max_media_bytes: int = 8 * 1024 * 1024
    show_typing: bool = True


    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def merged_with(self, raw: dict[str, Any] | None) -> "TriggerSettings":
        base = self.to_dict()

        def overlay(dst: dict, src: dict) -> dict:
            for k, v in (src or {}).items():
                if isinstance(v, dict) and isinstance(dst.get(k), dict):
                    dst[k] = overlay(dict(dst[k]), v)
                else:
                    dst[k] = v
            return dst

        return TriggerSettings.from_dict(overlay(base, raw or {}))

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None) -> "TriggerSettings":
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
            send_media=bool(raw.get("send_media", raw.get("send_images", False))),
            max_media_bytes=int(raw.get("max_media_bytes",
                                        raw.get("max_image_bytes", 8 * 1024 * 1024))),
            show_typing=bool(raw.get("show_typing", True)),
        )

    def redacted(self) -> dict[str, Any]:
        out = self.to_dict()
        if out["model"].get("api_key"):
            out["model"]["api_key"] = "***"
        out["webhook"]["headers"] = {
            k: ("***" if _secretish(k) else v)
            for k, v in (out["webhook"].get("headers") or {}).items()
        }
        return out

    def ready(self) -> tuple[bool, str]:
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
