"""The two ways a reply gets produced: a model, or a webhook.

Both return plain text or None. Everything about *whether* to reply lives in the
engine; these only answer "given this conversation, what would you say".
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any

import httpx

from .settings import ModelBackend, WebhookBackend

log = logging.getLogger(__name__)

_TOKEN = re.compile(r"\{\{\s*([a-z_]+)\s*\}\}")


@dataclass
class Context:
    """Everything a template or a prompt can draw on."""

    message: str
    chat_name: str
    chat_jid: str
    sender_name: str
    sender_jid: str
    me_name: str
    message_id: str
    timestamp: str
    history: list[tuple[bool, str, str]]   # (from_me, speaker, text), oldest first

    def history_text(self) -> str:
        return "\n".join(f"{speaker}: {text}" for _fm, speaker, text in self.history)

    def tokens(self) -> dict[str, str]:
        return {
            "message": self.message,
            "chat_name": self.chat_name,
            "chat_jid": self.chat_jid,
            "sender_name": self.sender_name,
            "sender_jid": self.sender_jid,
            "me_name": self.me_name,
            "message_id": self.message_id,
            "timestamp": self.timestamp,
            "history": self.history_text(),
        }


def render(template: str, ctx: Context, extra: dict[str, str] | None = None,
           json_safe: bool = False) -> str:
    """Substitute {{tokens}}.

    `json_safe` escapes each substitution for embedding inside a JSON string
    literal. Without it the first message containing a quote produces an invalid
    body — and that happens on day one, not eventually.
    """
    values = {**ctx.tokens(), **(extra or {})}

    def sub(m: re.Match) -> str:
        v = values.get(m.group(1), "")
        return json.dumps(v)[1:-1] if json_safe else v

    return _TOKEN.sub(sub, template or "")


def dig(payload: Any, path: str) -> str | None:
    """Pull a value out of a response by dotted path, e.g. choices.0.message.content."""
    if not path:
        return payload if isinstance(payload, str) else None
    cur = payload
    for part in path.split("."):
        if isinstance(cur, list):
            try:
                cur = cur[int(part)]
            except (ValueError, IndexError):
                return None
        elif isinstance(cur, dict):
            if part not in cur:
                return None
            cur = cur[part]
        else:
            return None
    return cur if isinstance(cur, str) else (json.dumps(cur) if cur is not None else None)


class BackendError(Exception):
    pass


async def reply_via_model(cfg: ModelBackend, ctx: Context,
                          client: httpx.AsyncClient | None = None) -> str:
    """Call an OpenAI-compatible endpoint.

    History is mapped to real conversation turns rather than pasted into one
    string. The direction of every stored message is already known, so the model
    sees a dialogue it is continuing — which is what it was trained on — instead
    of a transcript it has to parse out of a prompt.
    """
    if not cfg.configured:
        raise BackendError("model backend is not configured")

    messages: list[dict[str, str]] = []
    system = render(cfg.system_prompt, ctx)
    if system.strip():
        messages.append({"role": "system", "content": system})
    for from_me, _speaker, text in ctx.history[-cfg.history_messages:]:
        if text:
            messages.append({"role": "assistant" if from_me else "user", "content": text})
    if not messages or messages[-1]["role"] != "user" or messages[-1]["content"] != ctx.message:
        messages.append({"role": "user", "content": ctx.message})

    headers = {"Content-Type": "application/json"}
    if cfg.api_key:
        headers["Authorization"] = f"Bearer {cfg.api_key}"

    url = cfg.base_url.rstrip("/") + "/chat/completions"
    body = {
        "model": cfg.model,
        "messages": messages,
        "temperature": cfg.temperature,
        "max_tokens": cfg.max_tokens,
    }

    own = client is None
    client = client or httpx.AsyncClient(timeout=cfg.timeout_seconds)
    try:
        r = await client.post(url, json=body, headers=headers)
        if r.status_code >= 400:
            raise BackendError(f"model returned HTTP {r.status_code}: {r.text[:300]}")
        data = r.json()
    except BackendError:
        raise
    except Exception as exc:
        raise BackendError(f"{type(exc).__name__}: {exc}") from exc
    finally:
        if own:
            await client.aclose()

    text = dig(data, "choices.0.message.content")
    if not text:
        raise BackendError(f"no reply in response: {json.dumps(data)[:300]}")
    return text.strip()


async def reply_via_webhook(cfg: WebhookBackend, ctx: Context,
                            client: httpx.AsyncClient | None = None) -> str:
    """POST the configured body and pull the reply out of the response."""
    if not cfg.configured:
        raise BackendError("webhook backend is not configured")

    prompt = render(cfg.prompt_template, ctx)
    looks_json = cfg.body.strip().startswith(("{", "["))
    rendered = render(cfg.body, ctx, {"prompt": prompt}, json_safe=looks_json)

    headers = {k: render(v, ctx, {"prompt": prompt}) for k, v in (cfg.headers or {}).items()}
    payload: Any = rendered
    if looks_json:
        headers.setdefault("Content-Type", "application/json")
        try:
            payload = json.loads(rendered)
        except json.JSONDecodeError as exc:
            raise BackendError(f"body did not render to valid JSON: {exc}") from exc

    own = client is None
    client = client or httpx.AsyncClient(timeout=cfg.timeout_seconds)
    try:
        r = await client.request(
            cfg.method.upper() or "POST", cfg.url,
            json=payload if looks_json else None,
            content=None if looks_json else rendered,
            headers=headers,
        )
        if r.status_code >= 400:
            raise BackendError(f"webhook returned HTTP {r.status_code}: {r.text[:300]}")
        try:
            data = r.json()
        except Exception:
            data = r.text
    except BackendError:
        raise
    except Exception as exc:
        raise BackendError(f"{type(exc).__name__}: {exc}") from exc
    finally:
        if own:
            await client.aclose()

    text = dig(data, cfg.reply_path) if not isinstance(data, str) else data
    if not text or not text.strip():
        raise BackendError(
            f"no reply at {cfg.reply_path!r} in the response: {json.dumps(data)[:200]}"
            if not isinstance(data, str) else "webhook returned an empty body"
        )
    return text.strip()
