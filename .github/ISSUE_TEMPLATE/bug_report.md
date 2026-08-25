---
name: Bug report
about: Something behaves differently from what it says it does
labels: bug
---

**What happened, and what you expected instead**

**To reproduce**
Ideally the settings involved. `python -m wa_mcp --print-config` shows the
resolved configuration — please check it for a token before pasting.

**Version and environment**
- version:
- Python:
- OS:
- storage: sqlite / postgres / mongo
- backend: model / webhook

**Logs**
Run with `LOG_LEVEL=DEBUG` if you can. Redact chat contents and tokens.

> Do not paste `WA_AUTH_TOKEN`, an `api_key`, or a session token. Any of them
> is full access to the WhatsApp account.
