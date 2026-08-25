# Contributing

## Getting set up

```bash
pip install -e ".[dev]"
pytest -q
```

You need **libmagic** on the machine (`brew install libmagic`,
`apt install libmagic1`) — neonize imports python-magic at module load, so
without it nothing imports and the traceback blames a Python package.

The Postgres and Mongo suites skip unless a server is reachable:

```bash
WA_TEST_POSTGRES=postgresql://localhost/wa_test \
WA_TEST_MONGO=mongodb://localhost/wa_test pytest -q
```

## Before you open a pull request

```bash
ruff check wa_mcp tests --select F,E9
pytest -q
```

## What tests are for here

This talks to somebody's real phone number, so the tests are mostly about
things that are expensive to get wrong rather than coverage for its own sake.
A few of them exist because of a specific incident, and the docstring says
which — those are worth reading before changing the behaviour they pin.

If you fix a bug, the test should fail without the fix. Reverting the change
and watching it go red is worth the thirty seconds.

## Things to know before changing them

**History syncs once, at pair time.** There is no way to ask WhatsApp for more
later. Anything that drops the message store cannot be undone, and deleting
`app.db` during development costs whatever was in it.

**Schema changes must be additive.** `MIGRATIONS` in `store/sqlite.py` adds
columns on open. A change that requires a rebuild would make users choose
between upgrading and keeping their messages.

**Three storage backends implement one port.** A change to `store/base.py`
means three implementations and a test that runs against each.

**Some prompt text is deliberately not configurable** — the injection guard,
the delivery clause, the anti-mirroring and anti-guessing rules. Each is there
because a specific failure reached a real person. If you want to move one into
settings, please open an issue first.

## Style

Match the surrounding code. Comments explain *why*, especially where the
obvious approach is wrong — most of the comments here are about a thing that
was tried and did not work.
