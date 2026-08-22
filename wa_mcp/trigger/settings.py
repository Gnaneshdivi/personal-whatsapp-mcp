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
    system_prompt: str = (
        "You are replying on WhatsApp as {{me_name}}.\n"
        "Keep replies short and natural — one or two sentences.\n"
        "You are talking to {{chat_name}}."
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
    prompt_template: str = (
        "Conversation with {{chat_name}}:\n{{history}}\n"
        "Latest message: {{message}}"
    )
    history_messages: int = 10
    timeout_seconds: float = 30.0

    @property
    def configured(self) -> bool:
        return bool(self.url)


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


def _build(cls, raw):
    if not isinstance(raw, dict):
        return cls()
    allowed = {f for f in cls.__dataclass_fields__}
    return cls(**{k: v for k, v in raw.items() if k in allowed})


def _secretish(key: str) -> bool:
    k = key.lower()
    return any(s in k for s in ("auth", "key", "token", "secret", "password"))
