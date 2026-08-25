# suprai-whatsapp-mcp

WhatsApp for any LLM. One phone number, one process: an MCP server with 22
tools, a web UI that looks like WhatsApp Web, and an auto-reply you configure
rather than code.

No Redis, no database server, no Docker required. SQLite is the default and
ships with Python.

```bash
pip install suprai-whatsapp-mcp
python -m wa_mcp --token=generate     # prints a token and starts the server
```

Open the URL it prints, scan the QR with WhatsApp → Linked Devices, and wait
for the history sync to finish.

---

## What it is

Three things sharing one WhatsApp connection:

**An MCP server.** 22 tools — send, search, read threads, download media,
delivery receipts, group info. Point Claude or any MCP client at `/mcp`.

**A web UI.** Two panes, live over SSE, with delivery ticks, lazy-loaded
history and search across both chats and message text.

**An auto-reply.** Either an OpenAI-compatible model or your own webhook,
with guardrails, active hours, an AI disclosure, and periodic summaries of
anything waiting on you.

## Requirements

- Python 3.11+
- **libmagic.** neonize imports python-magic at module load; without it the
  package will not import, and the error reads like a missing Python
  dependency rather than a missing system library.
  `brew install libmagic` / `apt install libmagic1`
- A phone number you can scan a QR with, on a phone that stays reachable.

## Configuration

Everything is environment variables. Copy `.env.example` to `.env` — it is read
from the working directory, and real environment variables always win, so a
stale file cannot override what your platform set.

| Variable | Default | Notes |
|---|---|---|
| `WA_AUTH_TOKEN` | — | Bearer token for the UI and MCP. Anyone holding it can read and send on your account. |
| `PUBLIC_BASE_URL` | — | The address clients reach you on. Required for OAuth. |
| `WA_DATABASE_URL` | SQLite | `postgresql://…`, `mongodb://…`, or `sqlite:////abs/path.db` |
| `WA_DATA_DIR` | OS data dir | Where the session and SQLite files live. |
| `WA_HISTORY_DAYS` | 365 | **Pair-time only.** Cannot change without unlinking. |

The full list is in `.env.example`, and it is checked against `config.py` by a
test, so it cannot drift.

### Storage

One variable decides everything:

- **unset** → SQLite in the data directory. Right answer for one number.
- **`postgresql://`** → Postgres, and the WhatsApp session rides along in it,
  so the container is stateless.
- **`mongodb://`** → Mongo for messages. The session stays a local file:
  whatsmeow's store is SQL, and Mongo cannot hold it.

## Running it

```bash
python -m wa_mcp --port 8100
```

Behind a tunnel, set `PUBLIC_BASE_URL` to the public address — OAuth builds its
redirect and metadata from it.

```bash
docker build -t suprai-wa .
docker run -p 8100:8100 --env-file .env -v wa-data:/data suprai-wa
```

**Mount the volume.** The session lives there, and history syncs exactly once,
at pair time — losing it means re-pairing and losing every message you had.

## Connecting an MCP client

```
https://your-host/mcp?k=<WA_AUTH_TOKEN>
```

Or leave the token off and let it use OAuth: the client opens a browser, you
scan the QR, and pairing and authorising become one step.

## Auto-reply

Configure it at `/settings` — 64 settings, each with an explanation on hover.
The parts worth knowing:

**Two backends.** A model (any OpenAI-compatible endpoint: OpenRouter, OpenAI,
Groq, Ollama) or a webhook. Both are sent the *same* prompt; only the transport
differs.

**Webhooks have two modes.** Wait for the reply and this server sends what
comes back. Or hand it over: the payload carries a token scoped to that one
delivery, and your endpoint sends the reply itself.

**Guardrails.** Answer only from the conversation, topic allow/deny lists,
blocked keywords checked before the model runs, and a fallback message.

**It says it is a bot** — once per conversation, before the first reply.

**Active hours**, in an explicit timezone, because the server may not be in the
same country as the phone.

**Summaries.** A digest on an interval, led by what is waiting on you, with a
list of terms that must be called out. In groups, only messages that mention
you or reply to you are considered — the rest is people talking to the room.

## Security

The number is a real person's. A few things follow from that:

**Untrusted input is tagged.** Every inbound message is wrapped in
`<msg id="…">` with a per-request nonce, and the model is told that anything
inside is data. History is wrapped too: an attacker can seed an instruction and
wait a turn for it to replay as context.

**Hand-off tokens are scoped, not trusted.** An agent processing a stranger's
message can otherwise reach every conversation on the account. A routine's
token can call three tools, only with a token proving a real message arrived,
and that token names the chat. Reading other conversations is not a refusal it
has to be talked into — it is not available.

**Sending to the wrong person is unrecoverable**, so an ambiguous name is
refused with the candidates listed rather than resolved to a guess.

**Rate limits are a circuit breaker**: a per-chat cooldown and an hourly cap
across all chats, so a fault costs you a few messages rather than your number.

WhatsApp marks messages sent through any unofficial client with an `AI` label.
That is Meta's, applied to the client, and nothing here can or should remove
it.

## Development

```bash
pip install -e ".[dev]"
pytest -q
```

362 tests. Postgres and Mongo suites skip unless `WA_TEST_POSTGRES` /
`WA_TEST_MONGO` point at a server.

## Limits

- **One number, one process.** By design.
- **History arrives once, at pair time.** whatsmeow can request more, but
  neonize does not export the call, so it is not reachable from Python.
- Group participant names come from message metadata, so a silent member of a
  group may show as a number.

## Licence

Apache-2.0.
