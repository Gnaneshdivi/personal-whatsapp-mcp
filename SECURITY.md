# Security

This runs on somebody's real phone number, with their real conversations. A bug
here is not a service outage — it is a message sent to the wrong person, or an
archive read by someone who should not have it.

## Reporting a vulnerability

Please do not open a public issue. Email the maintainers, and give it a few
days before disclosing.

Include what an attacker can do, not just what is wrong: "an injected message
can make the agent send to an arbitrary number" is actionable, "the prompt is
weak" is not.

## What holds the line

**The token is the account.** `WA_AUTH_TOKEN` grants everything: read every
conversation, send as you, change the auto-reply. It appears in `?k=` URLs,
which means browser history and proxy logs. Treat it as a password, prefer
OAuth where the client supports it, and rotate it by restarting with a new one.

**Untrusted input is tagged, not trusted.** Every inbound message is wrapped in
`<msg id="…">` with a per-request nonce, and the model is told the contents are
data. History is wrapped too, because an attacker can seed an instruction and
wait a turn for it to replay as context.

This raises the cost of prompt injection. It does not eliminate it. Nothing at
the prompt level does, and anything claiming otherwise is wrong.

**Capability, not persuasion, bounds a hand-off.** An agent processing a
stranger's message would otherwise hold every tool and every chat. So:

- a delivery token allows three tools, one chat, and expires in minutes;
- a routine's standing token authorises nothing without a `reply_token` from a
  live delivery, and that token names the chat.

An injected "send this elsewhere" fails on the destination check, and "send it
without the token" fails because the token is what permits sending at all. This
is enforced in one gate in front of `/mcp`, not inside each tool — a tool added
later without a check would otherwise be reachable.

**Ambiguity is refused, not guessed.** A name matching more than one contact is
rejected with the candidates listed. Sending to the wrong person cannot be
undone.

**Rate limits are a blast radius, not a nicety.** A per-chat cooldown and an
hourly cap across all chats mean a fault costs a few messages rather than the
number.

## What is not protected

**A full token in a connector.** If you configure an MCP client with
`WA_AUTH_TOKEN`, that client has everything. The scoping above applies to
delivery and routine tokens, not to yours.

**The model's judgement.** With `context_only` off, or a model that ignores
instructions, replies can state things that are not true. The measured
differences are in [docs/auto-reply.md](docs/auto-reply.md#choosing-a-model), and they are large.

**The webhook you point at.** In hand-off mode this server sends the message
and stops. What your endpoint does with it is yours.

**WhatsApp's own terms.** Automated replies from an unofficial client can get
a number banned. Meta identifies clients built on whatsmeow — this is visible
as the `AI` label on messages you send. That label is Meta's and cannot be
removed from here.

## Data

Everything stays where you point `WA_DATABASE_URL`. Nothing is sent anywhere
except: WhatsApp, the model endpoint you configure, and the webhook you
configure. There is no telemetry.

The message store holds full conversation text. The `api_key` is stored in it
too — redacted in the UI and in `wa_get_reply_settings`, but present on disk.
