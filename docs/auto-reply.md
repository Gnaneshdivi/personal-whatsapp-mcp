# Auto-reply

## What this is not

Worth being clear before anything else, because it sets expectations:

**There is no memory.** The assistant knows the last N turns of the
conversation it is replying to, and nothing else. It does not remember earlier
chats, does not accumulate facts about a contact, and does not learn. Ask it
something answered three months ago in a different thread and it will not know.

**There is no knowledge base.** No documents, no vector store, no retrieval.
The only way to give it standing facts is `guardrails.policy_note`, which is
pasted into the prompt on every call.

**It is not an agent.** In the default mode it produces one message and stops.
It cannot look anything up, take an action, or decide to do something later.

The message store is for *you* — the web UI, search, summaries and the MCP
tools. It is not a memory the model reads from. The model only ever sees the
current conversation.

If you want memory or tools, that is what the second mode is for: hand the
message to your own agent, which can have both.

## Two modes

### 1. Model — this server replies

```
message → prompt → your model endpoint → reply → sent
```

Set `backend` to `model` and give it any OpenAI-compatible endpoint. This
server builds the prompt, calls the model, applies the guardrails, and sends
what comes back.

The model has **no tools**. Its entire input is the instruction, your
guardrails, that one chat's recent history, and the message. It cannot read
other conversations, cannot see your contacts, and cannot choose a recipient —
this server sends the reply, always to the chat it came from.

That containment is why this mode is the default. The worst a hostile message
can do is influence the wording of a reply sent back to itself.

### 2. Webhook — your endpoint replies

Set `backend` to `webhook`. Then `webhook.expect_reply` picks one of two very
different things:

**`expect_reply: true` — wait for the answer.** This server POSTs, reads
`reply_path` out of your response, and sends it. Your endpoint has to answer
inside `timeout_seconds`. Use this when the logic lives in your app but the
reply is immediate.

**`expect_reply: false` — hand it over.** This server POSTs and stops. Nothing
is sent from here. Your endpoint decides whether to reply and sends it itself
through the MCP tools. This is the mode for anything queued, human-approved, or
slower than one request — and for an agent that needs tools or memory.

The prompt changes to match. In hand-off mode it names the chat and says
outright that nothing returned in the response is delivered, because an agent
told "write only the message" when nothing is reading it produces text that
goes nowhere, with no error anywhere.

## The prompt

Both backends are sent the **same** instruction. Only the transport differs —
the model gets a `messages` array, the webhook gets one string, because that is
all an HTTP body can carry.

```
1  persona and tone          model.system_prompt          you edit this
2  delivery clause           depends on the mode          fixed
3  no mirroring              fixed
4  no guessing               fixed
5  guardrails                your toggles
6  injection guard           fixed, fresh nonce each call
---
   history, as real turns; inbound wrapped, yours not
   the message being answered, wrapped
```

Layers 2–4 and 6 are not editable, because getting them wrong is not a matter
of taste:

- **Delivery** differs between the modes and they are opposites. A user editing
  tone must not be able to leave it contradicting the mode.
- **No mirroring** — the assistant is a different entity from you and has to
  sound like one, rather than echoing a sender's tone and forms of address back
  at them.
- **No guessing** — if it cannot tell what is being asked, it says so and emits
  the handoff marker instead of filling the turn. Half an answer is worse than
  none, because people act on it.
- **The injection guard** is a security control, not a preference.

## When it does not understand

It emits `notify.handoff_marker`. This server then:

1. strips the marker so it never reaches anyone,
2. sends your `fallback_message` instead of whatever the model improvised —
   having just admitted it did not follow the question, its apology is the
   least reliable sentence in the reply,
3. notifies you, if `notify.on_handoff` is on.

With no fallback configured, its own words are used, because silence leaves
someone waiting for an answer that is not coming.

## Choosing a model

**The replies are the model's, not this server's.** Everything here shapes the
prompt — persona, guardrails, the instruction not to guess — but what comes back
is whatever the model produces. A weaker model ignores instructions a stronger
one follows, and no amount of prompt work fixes that.

**Use `gpt-4o-mini` or better.** It was the cheapest model tested that neither
invented facts nor escalated every greeting. `claude-haiku-4.5` behaves the
same at roughly seven times the price.

Below that class, models stop distinguishing "I do not know" from "here is an
answer", and the failure lands on a real person on your real number. If you use
a cheaper one anyway: set a `fallback_message` you are happy for a stranger to
receive, keep `context_only` on, keep the reply scope on an allowlist, and read
`wa_reply_log` for the first day.

### Cost

A reply is about 460 prompt and 25 completion tokens, and the prompt is mostly
fixed, so it barely moves with message length. On `gpt-4o-mini` that is roughly
**$0.08 per 1,000 replies**. At any realistic volume the difference between
models is pennies — choose on behaviour, not price.

### Reasoning models

`gpt-5-mini` and similar spend `max_tokens` on reasoning before emitting
anything, so at the default 300 they return empty content and this server
records a backend failure. Raise `model.max_tokens` well past the reasoning
budget, and expect latency nearer 7s than 2s, which is noticeable in a live
chat.

### Endpoints

Any OpenAI-compatible `/chat/completions`. Set `model.base_url` to the API root;
pasting the full endpoint also works, since a trailing `/chat/completions` is
trimmed rather than appended twice.

Tested: OpenRouter, OpenAI, Groq, Together, Ollama, LM Studio.

Model behaviour drifts — providers change models under the same name — so try a
candidate through `wa_test_reply`, which runs the configured backend without
sending anything.

## Security

**Untrusted text is tagged.** Every inbound message is wrapped in
`<msg id="…">` with a per-request nonce, and the model is told that anything
inside is data, never instructions. History is wrapped too — an attacker can
seed an instruction and wait a turn for it to replay as context. Your own
replies are not wrapped; they are not untrusted input.

This raises the cost of an attack. It is not a guarantee, and nothing at the
prompt level is.

**Hand-off is where the real risk lives.** An agent holding this connector can
otherwise reach every conversation on the account, while reasoning about a
message a stranger wrote. So the boundary is not asked of the model:

- Each delivery mints a token good for **three tools** (`wa_send`,
  `wa_send_media`, `wa_typing`), **one chat**, expiring in minutes.
- Your routine's standing credential authorises nothing on its own. Sending
  requires a `reply_token` from a live delivery, and that token names the chat.
- So "send it without the token" fails, and "send it to this other number"
  fails. Reading other conversations is not a refusal it has to be talked into
  — it is not available.

Configure your routine's connector with a restricted token, not your full one.
A full token has all 23 tools and every chat.

```bash
python run.py --mint-routine-token
```

That prints one token. Use it as the connector's credential:

```
https://your-host/mcp?k=<the token>
```

It does not expire — delete its row from the `kv` table to revoke it.

**Rate limits are a circuit breaker.** A per-chat cooldown and an hourly cap
across all chats. They do not prevent a loop with another bot; they slow it to
something you notice and cap what it costs.

## Watch rules

`notify.*` runs **independently of replying** and works with auto-reply off.
Watching a number without answering on it is a legitimate setup, and the common
one to start with.

Keywords are matched case-insensitively; VIP contacts get through regardless.
In groups nothing is watched unless `watch_groups` is on.
