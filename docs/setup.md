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
pip install suprai-whatsapp-mcp
```

From source:

```bash
git clone <repo> && cd suprai-whatsapp-mcp
pip install -e ".[dev]"
pytest -q
```

## First run

```bash
python -m wa_mcp --token=generate
```

That prints a bearer token and starts the server. Save the token — it is the
credential for the web UI and the MCP endpoint, and anyone holding it can read
and send on your WhatsApp account.

Open the URL it prints, then **WhatsApp → Settings → Linked Devices → Link a
device**, and scan.

### Then wait

History sync is not instant, and it matters more than it looks:

- WhatsApp sends history **exactly once, at pair time**. There is no way to
  ask for more later. The whole conversation archive you will ever have is
  decided in the minute after you scan.
- `WA_HISTORY_DAYS` and `WA_HISTORY_SIZE_MB` are read **at pair time only**.
  Changing them later does nothing until you unlink and pair again.
- Auto-reply is held until sync settles, so that switching it on does not
  answer weeks of old messages at once.

The UI shows progress. On a busy account expect a few thousand messages and a
couple of minutes.

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
the session stays a local file — which means the container still needs a
volume.

For one number, SQLite is the right answer. The others exist because the same
code runs inside a larger system.

> `sqlite:///path` is treated as an **absolute** path here, not the relative
> one SQLAlchemy's three-slash form implies. A relative database silently
> created next to whatever directory you happened to start in is worse than an
> error.

## Running it behind a tunnel

An MCP client needs to reach the server, and OAuth builds its redirect and
metadata from `PUBLIC_BASE_URL`, so it has to be the public address:

```bash
PUBLIC_BASE_URL=https://wa.example.com python -m wa_mcp --port 8100
```

Cloudflare named tunnels work well. Quick tunnels (`--url`) are unreliable for
this — they frequently establish only one of four edge connections and 404.

ngrok's free tier serves an interstitial page before your app, which breaks the
OAuth redirect.

## Docker

```bash
docker build -t suprai-wa .
docker run -p 8100:8100 --env-file .env -v wa-data:/data suprai-wa
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
python -m wa_mcp [--host H] [--port P] [--database-url URL] [--data-dir DIR]
                 [--token TOKEN | --token=generate] [--log-level LEVEL]
                 [--print-config] [--mint-routine-token]
```

`--print-config` resolves everything and exits — the quickest way to see which
database and data directory you are actually about to use.

`--mint-routine-token` prints a restricted credential for a hand-off webhook's
connector, on stdout so it can be piped. See
[auto-reply](auto-reply.md#security).
