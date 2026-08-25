# Settings reference

Two separate things are configured here.

**Environment variables** set up the server: where it listens, where data goes,
how it pairs. They are read at startup and change only on restart.

**Auto-reply settings** are edited at `/settings`, stored in your database, and
take effect on the next message. They can also be read and changed over MCP
with `wa_get_reply_settings` and `wa_set_reply_settings` — the latter merges,
so `{"enabled": true}` switches replies on and touches nothing else. Every one has an explanation on hover in the
UI; this page is the same information, written down.

---

# Environment

| Variable | Default | What it does |
|---|---|---|
| `WA_AUTH_TOKEN` | — | Not needed on loopback, where it runs open. Created in the database and shown at startup when reachable from elsewhere, and stable across restarts. `MCP_AUTH_TOKEN` is an alias. |
| `WA_ALLOW_OPEN` | `0` | Run with no authentication even when reachable. Only for a network you trust. |
| `PUBLIC_BASE_URL` | — | Tells the server it is reachable from elsewhere, so it protects itself and prints the right link. Set it to the tunnel's address. |
| `WA_HOST` | `127.0.0.1` | In Docker this must be `0.0.0.0` or nothing outside the container can reach it. |
| `WA_PORT` | `8100` | |
| `WA_DATABASE_URL` | *unset* | Unset → SQLite. See [setup](setup.md#storage). |
| `WA_DATA_DIR` | OS data dir | Where SQLite files, the session and cached media live. |
| `WA_SESSION_SSLMODE` | `disable` | Postgres path only. A managed database wants `require`. |
| `WA_HISTORY_DAYS` | `365` | **Pair-time only.** How much history WhatsApp sends when you link. |
| `WA_HISTORY_SIZE_MB` | `500` | **Pair-time only.** |
| `WA_DEVICE_OS` | `Chrome` | Shown in WhatsApp → Linked Devices. |
| `WA_DEVICE_PLATFORM` | `CHROME` | |
| `WA_STORE_RAW_PROTO` | `0` | Keeps each message's raw protobuf. Only needed to re-download media never fetched; ~1 KB per message. |
| `LOG_LEVEL` | `INFO` | |

The pair-time ones are worth repeating: they are read **once**, when you scan
the QR. Changing them afterwards does nothing until you unlink and pair again.

---

# Auto-reply

## Master

| Setting | Default | What it does |
|---|---|---|
| `enabled` | `false` | Nothing is ever sent while this is off. Watch rules still run. |
| `backend` | `model` | `model` or `webhook`. See [auto-reply modes](auto-reply.md). |

## Model

Used when `backend` is `model`. See [choosing a model](auto-reply.md#choosing-a-model).

| Setting | Default | What it does |
|---|---|---|
| `model.base_url` | — | Any OpenAI-compatible root, e.g. `https://openrouter.ai/api/v1`. A trailing `/chat/completions` is trimmed, so pasting the documented endpoint also works. |
| `model.api_key` | — | Stored in your own database. The UI shows `***` and posting that back keeps the existing key. |
| `model.model` | — | Exactly as your provider names it. |
| `model.system_prompt` | persona | **Persona and tone only.** How the reply is delivered is added automatically and differs by mode, so it is not yours to set here. |
| `model.history_messages` | `10` | Turns of conversation sent. More context costs more and, past a point, buys nothing. |
| `model.temperature` | `0.7` | 0 is repeatable and flat. |
| `model.max_tokens` | `300` | Hard ceiling. Reasoning models need far more — see [models](auto-reply.md#reasoning-models). |
| `model.timeout_seconds` | `30.0` | A late answer reads worse than none. |

## Webhook

Used when `backend` is `webhook`.

| Setting | Default | What it does |
|---|---|---|
| `webhook.url` | — | |
| `webhook.method` | `POST` | |
| `webhook.headers` | `{}` | One per line as `Name: value` in the UI. Tags work here too. |
| `webhook.body` | JSON with `{{prompt}}` | A JSON body is escaped for you, so a message containing a quote cannot break it. |
| `webhook.reply_path` | `reply` | Dotted path into your response — `reply`, `content.0.text`, `choices.0.message.content`. Blank if you return plain text. Ignored when not waiting. |
| `webhook.expect_reply` | `true` | **The mode switch.** See [auto-reply modes](auto-reply.md). |
| `webhook.token_ttl_seconds` | `300` | Lifetime of the scoped token in a hand-off payload. |
| `webhook.history_messages` | `10` | |
| `webhook.timeout_seconds` | `30.0` | |

## Who gets replies

Start narrow. `all` means every stranger who messages you gets an automated
answer on your personal number.

| Setting | Default | What it does |
|---|---|---|
| `reply.personal` | `none` | `none` / `all` / `allowlist` |
| `reply.personal_allowlist` | `[]` | Used when `personal` is `allowlist`. |
| `reply.groups` | `none` | Groups are noisy and a wrong reply is seen by everyone. |
| `reply.groups_allowlist` | `[]` | |
| `reply.require_mention_in_groups` | `true` | Strongly recommended. Off, it answers every message in the group. |
| `reply.cooldown_seconds` | `30` | Shortest gap between two replies in one chat. Stops a burst producing a burst, and is what breaks a loop when the other end is also a bot. |
| `reply.max_replies_per_hour` | `60` | Ceiling across **all** chats, rolling. The circuit breaker: it caps the damage before you notice. |
| `reply.max_reply_chars` | `1200` | Longer replies are truncated. |

## Guardrails

| Setting | Default | What it does |
|---|---|---|
| `guardrails.context_only` | `true` | Answer only from this conversation. Off, the model invents prices, dates and order numbers that sound entirely plausible. |
| `guardrails.allow_external_knowledge` | `false` | The deliberate escape hatch, stated to the model in words. |
| `guardrails.allowed_topics` | `[]` | Empty allows any subject. **A single topic here makes it refuse ordinary greetings.** |
| `guardrails.require_allowed_topic` | `false` | Strict: a message mentioning none of them is refused before the model runs. |
| `guardrails.blocked_topics` | `[]` | Passed to the model as instructions. |
| `guardrails.blocked_keywords` | `[]` | Checked in code **before** the model is called, so these cost nothing and cannot be talked around. |
| `guardrails.policy_note` | — | Added to the prompt verbatim. The right place for standing facts — your role, hours, what you can commit to. |
| `guardrails.fallback_message` | *"Sorry, I can't help…"* | Sent when a reply is refused or the model says it did not understand. |
| `guardrails.send_fallback_when_blocked` | `true` | Off, a blocked message gets silence. |
| `guardrails.send_fallback_on_error` | `false` | Off, an outage is invisible — usually better than apologising for something they did not see break. |

## Say it is a bot

| Setting | Default | What it does |
|---|---|---|
| `disclosure.enabled` | `true` | Sent once per conversation, before the first automated reply. |
| `disclosure.message` | *"Hi — I'm an AI assistant…"* | Its own message, not glued to the answer. Which chats have been told is stored, so a restart does not re-announce to everyone. |

Once per contact, permanently — not once per session.

## When it may reply

| Setting | Default | What it does |
|---|---|---|
| `hours.enabled` | `false` | |
| `hours.start` / `hours.end` | `09:00` / `21:00` | 24-hour. An end before the start runs overnight, so `22:00`–`06:00` works. |
| `hours.timezone` | `Asia/Kolkata` | IANA name. Explicit because the server may not be in the same country as the phone. |
| `hours.after_hours_message` | — | Optional, once per chat per day. Blank means silence until the window opens. |

Outside the window nothing is sent, but messages are still stored and watch
rules still fire. This gates replying, not listening.

A malformed time falls **open**, not shut — a typo must not silently stop every
reply.

## Summaries

| Setting | Default | What it does |
|---|---|---|
| `summary.enabled` | `false` | |
| `summary.every_minutes` | `60` | 10 for a busy line, 1440 for daily. Changing it takes effect now, not after the old interval. |
| `summary.route` | `me` | `off` / `me` / `number` |
| `summary.jid` | — | Used when `route` is `number`. |
| `summary.important` | `[]` | **The point of the digest.** Anything matching is named first and explicitly. |
| `summary.include_groups` | `false` | Groups are most of the volume and least of what needs you. |
| `summary.max_chats` | `20` | Ceiling, so a busy hour still produces something you will read. |

Nothing is sent when nothing happened. In groups, only messages that **mention
you** or **reply to something you said** are considered — the rest is people
talking to the room, and reporting it as a request is worse than silence.

## Alerts

| Setting | Default | What it does |
|---|---|---|
| `notify.route` | `off` | `off` / `me` / `chat` / `number`. **`chat` means the person who messaged you sees the alert** — pick it only if that is genuinely what you want. |
| `notify.jid` | — | Used when `route` is `number`. |
| `notify.on_keywords` | `[]` | Case-insensitive. Works with auto-reply off. |
| `notify.vip_contacts` | `[]` | These get through regardless of keywords. |
| `notify.watch_groups` | `false` | |
| `notify.on_handoff` | `true` | The model asked for a human, or said it did not understand. |
| `notify.on_blocked` | `false` | A guardrail refused. |
| `notify.on_error` | `false` | The backend failed. |
| `notify.handoff_marker` | `[[NOTIFY]]` | Stripped before anything is sent. |
| `notify.template` | see UI | `{{reason}}` is why it fired. Includes a `wa.me` link, which WhatsApp turns into a tap that opens the chat. |

The last four describe things that only happen during an auto-reply, so they
appear in the UI only when it is on.

## Media

| Setting | Default | What it does |
|---|---|---|
| `send_media` | `false` | When a reply links a picture, video, voice note or document, download it and send it as a real attachment. Anything unrecognised goes as a document; a URL returning HTML is refused. |
| `max_media_bytes` | `8388608` | The URL comes from a model, so it cannot be trusted to be small. |
| `show_typing` | `true` | |

## Log out

One control. It unlinks WhatsApp and removes everything stored here: messages,
chats, settings, and every credential this server issued — connectors, routine
tokens, pending hand-off tokens.

This cannot be undone. WhatsApp sends history once, at pair time, so pairing
again starts with an empty archive rather than this one.

`WA_AUTH_TOKEN` survives, because it comes from the environment and is
re-registered on every start; revoking it would lock you out until a restart
and do nothing after one. To change it, change the variable and restart.

The button confirms in the page — a second click within five seconds — rather
than in a browser dialog.

## Template tags

Usable in `system_prompt`, `webhook.body`, `webhook.headers` and
`notify.template`.

| Tag | Value |
|---|---|
| `{{message}}` | The message that arrived. |
| `{{prompt}}` | The fully rendered prompt. Webhook only. |
| `{{chat_name}}` | Contact or group name. |
| `{{chat_jid}}` | Chat address. Stable — use it as a session key. |
| `{{sender_name}}` / `{{sender_jid}}` | In a group, the individual rather than the group. |
| `{{me_name}}` | Your WhatsApp display name. |
| `{{message_id}}`, `{{timestamp}}` | |
| `{{history}}` | Recent turns, oldest first. |
| `{{policy}}` | Your guardrails as instructions. |
| `{{chat_link}}` | `wa.me` link. Empty for `@lid` senders, which carry no phone number. |
| `{{reply_token}}` | Scoped token for a hand-off webhook. |
| `{{reason}}` | Why an alert fired. Alerts only. |
