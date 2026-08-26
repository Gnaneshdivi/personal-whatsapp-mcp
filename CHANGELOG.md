# Changelog

Notable changes only. Dates are release dates.

## 0.2.0 — unreleased

First release intended for anyone other than its author.

The package is named `personal-whatsapp-mcp`: personal because it links one
number that belongs to one person, and because it is not an official WhatsApp
or Meta product. The command is `personal-whatsapp-mcp`, and application data
lives in a `personal-whatsapp-mcp` directory under the platform's data path.

### Added
- MCP server with 23 tools, a WhatsApp-Web-style UI, and auto-reply.
- Auto-reply in two modes: an OpenAI-compatible model, or a webhook — either
  waiting for the reply or handing the message over.
- Guardrails: context-only answering, topic allow/deny, keyword blocks checked
  before the model runs, and a fallback message.
- AI disclosure, sent once per conversation before the first automated reply.
- Active hours in an explicit timezone, with an optional out-of-hours note.
- Periodic summaries led by what is waiting on you.
- Watch rules — keywords and VIP contacts — that run with auto-reply off.
- Delivery tokens: a hand-off webhook gets a credential good for three tools,
  one chat, and a few minutes.
- Storage on SQLite, Postgres or Mongo behind one setting.

### Behaviour worth knowing
- **Authentication is one bearer token**, given as a header or `?k=`. Not
  needed on loopback, generated and printed when reachable from elsewhere.
  There is no OAuth: it existed, worked, and was a great deal of machinery to
  get a credential into a connector dialog that accepts a URL.
- **Log out removes everything**: it unlinks WhatsApp, deletes messages, chats
  and settings, and revokes every issued credential. History syncs once at pair
  time, so this cannot be undone by pairing again.

### Notes for anyone upgrading a pre-release install
- `send_images` is now `send_media`, and `max_image_bytes` is
  `max_media_bytes`. The old keys are still read.
- `notify.jid` no longer decides where alerts go on its own; `notify.route`
  does. A config with a number set is migrated to `route: number`.
- `webhook.prompt_template` is gone. Both backends now send the same
  instruction, edited once in `model.system_prompt`.
