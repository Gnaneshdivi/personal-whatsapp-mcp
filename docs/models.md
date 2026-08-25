# Choosing a model

**The replies are the model's, not this server's.** Everything here shapes the
prompt — persona, guardrails, the instruction not to guess — but what comes
back is whatever the model produces. A weaker model will ignore instructions a
stronger one follows, and no amount of prompt work fixes that. Budget for the
model accordingly.

## What was measured

Six cases, three trials each, through the real prompt. Three must be escalated
(nothing in the conversation answers them) and three must be answered:

| Model | Score | Behaviour |
|---|---:|---|
| **openai/gpt-4o-mini** | **18/18** | recommended |
| anthropic/claude-haiku-4.5 | 18/18 | equal, ~7× the price |
| google/gemma-3-12b-it | 12/18 | escalates *everything*, including "Hi" |
| deepseek/deepseek-v4-flash | 3/6 | invents facts; often omits the marker |
| qwen/qwen3-30b-a3b-instruct | 3/6 | invents |
| openai/gpt-oss-120b | 2/6 | invents |
| mistralai/mistral-small-24b | 2/6 | invents |
| openai/gpt-4.1-nano | — | mirrored the sender's language, replied in nonsense |
| google/gemini-2.5-flash | — | invents confidently |
| amazon/nova-micro-v1 | 0/6 | API errors |
| openai/gpt-5-mini | — | returns no content: reasoning consumes `max_tokens` |

### The two failure modes

**Inventing.** Asked `Scrum undatledha?` — Telugu for "did the scrum happen?" —
Gemini answered `Ledu, inka raledu` ("no, not yet"), a fact it could not have
known. Asked to share a deck, `gpt-oss-120b` and `qwen3-30b` both replied
`Sure, I'll send it over by tonight`, committing the account owner to something.
This is the dangerous one: it is fluent, confident and wrong, and the person
acts on it.

**Over-escalating.** `gemma-3-12b-it` is tempting at a third of the price, but
it hands over on `Hi` and `thanks!` too. Every greeting notifies you and sends
the fallback instead of a reply. Cheap and useless.

`gpt-4o-mini` was the cheapest model that did neither.

## Cost

Measured on a real call: **461 prompt + 24 completion tokens**. The prompt is
mostly fixed — persona, guardrails, injection guard — so it barely moves with
message length.

| Model | Per 1000 replies |
|---|---:|
| openai/gpt-4o-mini | $0.08 |
| anthropic/claude-haiku-4.5 | $0.55 |
| google/gemma-3-12b-it | $0.03 |

At any realistic volume the difference between these is pennies. Choose on
behaviour.

## Minimum

**Do not go below `gpt-4o-mini` class.** Below it, models stop distinguishing
"I do not know" from "here is an answer", and the failure lands on a real
person on your real number.

If you must use a cheaper model, then:

- set a `fallback_message` you are content for a stranger to receive,
- keep `context_only` **on**,
- keep the reply scope on an **allowlist**, not everyone,
- and read `wa_reply_log` for the first day.

## Reasoning models

`gpt-5-mini` and similar spend `max_tokens` on reasoning before emitting
anything, so at 300 they return empty content and this server records a backend
failure. If you want one, raise `model.max_tokens` well past the reasoning
budget — and note that latency goes from ~2s to ~7s, which is noticeable in a
live chat.

## Endpoints

Any OpenAI-compatible `/chat/completions`. Set `model.base_url` to the API
root; pasting the full endpoint also works, since a trailing
`/chat/completions` is trimmed rather than appended twice.

Tested: OpenRouter, OpenAI, Groq, Together, Ollama, LM Studio.

## Re-running this

The comparison is not a fixture — providers change models under the same name.
Point `model.model` at a candidate and try the cases above through
`wa_test_reply`, which runs the configured backend without sending anything.
