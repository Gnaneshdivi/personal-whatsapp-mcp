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
    policy: str = ""                        # guardrails, rendered for the model
    reason: str = ""                        # only used by notification templates

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
            "policy": self.policy,
            "reason": self.reason,
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
    # The policy goes in the system message rather than the user turn: a rule
    # placed alongside the user's words is easier to argue with than one the
    # model reads as its own instruction.
    if ctx.policy:
        system = (system + "\n\n" + ctx.policy).strip()
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


# ---------------------------------------------------------------- images

_IMG = re.compile(
    r"""(?:!\[[^\]]*\]\(\s*(https?://[^\s)]+)\s*\))"""     # markdown image
    r"""|(https?://[^\s<>"']+\.(?:png|jpe?g|gif|webp)(?:\?[^\s<>"']*)?)""",
    re.IGNORECASE,
)


def extract_images(text: str) -> tuple[str, list[str]]:
    """Pull image URLs out of a reply, and return the text without them.

    Models that generate pictures hand back a markdown image or a bare URL. Sent
    verbatim that is a link the recipient has to tap; downloaded and attached it
    is a photo in the conversation, which is what anyone actually wanted.
    """
    urls: list[str] = []
    for m in _IMG.finditer(text or ""):
        url = m.group(1) or m.group(2)
        if url and url not in urls:
            urls.append(url)
    cleaned = _IMG.sub("", text or "")
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned).strip()
    return cleaned, urls


async def fetch_image(url: str, max_bytes: int,
                      client: httpx.AsyncClient | None = None) -> tuple[bytes, str]:
    """Download an image, refusing anything too large or not an image.

    The size and content-type checks are the point: this fetches a URL a model
    produced, so it must not be trusted to be small or to be a picture.
    """
    own = client is None
    client = client or httpx.AsyncClient(timeout=30.0, follow_redirects=True)
    try:
        r = await client.get(url)
        if r.status_code >= 400:
            raise BackendError(f"image fetch returned HTTP {r.status_code}")
        ctype = (r.headers.get("content-type") or "").split(";")[0].strip().lower()
        if not ctype.startswith("image/"):
            raise BackendError(f"{url} is {ctype or 'unknown'}, not an image")
        data = r.content
        if len(data) > max_bytes:
            raise BackendError(f"image is {len(data)} bytes, over the {max_bytes} limit")
        return data, ctype
    except BackendError:
        raise
    except Exception as exc:
        raise BackendError(f"image fetch failed: {exc}") from exc
    finally:
        if own:
            await client.aclose()
