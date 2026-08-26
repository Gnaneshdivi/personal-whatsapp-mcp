"""Assemble README.md from the hand-written head, the docs, and a tail.

Reading the docs rather than restating them means the README cannot quietly
disagree with docs/ about how the thing works.
"""
from __future__ import annotations

import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parent.parent

HEAD = """# personal-whatsapp-mcp — WhatsApp MCP server for Claude and any LLM

[![CI](https://github.com/Gnaneshdivi/personal-whatsapp-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/Gnaneshdivi/personal-whatsapp-mcp/actions/workflows/ci.yml)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![MCP](https://img.shields.io/badge/MCP-23%20tools-purple)](https://modelcontextprotocol.io)

**Connect your personal WhatsApp number to Claude, ChatGPT, or any
Model Context Protocol client — and reply automatically when you are away.**

Self-hosted, open source, and a single process. One phone number, 23 MCP tools,
a web UI that looks like WhatsApp Web, and an auto-reply you configure rather
than code.

No Redis, no database server, no build step. SQLite is the default and ships
with Python.

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

Open <http://127.0.0.1:8100>, scan the QR code with **WhatsApp → Linked
Devices**, and wait for history to sync.

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

---

## What it is

Three things sharing one WhatsApp connection:

**An MCP server.** 23 tools — send, search, read threads, download media,
delivery receipts, group info. Point Claude Desktop, Claude Code, or any MCP
client at `/mcp`.

**A web UI.** Two panes, live over server-sent events, with delivery ticks,
lazy-loaded history, and search across both chats and message text.

**An auto-reply, in two modes.** Either an OpenAI-compatible model replies from
here, or your own webhook does — synchronously, or by handing the message over
to an agent that answers in its own time.

![The web UI: a chat list on the left and an open conversation on the right, with delivery ticks](assets/02-chats.png)

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
follows — see [Choosing a model](#choosing-a-model).

---

## MCP tools

All 23 tools exposed at `/mcp`, callable from Claude or any MCP client.

| Tool | What it does |
|---|---|
{TOOLS}

![Claude calling the WhatsApp tools: status, recent messages and a summary of the day](assets/07-claude-using-it.png)

---
"""

FAQ = """
---

## Frequently asked questions

### Can Claude read and send my WhatsApp messages?

Yes. Point Claude at `http://127.0.0.1:8100/mcp` after pairing and it gets 23
tools covering sending, searching, reading threads, downloading media, delivery
receipts and group info. It uses your own number, linked the same way WhatsApp
Web is.

### Is this an official WhatsApp API?

No. This is an independent, unofficial client and is not affiliated with
WhatsApp or Meta. It uses the same multidevice protocol WhatsApp Web uses, via
[whatsmeow](https://github.com/tulir/whatsmeow). The official route is the
WhatsApp Business API, which requires a business account and approved message
templates. This is for your personal number.

### Do I need a WhatsApp Business account?

No. It links to a normal personal WhatsApp account by scanning a QR code under
Linked Devices, exactly like WhatsApp Web.

### Will my account get banned?

Nothing here can promise otherwise. WhatsApp's Terms of Service govern what you
may do with your account. The risk that matters is behaving like a bot at
scale, so this ships a per-chat cooldown and an hourly cap across all chats as a
circuit breaker, and an allowlist so auto-reply starts off answering nobody.
Automating replies to real people is your responsibility.

### Does it cost anything to run?

The server is free and open source. The only cost is your model: measured at
461 prompt + 24 completion tokens per reply, `gpt-4o-mini` works out around
**$0.08 per 1,000 replies**. Running a local model through Ollama costs
nothing. The webhook mode has no model cost here at all, because your endpoint
answers.

### Which model should I use?

`gpt-4o-mini` is the cheapest that behaved correctly across the test cases —
see [Choosing a model](#choosing-a-model) for the measurements. Below that
class, models stop distinguishing "I do not know" from "here is an answer", and
that failure lands on a real person on your real number.

### Is this a WhatsApp bot?

It can be. With auto-reply on it behaves as a WhatsApp bot that answers on your
own number; with auto-reply off it is purely an MCP server your assistant reads
and writes through. WhatsApp automation of this kind is on you to use
responsibly — the guardrails, allowlist and rate limits exist because the other
end is a real person.

### Can I run it without an AI model at all?

Yes. Auto-reply is off by default. You can use it purely as an MCP server, and
the watch rules — keyword and VIP alerts — run with auto-reply off entirely.

### Does it work with ChatGPT, Cursor, or other MCP clients?

Yes. It is a standard Model Context Protocol server over streamable HTTP, so
any MCP client can connect. There is nothing Claude-specific in it.

### Where is my data stored?

On your machine. SQLite in a `personal-whatsapp-mcp` directory under your
platform's data path, unless you point `WA_DATABASE_URL` at Postgres or Mongo.
No message ever leaves your server except the one being answered, which goes to
whichever model endpoint you configured.

### Can I read old messages from before I connected?

Only what WhatsApp sends at pair time, which is once and never again. There is
no way to request more later. Whatever arrives in the minute after you scan is
the entire archive you will ever have.

### Can I use it for more than one number?

No. One number, one process, by design. Run a second instance with a separate
`WA_DATA_DIR` for a second number.

### Why do my messages show an "AI" label in WhatsApp?

WhatsApp marks messages sent through any unofficial client that way. It is
applied by Meta to the client, not by anything in this project, and nothing
here can or should remove it.
"""

