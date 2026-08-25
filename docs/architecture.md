# How the code is laid out

For anyone adding something. The user-facing docs are elsewhere; this is the
map.

## You do not need a WhatsApp number

The whole suite runs against temporary SQLite files and a fake client:

```bash
pip install -e ".[dev]"
pytest -q          # 335 passing, no phone, no network
```

Only pairing and live sending need a real account, and nothing in the test
suite does either. This is worth knowing before you assume you cannot work on
it.

## One process, four layers

```
  wa_mcp/app.py          MCP tools (22) + the ASGI app + auth
  wa_mcp/web.py          the HTTP routes behind the UI
  wa_mcp/ui.py           the chat UI: CSS, JS, markup
  wa_mcp/settings_ui.py  the settings page, same shape
        │
  wa_mcp/runtime.py      one object holding the socket, store and engine
        │
  wa_mcp/trigger/        auto-reply: engine, backends, settings, summaries
  wa_mcp/whatsapp/       the socket: client, events, contacts, jid, extract
  wa_mcp/store/          base.py is the port; sqlite/postgres/mongo implement it
```

Nothing above talks to neonize directly except `whatsapp/client.py`, and
nothing talks to SQL except `store/*`. Those two boundaries are what make the
rest testable without a phone or a server.

## Where a change goes

| You want to | Start in |
|---|---|
| add an MCP tool | `app.py` — one decorated function, plus a test |
| add a setting | `trigger/settings.py`, then `settings_ui.py`. A test fails until the form has a control for it |
| change reply behaviour | `trigger/engine.py` for the gates, `trigger/backends.py` for the prompt |
| add a storage backend | implement `store/base.py`; the store tests run against every backend |
| change the chat UI | `ui.py`. A test fails if a rendered class has no rule |
| touch the WhatsApp socket | `whatsapp/client.py`, the one file that knows neonize exists |

## Things that will bite you

**`whatsapp/client.py` is 950 lines** and the least pleasant file here. It is
one class because the neonize client, the event handlers and the session state
are genuinely coupled — splitting it has to preserve that, not just move code.

**Three backends implement one port.** A method added to `store/base.py` means
three implementations. The store tests are parametrised over all three, so
Postgres and Mongo are skipped unless a server is reachable — if you change
storage, run them:

```bash
WA_TEST_POSTGRES=postgresql://localhost/wa_test \
WA_TEST_MONGO=mongodb://localhost/wa_test pytest -q
```

**Schema changes must be additive.** `MIGRATIONS` in `store/sqlite.py` adds
columns on open. Anything needing a rebuild would make users choose between
upgrading and keeping their messages, and history cannot be re-fetched.

**Some prompt text is not configurable on purpose** — the injection guard, the
delivery clause, the anti-mirroring and anti-guessing rules in
`trigger/backends.py`. Each exists because a specific failure reached a real
person, and the comment above each says which. Please open an issue before
moving one into settings.

## Tests

They are about things expensive to get wrong rather than coverage. Several
exist because of a specific incident and say so in the docstring — those are
worth reading before changing the behaviour they pin.

If you fix a bug, the test should fail without the fix. Reverting your change
and watching it go red takes thirty seconds and is the difference between a
test and a comment.

Some enforce structure rather than behaviour, and will fail on a change you did
not expect them to notice:

- every settings field has a control on the form,
- every class the UI renders has a CSS rule,
- every environment variable appears in `.env.example`,
- both backends send the same instruction,
- every declared dependency is imported.

## Good first things

- A storage backend, or the Mongo one's missing full-text parity.
- Incoming reactions — we send them, we do not parse them.
- Wiring `GetAllContacts` through ctypes, so names come from WhatsApp's own
  contact store rather than only from chats.
- Exporting `BuildHistorySyncRequest` in neonize, which would let this ask for
  history after pairing instead of only at it. That is a PR to neonize, not
  here, and it is the single biggest limitation of the project.
