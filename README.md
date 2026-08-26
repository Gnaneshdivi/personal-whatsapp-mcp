# personal-whatsapp-mcp

[![CI](https://github.com/Gnaneshdivi/personal-whatsapp-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/Gnaneshdivi/personal-whatsapp-mcp/actions/workflows/ci.yml)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue)](https://www.python.org/downloads/)
[![License: Apache-2.0](https://img.shields.io/badge/license-Apache--2.0-green)](LICENSE)
[![MCP](https://img.shields.io/badge/MCP-23%20tools-purple)](https://modelcontextprotocol.io)

WhatsApp for any LLM. One phone number, one process: an MCP server with 23
tools, a web UI that looks like WhatsApp Web, and an auto-reply you configure
rather than code.

No Redis, no database server, no Docker required. SQLite is the default and
ships with Python.

> **This project is independent and is not affiliated with WhatsApp or Meta.**
> It links to your account the same way WhatsApp Web does, through
> [whatsmeow](https://github.com/tulir/whatsmeow). Use it at your own risk:
> WhatsApp's Terms of Service govern what you may do with your account, and
> automating replies to real people is your responsibility, not this
> project's.

---

## Quick start

```bash
git clone https://github.com/Gnaneshdivi/personal-whatsapp-mcp.git
cd personal-whatsapp-mcp
pip install -e .
python run.py
```

Open <http://127.0.0.1:8100>, scan the QR with **WhatsApp → Linked Devices**,
and wait for history to sync.

Then point your AI client at:

```
http://127.0.0.1:8100/mcp
```

That is the whole setup. On localhost there is no token and no sign-in — only
this machine can reach it.

> **Before you start:** you need **libmagic**, or the package will not import.
> `brew install libmagic` on macOS, `apt install libmagic1` on Debian/Ubuntu.
> The traceback names a Python package rather than the missing C library, which
> sends most people the wrong way.

### Exposing it beyond this machine

Set `PUBLIC_BASE_URL` and the server protects itself — it generates a token,
keeps it across restarts, and prints both URLs at startup:

```bash
PUBLIC_BASE_URL=https://you.ngrok.io python run.py
```

```
  Reachable from other machines, so access needs a token.

  Open this:      https://you.ngrok.io/?k=Tfk0n7Tx…
  Connect MCP to: https://you.ngrok.io/mcp?k=Tfk0n7Tx…
```

The token goes in the URL because a connector dialog takes a URL and nothing
else. Set `WA_AUTH_TOKEN` to choose your own, or `WA_ALLOW_OPEN=1` for none.

**Anyone holding that URL can read and send on your WhatsApp account.**

---

## What it is

Three things sharing one WhatsApp connection:

**An MCP server.** 23 tools — send, search, read threads, download media,
delivery receipts, group info. Point Claude or any MCP client at `/mcp`.

**A web UI.** Two panes, live over SSE, with delivery ticks, lazy-loaded
history, and search across both chats and message text.

**An auto-reply, in two modes.** Either an OpenAI-compatible model replies from
here, or your own webhook does — synchronously, or by handing the message over
to an agent that answers in its own time.

## What it is not

**There is no memory.** The assistant sees the last N turns of the conversation
it is answering and nothing else. It does not remember other chats, does not
build up knowledge of a contact, and does not learn.

**There is no knowledge base.** No documents, no retrieval. Standing facts go in
one prompt field and are pasted in on every call.

**It is not an agent** in the default mode: one message out, then it stops.

The message store exists for *you* — the UI, search, summaries, the MCP tools.
The model never reads from it beyond the current conversation. If you want
memory or tools, hand the message to your own agent; that is the second mode.

**The replies are the model's.** This server shapes the prompt; what comes back
is whatever the model produces. A weak model ignores instructions a strong one
follows — see [choosing a model](docs/auto-reply.md#choosing-a-model).

---

## Requirements

- Python 3.11+
- **libmagic** (see above)
- A phone number you can scan a QR with, on a phone that stays reachable.
  WhatsApp unlinks a companion device that has not seen the phone in about
  two weeks.

## Configuration

Everything is an environment variable. Copy `.env.example` to `.env` — it is
read from the working directory, and real environment variables always win, so
a stale file cannot override what your platform set.

| Variable | Default | Notes |
|---|---|---|
| `WA_AUTH_TOKEN` | generated when needed | Bearer token for the UI and MCP. |
| `PUBLIC_BASE_URL` | — | Tells the server it is reachable from elsewhere, so it protects itself and prints the right link. |
| `WA_ALLOW_OPEN` | — | `1` runs without a token even when reachable. |
| `WA_DATABASE_URL` | SQLite | `postgresql://…`, `mongodb://…`, or `sqlite:////abs/path.db` |
| `WA_DATA_DIR` | OS data dir | Where the session and SQLite files live. |
| `WA_HISTORY_DAYS` | 365 | **Pair-time only.** Cannot change without unlinking. |

The full list is in `.env.example`, checked against `config.py` by a test so it
cannot drift. Every auto-reply setting is at `/settings`, each with an
explanation on hover.

### Storage

One variable decides everything:

- **unset** → SQLite in the data directory. The right answer for one number.
- **`postgresql://`** → Postgres, and the WhatsApp session rides along in it, so
  the container is stateless.
- **`mongodb://`** → Mongo for messages. The session stays a local file:
  whatsmeow's store is SQL, and Mongo cannot hold it.

---

## Auto-reply

Configure it at `/settings`. The parts worth knowing:

**Two backends.** A model (any OpenAI-compatible endpoint: OpenRouter, OpenAI,
Groq, Ollama) or a webhook. Both are sent the *same* prompt; only the transport
differs.

**Webhooks have two modes.** Wait for the reply and this server sends what comes
back. Or hand it over: the payload carries a token scoped to that one delivery,
and your endpoint sends the reply itself.

**Guardrails.** Answer only from the conversation, topic allow/deny lists,
blocked keywords checked before the model runs, and a fallback message.

**It says it is a bot** — once per conversation, before the first reply.

**Active hours**, in an explicit timezone, because the server may not be in the
same country as the phone.

**Summaries.** A digest on an interval, led by what is waiting on you. In
groups, only messages that mention you or reply to you are considered — the
rest is people talking to the room.

Step-by-step for both backends: **[docs/recipes.md](docs/recipes.md)**.

---

## Security

Full policy: [SECURITY.md](SECURITY.md). The number is a real person's, and a
few things follow from that:

**Untrusted input is tagged.** Every inbound message is wrapped in
`<msg id="…">` with a per-request nonce, and the model is told anything inside
is data. History is wrapped too — an attacker can seed an instruction and wait a
turn for it to replay as context.

**Hand-off tokens are scoped, not trusted.** An agent processing a stranger's
message could otherwise reach every conversation on the account. A routine's
token can call three tools, only with a token proving a real message arrived,
and that token names the chat. Reading other conversations is not a refusal it
has to be talked into — it is not available.

**Sending to the wrong person is unrecoverable**, so an ambiguous name is
refused with the candidates listed rather than resolved to a guess.

**Rate limits are a circuit breaker**: a per-chat cooldown and an hourly cap
across all chats, so a fault costs you a few messages rather than your number.

WhatsApp marks messages sent through any unofficial client with an `AI` label.
That is Meta's, applied to the client, and nothing here can or should remove it.

---

## Documentation

| | |
|---|---|
| [Setup](docs/setup.md) | Install, pairing, storage, tunnels, Docker |
| [Recipes](docs/recipes.md) | Step by step: an OpenAI-compatible model, and a Claude Routine |
| [Auto-reply](docs/auto-reply.md) | The two modes, the prompt, choosing a model, the security model |
| [Settings](docs/settings.md) | Every environment variable and all 64 auto-reply settings |
| [Architecture](docs/architecture.md) | Where the code lives — start here to contribute |

## Development

```bash
pip install -e ".[dev]"
pytest -q
```

397 tests. Postgres and Mongo suites skip unless `WA_TEST_POSTGRES` /
`WA_TEST_MONGO` point at a server.

See [CONTRIBUTING.md](CONTRIBUTING.md) for what the tests are for and which
behaviour is deliberately not configurable.

## Limits

- **One number, one process.** By design.
- **History arrives once, at pair time.** whatsmeow can request more, but
  neonize does not export the call, so it is not reachable from Python.
- Group participant names come from message metadata, so a silent member of a
  group may show as a number.

## Built on

This project is a thin layer over other people's hard work, and would not exist
without it:

- **[whatsmeow](https://github.com/tulir/whatsmeow)** (MPL-2.0) — the Go library
  that speaks WhatsApp's multidevice protocol. Everything here that touches
  WhatsApp ultimately goes through it.
- **[neonize](https://github.com/krypton-byte/neonize)** (Apache-2.0) — the
  Python bindings that make whatsmeow reachable from Python, via a CGO shared
  library.
- **[FastMCP](https://github.com/jlowin/fastmcp)** — the MCP server framework.

All three are used as published dependencies. No code from any of them is
vendored or modified here, so their licences apply to them rather than to this
project.

## Licence

Apache-2.0. See [LICENSE](LICENSE).
