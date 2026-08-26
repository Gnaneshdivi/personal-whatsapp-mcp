# Setup

## What you need

- **Python 3.11+**
- **libmagic.** neonize imports python-magic when the module loads, so without
  it the package will not import at all — and the traceback names a Python
  package, not the missing C library, which sends most people the wrong way.
  ```bash
  brew install libmagic          # macOS
  apt install libmagic1          # Debian/Ubuntu
  ```
- **A phone number.** One number per install. The phone must be reachable to
  scan the QR, and should stay online — WhatsApp unlinks a companion device
  that has not seen the phone for about two weeks.

There is no Redis and no database server. SQLite is the default and ships with
Python.

## Install

```bash
pip install personal-whatsapp-mcp
```

That puts a `personal-whatsapp-mcp` command on your PATH. It takes the same
options as `run.py` and needs no source directory:

```bash
personal-whatsapp-mcp
personal-whatsapp-mcp --print-config
```

Install into a **virtual environment** rather than system Python — it pulls in
neonize, which ships a compiled shared library:

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install personal-whatsapp-mcp
```

If pip says *"requires a different Python"*, that is the whole problem: this
needs 3.11+, and the system `python3` on macOS is still 3.9.

### From source

What you want if you intend to change it:

```bash
git clone https://github.com/Gnaneshdivi/personal-whatsapp-mcp.git
cd personal-whatsapp-mcp
pip install -e ".[dev]"
pytest -q
python run.py
```

`python run.py`, `python -m wa_mcp` and `personal-whatsapp-mcp` all start the
same server and take the same options.

### Building a wheel yourself

Only needed to install somewhere with no access to PyPI:

```bash
pip install build
python -m build          # writes dist/*.whl and dist/*.tar.gz
pip install dist/*.whl
```

## First run

```bash
python run.py                # from the source tree
personal-whatsapp-mcp        # if you installed the wheel
```

`python -m wa_mcp` does the same thing. All three take the same options.

Open <http://127.0.0.1:8100>. You will get a QR code — scan it with
**WhatsApp → Settings → Linked Devices → Link a device**.

On localhost there is no token, no sign-in and nothing to configure: the server
is open because only this machine can reach it. The QR is the front door.

The chat view once history has synced:

![The web UI: a chat list on the left and an open conversation on the right, with delivery ticks](../assets/02-chats.png)

### Then wait

History sync is not instant, and it matters more than it looks:

- WhatsApp sends history **exactly once, at pair time**. There is no way to ask
  for more later. The whole conversation archive you will ever have is decided
  in the minute after you scan.
- `WA_HISTORY_DAYS` and `WA_HISTORY_SIZE_MB` are read **at pair time only**.
  Changing them later does nothing until you unlink and pair again.
- Auto-reply is held until sync settles, so that switching it on does not answer
  weeks of old messages at once.

The UI shows progress. On a busy account expect a few thousand messages and a
couple of minutes.

## Connecting an AI client

Three steps, in this order. The first two happen here; the third happens in
Claude or ChatGPT.

### 1. Link your WhatsApp

Open the server and scan the QR with **WhatsApp → Settings → Linked devices →
Link a device**. Nothing else works until a number is linked, so this is first.

![The pairing page: a QR code to scan with WhatsApp, showing "Waiting for you to scan…"](../assets/01-pair-qr.png)

Wait for the sync to settle before moving on. The header says when it has.

### 2. Copy the MCP endpoint

Go to **Settings → Connect an AI client**. It shows the full URL with a copy
button:

```
http://127.0.0.1:8100/mcp                 # on this machine
https://your-host/mcp?k=<token>           # reachable from elsewhere
```

That is the place to get it. The startup log prints it too, but a terminal you
have closed is no help, and neither is one you never saw because the server runs
as a service.

![Settings → Connect an AI client, showing the MCP endpoint with a Copy button](../assets/05-mcp-endpoint.png)

> Behind a tunnel the token is part of that URL, which makes the URL the whole
> credential. Treat it like a password: anyone holding it can read and send on
> your WhatsApp account. Do not paste it into a screenshot, an issue, or a chat.

### 3. Add it as a connector

**In Claude** — Settings → Connectors → **Add custom connector**. Give it a
name, paste the URL, and Continue.

![Claude's Add custom connector dialog with the name and the MCP URL filled in](../assets/06-claude-add-connector.png)

**In ChatGPT** — Settings → Connectors → add an MCP server, same URL.

Any MCP client works the same way: this is a standard Model Context Protocol
server over streamable HTTP, with nothing specific to one vendor.

Once it connects, all 23 tools are available and the assistant can read and send
on your number.

![Claude calling the WhatsApp tools: status, recent messages and a summary of the day](../assets/07-claude-using-it.png)

### If the connector will not connect

- **Check the URL ends in `/mcp`.** The bare host serves the web UI, not MCP.
- **Check the token is on the URL** if the server is reachable from elsewhere.
  Without it every request is a 401 and the client cannot tell you why.
- **Open the URL in a browser.** `GET /mcp` returning *405 Method Not Allowed*
  is correct and means the endpoint is alive — MCP requires POST.
- **A generic icon next to the connector is not a fault.** Claude does not yet
  render the icon a server advertises, so every custom connector shows the same
  placeholder.

## Running it beyond this machine

Set `PUBLIC_BASE_URL` to the public address. That is how the server knows it is
no longer only reachable from here, so it protects itself instead of running
open:

```bash
PUBLIC_BASE_URL=https://wa.example.com python run.py --port 8100
```

It generates a token, stores it, and prints both URLs:

```
  Reachable from other machines, so access needs a token.

  Open this:      https://wa.example.com/?k=Tfk0n7Tx…
  Connect MCP to: https://wa.example.com/mcp?k=Tfk0n7Tx…

  The same one after a restart. Set WA_AUTH_TOKEN to choose your own,
  or WA_ALLOW_OPEN=1 for none.
```

The token is the same across restarts, so a connector you configure once keeps
working. It goes in the URL because a connector dialog takes a URL and nothing
else — which makes that URL the entire credential. **Anyone holding it can read
and send on your WhatsApp account.**

The first browser load trades `?k=` for an HttpOnly session cookie and
redirects to the bare address, so the token stops appearing in browser history
and proxy logs. The cookie lasts 30 days.

### Tunnels

Cloudflare named tunnels work well. Quick tunnels (`--url`) are unreliable for
this — they frequently establish only one of four edge connections and 404.

ngrok works. Its free tier serves an interstitial page before your app, which is
a nuisance in a browser but does not affect the MCP endpoint.

## Configuration

Everything is environment variables. Copy `.env.example` to `.env` in the
working directory — it is read at startup, and real environment variables win
over it, so a stale file cannot override what your platform sets.

Full reference: [settings.md](settings.md).

## Storage

One variable, `WA_DATABASE_URL`, decides everything:

| Value | Messages | WhatsApp session |
|---|---|---|
| *unset* | SQLite in the data dir | file beside it |
| `postgresql://…` | Postgres | **in Postgres** |
| `mongodb://…` | Mongo | file on disk |
| `sqlite:////abs/path.db` | that file | file beside it |

Postgres is the only one that makes the process stateless, because whatsmeow's
session store is SQL and can live there. Mongo cannot hold it, so even on Mongo
the session stays a local file — which means the container still needs a volume.

For one number, SQLite is the right answer. The others exist because the same
code runs inside a larger system.

All three implement the same interface and are held to the same test suite,
which runs against a real Postgres and a real Mongo, not a stand-in. Set
`WA_TEST_POSTGRES` and `WA_TEST_MONGO` to run those yourself.

> `sqlite:///path` is treated as an **absolute** path here, not the relative one
> SQLAlchemy's three-slash form implies. A relative database silently created
> next to whatever directory you happened to start in is worse than an error.


## Upgrading

Schema changes are additive and applied on open, so an upgrade keeps your
messages. Do not delete `app.db` to "reset" — the messages in it cannot be
re-fetched from WhatsApp.

## Command line

```
python run.py [--host H] [--port P] [--database-url URL] [--data-dir DIR]
              [--token TOKEN | --token=generate] [--log-level LEVEL]
              [--print-config] [--mint-routine-token]
```

`--print-config` resolves everything and exits — the quickest way to see which
database and data directory you are actually about to use.

`--mint-routine-token` prints a restricted credential for a hand-off webhook's
connector, on stdout so it can be piped. See
[auto-reply](auto-reply.md#security).

## Logging out

**Settings → Log out** unlinks WhatsApp, deletes every message, chat and
setting, and revokes all issued credentials. History syncs once at pair time, so
this cannot be undone by pairing again.
