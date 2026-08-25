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
git clone <repo> && cd whatsapp-mcp
pip install -e ".[dev]"
pytest -q
```

## First run

```bash
python run.py
```

`python -m wa_mcp` does exactly the same thing and takes the same options.

Open <http://127.0.0.1:8100>. You will get a QR code — scan it with
**WhatsApp → Settings → Linked Devices → Link a device**.

On localhost there is no token, no sign-in and nothing to configure: the server
is open because only this machine can reach it. The QR is the front door.

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

Locally, the URL is all there is:

```
http://127.0.0.1:8100/mcp
```

**Settings → Connect an AI client** has it with a copy button. That is the place
to get it — the startup log prints it too, but a terminal you have closed is no
help, and neither is one you never saw because it runs as a service.

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

> `sqlite:///path` is treated as an **absolute** path here, not the relative one
> SQLAlchemy's three-slash form implies. A relative database silently created
> next to whatever directory you happened to start in is worse than an error.

## Docker

```bash
docker build -t whatsapp-mcp .
docker run -p 8100:8100 --env-file .env -v wa-data:/data whatsapp-mcp
```

**Mount the volume.** The session lives in `/data`. Losing it means re-pairing,
and re-pairing means the message archive starts over — history syncs once.

The image is glibc-based on purpose: neonize ships a CGO shared library, and
Alpine's musl fails at import time rather than at build time.

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
