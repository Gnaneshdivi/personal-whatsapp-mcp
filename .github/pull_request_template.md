**What this changes, and why**

**How you checked it**

- [ ] `pytest -q`
- [ ] `ruff check wa_mcp tests --select F,E9`
- [ ] If it fixes a bug: the test fails without the fix (please confirm you
      reverted it and watched it go red)
- [ ] If it adds a setting: it has a control on the settings form and appears
      in `docs/settings.md`
- [ ] If it touches storage: run against Postgres and Mongo, or say you could
      not

**Anything a reviewer should look at twice**