TAIL = """
---

## Documentation

Every section above is also a standalone file, which is the easier thing to
link someone to:

| | |
|---|---|
| [docs/setup.md](docs/setup.md) | Install, pairing, storage, tunnels |
| [docs/recipes.md](docs/recipes.md) | Step by step: an OpenAI-compatible model, and a Claude Routine |
| [docs/auto-reply.md](docs/auto-reply.md) | The two modes, the prompt, choosing a model, the security model |
| [docs/settings.md](docs/settings.md) | Every environment variable and all 64 auto-reply settings |
| [docs/architecture.md](docs/architecture.md) | Where the code lives — start here to contribute |

## Limits

- **One number, one process.** By design.
- **History arrives once, at pair time.** whatsmeow can request more, but
  neonize does not export the call, so it is not reachable from Python.
- Group participant names come from message metadata, so a silent member of a
  group may show as a number.

## Contributing

```bash
pip install -e ".[dev]"
pytest -q
```

{TESTS} tests. Postgres and Mongo suites skip unless `WA_TEST_POSTGRES` /
`WA_TEST_MONGO` point at a server.

See [CONTRIBUTING.md](CONTRIBUTING.md) for what the tests are for and which
behaviour is deliberately not configurable, and
[CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md).

Security reports: [SECURITY.md](SECURITY.md) — please do not open a public
issue.

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

MIT. See [LICENSE](LICENSE).
"""

# Order matters: install, then configure, then the deep ends.
DOCS = [
    ("docs/setup.md", "Setup and installation"),
    ("docs/auto-reply.md", "Auto-reply"),
    ("docs/recipes.md", "Recipes: setting up replies"),
    ("docs/settings.md", "Settings reference"),
    ("docs/architecture.md", "Architecture"),
]


def tools_table() -> str:
    import ast

    tree = ast.parse((ROOT / "wa_mcp" / "app.py").read_text())
    rows = []
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and \
           any("mcp.tool" in ast.unparse(d) for d in n.decorator_list):
            doc = (ast.get_docstring(n) or "").strip().split("\n")[0]
            rows.append((n.lineno, f"| `{n.name}` | {doc} |"))
    return "\n".join(r for _, r in sorted(rows))


def demote(text: str, title: str) -> str:
    """Nest a whole document under one `##` section.

    Some docs use several `#` headings as top-level dividers rather than one
    title. Shifting everything down by one would leave those dividers level
    with the section itself, and their own subsections level with them — the
    settings reference came out with Environment, Auto-reply and Master all
    siblings. Where a doc does that, everything below its title shifts by two
    instead, so the shape survives.
    """
    lines = text.splitlines()
    h1s = sum(1 for line in lines if re.match(r"^# ", line))
    shift = 2 if h1s > 1 else 1

    out, seen_title = [], False
    in_fence = False
    for line in lines:
        if line.startswith("```"):
            in_fence = not in_fence
        m = None if in_fence else re.match(r"^(#{1,5}) (.*)$", line)
        if not m:
            out.append(line)
            continue
        level, head = len(m.group(1)), m.group(2)
        if not seen_title and level == 1:
            out.append(f"## {title}")
            seen_title = True
            continue
        out.append("#" * min(level + shift, 6) + " " + head)
    body = "\n".join(out)
    # links written relative to docs/ have to work from the repo root
    body = re.sub(r"\]\((?!https?:|#|/)([a-z-]+\.md)", r"](docs/\1", body)
    # ../assets/x.png is correct from docs/ and wrong from the root
    body = body.replace("](../assets/", "](assets/")
    return body.strip() + "\n"


def slug(heading: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", heading.strip().lower()).strip("-")


def toc(markdown: str) -> str:
    """Two levels deep. Anchors follow GitHub's rule for a repeated heading:
    the second occurrence gets -1, so a link to it does not land on the first."""
    lines, used = [], {}
    in_fence = False
    for line in markdown.splitlines():
        if line.startswith("```"):
            in_fence = not in_fence
        if in_fence:
            continue
        m = re.match(r"^(#{2,3}) (.*)$", line)
        if not m:
            continue
        depth, head = len(m.group(1)), m.group(2)
        base = slug(head)
        n = used.get(base, 0)
        used[base] = n + 1
        anchor = base if n == 0 else f"{base}-{n}"
        lines.append(f"{'  ' * (depth - 2)}- [{head}](#{anchor})")
    return "\n".join(lines)


def main() -> None:
    tests = "397"
    head = HEAD.replace("{TOOLS}", tools_table())
    middle = "\n\n---\n\n".join(
        demote((ROOT / path).read_text(), title) for path, title in DOCS)
    body = head + "\n" + middle + FAQ + TAIL.replace("{TESTS}", tests)

    marker = "\n---\n\n## Quick start"
    assert marker in body
    body = body.replace(marker, "\n## Contents\n\n" + toc(body) +
                        "\n\n---\n\n## Quick start", 1)

    (ROOT / "README.md").write_text(body)
    print(f"  README.md: {len(body.splitlines())} lines, {len(body)} bytes")


if __name__ == "__main__":
    main()
