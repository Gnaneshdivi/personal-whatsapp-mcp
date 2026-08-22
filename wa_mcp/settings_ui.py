"""The settings page.

Split out of web.py, which had it as a 200-line inline f-string. The old page
had three problems worth naming, because they shaped this one:

  * **Save silently did nothing.** Its collector pre-declared `{model, webhook,
    reply}` and then wrote `o[parent][child]` for every dotted field name, so
    the first `guardrails.*` field threw on `undefined` and the submit handler
    died before it ever called fetch. No request, no error, no clue. The
    collector here walks the path and creates parents as it goes, so a field
    added to the form needs no matching change in the JS.
  * **Everything was visible at once**, including a webhook section that does
    nothing while the backend is set to model, and allowlists that do nothing
    while the scope is "all".
  * **yes/no dropdowns** for what are plainly switches, and no explanation of
    what any of it did -- several of these settings are the difference between
    a useful assistant and one that invents prices.

Rendering is server-side because the values come from a dataclass that already
knows its own defaults; shipping them as JSON and rebuilding the form in the
browser would duplicate that knowledge in a second place.
"""
from __future__ import annotations

import html
import json


def esc(s) -> str:
    return html.escape(str(s if s is not None else ""))


# --------------------------------------------------------------- the tokens

# Every {{token}} Context.tokens() can substitute. Listed here so the page and
# the engine cannot drift: if one is added there and not here it simply is not
# documented, which is better than documenting one that does not exist.
TOKENS = [
    ("{{message}}", "The message that arrived, as text."),
    ("{{prompt}}", "The fully rendered prompt, history included. Webhook only."),
    ("{{chat_name}}", "Contact or group name."),
    ("{{chat_jid}}", "Chat address, e.g. 919812345678@s.whatsapp.net. Stable — use it as a session key."),
    ("{{sender_name}}", "Who sent it. In a group this is the individual, not the group."),
    ("{{sender_jid}}", "The sender's address."),
    ("{{me_name}}", "Your own WhatsApp display name."),
    ("{{message_id}}", "Unique id of the incoming message."),
    ("{{timestamp}}", "When it arrived."),
    ("{{history}}", "Recent turns of this conversation, oldest first."),
    ("{{chat_link}}", "A wa.me link WhatsApp turns into a tap that opens the chat. "
                      "Empty for @lid senders, which carry no phone number."),
    ("{{reply_token}}", "A token scoped to this one delivery — send in this chat only, "
                        "expiring shortly. Only for a hand-off webhook."),
    ("{{policy}}", "Your guardrails, rendered as instructions."),
    ("{{reason}}", "Why an alert fired. Only substituted in the alert wording."),
]

TOKEN_HELP = "Available tags:\n\n" + "\n".join(f"{t} — {d}" for t, d in TOKENS)


# ------------------------------------------------------------- form pieces

def info(tip: str) -> str:
    """A hover/focus explanation. tabindex so it is reachable without a mouse."""
    return f'<span class="i" tabindex="0" data-tip="{esc(tip)}">i</span>'


def label(text: str, tip: str = "") -> str:
    return f'<div class="lab">{esc(text)}{info(tip) if tip else ""}</div>'


def row(text: str, control: str, tip: str = "", hint: str = "",
        when: str = "", wide: bool = False) -> str:
    cls = "fr wide" if wide else "fr"
    w = f' data-when="{esc(when)}"' if when else ""
    h = f'<div class="hint">{esc(hint)}</div>' if hint else ""
    return f'<div class="{cls}"{w}><div>{label(text, tip)}{h}</div>{control}</div>'


def toggle(name: str, on: bool) -> str:
    checked = " checked" if on else ""
    return (f'<label class="sw"><input type="checkbox" name="{esc(name)}"{checked}>'
            f'<span></span></label>')


def text_in(name: str, value: str, placeholder: str = "", kind: str = "text",
            listid: str = "") -> str:
    l = f' list="{listid}"' if listid else ""
    return (f'<input class="ctl" type="{kind}" name="{esc(name)}" '
            f'value="{esc(value)}" placeholder="{esc(placeholder)}"{l}>')


def num_in(name: str, value, step: str = "1") -> str:
    """step="1" collects as an integer; anything else as a float.

    Marked on the element rather than inferred in JS, because parseInt on a
    temperature of 0.7 quietly stores 0 and makes the model deterministic.
    """
    kind = "int" if step == "1" else "float"
    v = int(value) if kind == "int" else value
    return (f'<input class="ctl" type="number" step="{step}" data-num="{kind}" '
            f'name="{esc(name)}" value="{v}">')


def csv_in(name: str, values, placeholder: str = "") -> str:
    return (f'<input class="ctl" data-list="csv" name="{esc(name)}" '
            f'value="{esc(", ".join(values))}" placeholder="{esc(placeholder)}">')


def area(name: str, value: str, placeholder: str = "", rows: int = 4,
         data_list: str = "") -> str:
    d = f' data-list="{data_list}"' if data_list else ""
    return (f'<textarea class="ctl" name="{esc(name)}" rows="{rows}"'
            f'{d} placeholder="{esc(placeholder)}">{esc(value)}</textarea>')


def select(name: str, current: str, options) -> str:
    opts = "".join(
        f'<option value="{esc(v)}"{" selected" if current == v else ""}>{esc(lab)}</option>'
        for v, lab in options)
    return f'<select class="ctl" name="{esc(name)}">{opts}</select>'


def picker(name: str, jids, kind: str) -> str:
    """A chip list plus a button that opens the searchable chooser.

    The value lives in a hidden input so the collector treats it like any other
    field. `kind` decides whether the chooser lists people or groups.
    """
    return (f'<div class="pick" data-kind="{kind}">'
            f'<input type="hidden" data-list="jids" name="{esc(name)}" '
            f'value="{esc(",".join(jids))}">'
            f'<div class="chips"></div>'
            f'<button type="button" class="ghost sm choose">Choose…</button></div>')


def section(title: str, body: str, when: str = "", note: str = "") -> str:
    w = f' data-when="{esc(when)}"' if when else ""
    n = f'<p class="note">{esc(note)}</p>' if note else ""
    return f'<section class="card"{w}><h2>{esc(title)}</h2>{n}{body}</section>'


# ---------------------------------------------------------------------- css

CSS = """
*{box-sizing:border-box}
body{margin:0;font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
     background:#0b141a;color:#e9edef}
a{color:#53bdeb;text-decoration:none}
.wrap{max-width:760px;margin:0 auto;padding:26px 20px 90px}
h1{font-size:22px;margin:14px 0 4px;display:flex;align-items:center;gap:10px}
h2{font-size:13px;text-transform:uppercase;letter-spacing:.08em;color:#00a884;
   margin:0 0 4px;font-weight:600}
.note{color:#8696a0;font-size:12.5px;margin:0 0 10px}
.card{background:#111b21;border:1px solid #222d34;border-radius:12px;
      padding:16px 18px;margin-top:14px}
.card.hide,.fr.hide{display:none}

/* One grid for every row, so labels and controls line up down the whole page
   instead of each block choosing its own width. */
.fr{display:grid;grid-template-columns:1fr 280px;gap:16px;align-items:center;
    padding:11px 0;border-bottom:1px solid #1b262d}
.fr:last-child{border-bottom:0;padding-bottom:0}
.fr.wide{grid-template-columns:1fr}
.fr.wide .ctl{margin-top:7px}
.lab{display:flex;align-items:center;gap:7px;font-size:14px}
.hint{color:#8696a0;font-size:12.5px;margin-top:2px;max-width:52ch}
.ctl{width:100%;background:#2a3942;border:1px solid #2a3942;border-radius:8px;
     padding:8px 11px;color:#e9edef;font:inherit;font-size:14px;outline:none}
.ctl:focus{border-color:#00a884}
textarea.ctl{resize:vertical;font-size:13.5px;line-height:1.5}
select.ctl{cursor:pointer}
.sw{justify-self:end}

/* toggle */
.sw{position:relative;display:inline-block;width:44px;height:25px;flex:none}
.sw input{position:absolute;opacity:0;width:100%;height:100%;margin:0;cursor:pointer;z-index:1}
.sw span{position:absolute;inset:0;background:#3b4a54;border-radius:25px;transition:.15s}
.sw span::before{content:"";position:absolute;width:19px;height:19px;left:3px;top:3px;
  background:#fff;border-radius:50%;transition:.15s}
.sw input:checked+span{background:#00a884}
.sw input:checked+span::before{transform:translateX(19px)}
.sw input:focus-visible+span{outline:2px solid #53bdeb;outline-offset:2px}

/* info bubble */
.i{position:relative;display:inline-grid;place-items:center;width:16px;height:16px;
   border-radius:50%;border:1px solid #52646f;color:#8696a0;font-size:10px;
   font-weight:700;font-style:italic;cursor:help;flex:none;user-select:none}
.i:hover,.i:focus{border-color:#00a884;color:#00a884;outline:none}
.i::after{content:attr(data-tip);position:absolute;left:24px;top:-8px;width:290px;
  padding:10px 12px;background:#233138;color:#e9edef;border:1px solid #3b4a54;
  border-radius:8px;font:400 12.5px/1.5 inherit;font-style:normal;white-space:pre-wrap;
  opacity:0;visibility:hidden;transition:.12s;z-index:20;text-align:left;
  box-shadow:0 10px 30px #000a;pointer-events:none}
.i:hover::after,.i:focus::after{opacity:1;visibility:visible}
@media(max-width:620px){.i::after{left:auto;right:0;top:24px;width:min(290px,80vw)}}

/* chips + picker */
.chips{display:flex;flex-wrap:wrap;gap:6px}
.chips:not(:empty){margin-bottom:8px}
.chip{background:#2a3942;border-radius:14px;padding:3px 6px 3px 10px;font-size:12.5px;
      display:inline-flex;align-items:center;gap:6px}
.chip b{cursor:pointer;color:#8696a0;font-weight:400;font-size:14px;line-height:1}
.chip b:hover{color:#f15c6d}
button{background:#00a884;color:#111b21;border:0;border-radius:8px;padding:10px 18px;
       font:inherit;font-weight:600;cursor:pointer}
button.ghost{background:transparent;color:#e9edef;border:1px solid #3b4a54}
button.sm{padding:6px 12px;font-size:13px;font-weight:500}
.pick{justify-self:stretch}

/* save bar */
.bar{position:fixed;left:0;right:0;bottom:0;background:#111b21e8;backdrop-filter:blur(8px);
     border-top:1px solid #222d34;padding:12px 20px;display:flex;gap:10px;
     align-items:center;justify-content:center;z-index:30}
#out{font-size:13px;color:#8696a0;white-space:pre-wrap;max-height:64px;overflow:auto}
#out.bad{color:#f15c6d}#out.good{color:#00a884}
.pill{font-size:12px;padding:2px 10px;border-radius:11px;background:#2a3942;color:#8696a0;
      font-weight:500}
.pill.on{background:#00a884;color:#111b21}

/* chooser modal */
.mask{position:fixed;inset:0;background:#000b;display:none;place-items:center;z-index:60}
.mask.on{display:grid}
.modal{background:#111b21;border:1px solid #2a3942;border-radius:12px;
       width:min(520px,92vw);max-height:78vh;display:flex;flex-direction:column}
.modal header{padding:14px 16px;border-bottom:1px solid #222d34}
.modal .body{overflow-y:auto;flex:1}
.modal footer{padding:12px 16px;border-top:1px solid #222d34;display:flex;
              justify-content:space-between;align-items:center;gap:10px}
.opt{display:flex;align-items:center;gap:11px;padding:9px 16px;cursor:pointer}
.opt:hover{background:#182229}
.opt input{width:17px;height:17px;accent-color:#00a884;flex:none}
.opt .nm{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.count{color:#8696a0;font-size:13px}
"""


# --------------------------------------------------------------- the page

def build(rt, q: str, status: dict) -> str:
    """Render the whole settings document."""
    t = rt.trigger.settings
    ar = status["auto_reply"]

    state = ('<span class="pill on">active</span>' if ar["active"]
             else f'<span class="pill">idle — {esc(ar["reason"] or "off")}</span>')

    # --- backend ---------------------------------------------------------
    backend = section("Auto-reply", (
        row("Reply automatically", toggle("enabled", t.enabled),
            "Master switch. Off, nothing is ever sent on your behalf — but the "
            "watch rules further down still notify you.",
            "Everything below applies only while this is on.")
        + row("Answer using", select("backend", t.backend,
                                     [("model", "A language model"),
                                      ("webhook", "My own webhook")]),
              "Model: this server calls an OpenAI-compatible endpoint and sends "
              "the reply.\n\nWebhook: this server POSTs to a URL you control and "
              "sends back whatever it returns. Use this when the logic lives in "
              "your own app.", when="enabled")
    ), note="Replies go from your real number. Bulk or unsolicited messages can "
            "get it banned.")

    # --- model -----------------------------------------------------------
    presets = ("https://openrouter.ai/api/v1", "https://api.openai.com/v1",
               "https://api.groq.com/openai/v1", "https://api.together.xyz/v1",
               "http://localhost:11434/v1", "http://localhost:1234/v1")
    model = section("Model", (
        row("Base URL", text_in("model.base_url", t.model.base_url,
                                "https://openrouter.ai/api/v1", listid="presets"),
            "Any endpoint that speaks the OpenAI chat-completions format — "
            "OpenRouter, OpenAI, Groq, Together, or Ollama and LM Studio on "
            "localhost.\n\nEnd it at /v1. Pasting the full .../chat/completions "
            "URL your provider documents also works — that tail is trimmed "
            "rather than appended twice.")
        + '<datalist id="presets">'
        + "".join(f'<option value="{esc(p)}">' for p in presets) + "</datalist>"
        + row("API key", text_in("model.api_key", "***" if t.model.api_key else "",
                                 "sk-…", kind="password"),
              "Stored in your own database. Leave the dots untouched to keep the "
              "existing key; clear the field to remove it.")
        + row("Model", text_in("model.model", t.model.model,
                               "anthropic/claude-sonnet-4.5"),
              "Exactly as your provider names it.")
        + row("System prompt", area("model.system_prompt", t.model.system_prompt,
                                    rows=5),
              "Persona and tone. The same text is sent whichever backend you "
              "pick.\n\nHow the reply gets delivered is added automatically and "
              "is not yours to set here — it differs between waiting for a "
              "reply and handing the message over, and the two are opposites.\n\n"
              + TOKEN_HELP, wide=True)
        + row("Turns of history to send", num_in("model.history_messages",
                                                 t.model.history_messages),
              "How much of the conversation the model sees. More context costs "
              "more per reply and, past a point, buys nothing.")
        + row("Reply length cap (tokens)", num_in("model.max_tokens", t.model.max_tokens),
              "A hard ceiling on what the model may generate.")
        + row("Temperature", num_in("model.temperature", t.model.temperature, "0.1"),
              "How much the wording varies between runs. 0 is repeatable and "
              "flat; above about 1 it starts to wander. 0.7 is a reasonable "
              "middle for conversation.")
        + row("Give up after (seconds)", num_in("model.timeout_seconds",
                                                t.model.timeout_seconds, "0.5"),
              "A reply that takes longer than this is abandoned. WhatsApp is a "
              "live conversation — a late answer reads worse than none.")
    ), when="enabled&backend=model")

    # --- webhook ---------------------------------------------------------
    hdrs = "\n".join(f"{k}: {v}" for k, v in (t.webhook.headers or {}).items())
    webhook = section("Webhook", (
        row("URL", text_in("webhook.url", t.webhook.url, "https://your.app/hook"),
            "This server POSTs here and sends back what you return.")
        + row("Method", select("webhook.method", t.webhook.method,
                               [("POST", "POST"), ("PUT", "PUT")]), "")
        + row("Headers", area("webhook.headers", hdrs, "Authorization: Bearer …",
                              rows=3, data_list="headers"),
              "One per line, as Name: value. Tags work here too — handy for "
              "passing the chat as an id.", wide=True)
        + row("Body", area("webhook.body", t.webhook.body, rows=4),
              TOKEN_HELP + "\n\nA JSON body is escaped for you, so a message "
              "containing a quote cannot break it.", wide=True)
        + row("Wait for the reply", toggle("webhook.expect_reply",
                                          t.webhook.expect_reply),
              "On: this server waits for your response and sends whatever "
              "comes back, so your endpoint has to answer within the timeout.\n\n"
              "Off: the message is handed over and nothing more happens here. "
              "The prompt changes to match — it names the chat and tells the "
              "agent to send the reply itself through its own WhatsApp "
              "connector, because nothing it returns is delivered. This is what "
              "anything queued, human-approved, or slower than one request "
              "needs.")
        + row("Where the reply is in the response",
              text_in("webhook.reply_path", t.webhook.reply_path, "reply"),
              "A dotted path into the JSON you return — reply, or "
              "content.0.text for Anthropic, or choices.0.message.content for "
              "an OpenAI-shaped one. Leave blank if you return plain text.",
              when="webhook.expect_reply")
        + row("Reply token lifetime (seconds)",
              num_in("webhook.token_ttl_seconds", t.webhook.token_ttl_seconds),
              "When you hand the message over, the payload carries a token "
              "minted for that one delivery — it can send in that one "
              "conversation and nothing else, and it expires.\n\nThat is what "
              "makes handing off safe: whatever a message talks your agent "
              "into, reading other chats or messaging another number is not "
              "something it has to refuse, it is not available.\n\nUse "
              "{{reply_token}} in the body or a header. Keep it short; long "
              "enough for a queue or a person to approve the reply.",
              when="webhook.expect_reply=false")
        + row("Turns of history to send",
              num_in("webhook.history_messages", t.webhook.history_messages), "")
        + row("Give up after (seconds)", num_in("webhook.timeout_seconds",
                                                t.webhook.timeout_seconds, "0.5"),
              "Your endpoint gets this long to answer before the attempt is "
              "abandoned.")
    ), when="enabled&backend=webhook")

    # --- guardrails ------------------------------------------------------
    g = t.guardrails
    guards = section("Guardrails", (
        row("Answer only from this conversation", toggle("guardrails.context_only",
                                                         g.context_only),
            "On, the model works from the chat history alone and says it does "
            "not know rather than guessing. Off, it will invent prices, dates "
            "and order numbers that sound entirely plausible.")
        + row("Allow outside knowledge", toggle("guardrails.allow_external_knowledge",
                                                g.allow_external_knowledge),
              "The deliberate escape hatch. When on, the model is told in words "
              "that it may draw on what it knows beyond this chat.")
        + row("Only answer about", csv_in("guardrails.allowed_topics",
                                          g.allowed_topics,
                                          "orders, delivery, opening hours"),
              "Comma separated. Leave empty to allow any subject.", wide=True)
        + row("Refuse anything off-topic",
              toggle("guardrails.require_allowed_topic", g.require_allowed_topic),
              "Only meaningful with a topic list above. Strict: a message that "
              "mentions none of them is refused before the model runs.")
        + row("Never discuss", csv_in("guardrails.blocked_topics", g.blocked_topics,
                                      "pricing negotiation, legal advice"),
              "Comma separated. Passed to the model as instructions.", wide=True)
        + row("Blocked words", csv_in("guardrails.blocked_keywords",
                                      g.blocked_keywords, "refund, chargeback"),
              "Checked in code before the model is called, so these cost nothing "
              "and cannot be talked around.", wide=True)
        + row("House rules", area("guardrails.policy_note", g.policy_note,
                                  "Be brief and formal. Never promise a date.",
                                  rows=3),
              "Added to the prompt verbatim.", wide=True)
    ), when="enabled")

    # --- default message -------------------------------------------------
    fallback = section("Default message", (
        row("What to send", text_in("guardrails.fallback_message",
                                    g.fallback_message),
            "Sent in place of an answer when a reply is refused or fails. "
            "Keep it something a stranger can act on — it is the only thing "
            "they will see.", wide=True)
        + row("Use it when a guardrail refuses",
              toggle("guardrails.send_fallback_when_blocked",
                     g.send_fallback_when_blocked),
              "Off, a blocked message gets silence instead.")
        + row("Use it when the backend fails",
              toggle("guardrails.send_fallback_on_error", g.send_fallback_on_error),
              "Off, an outage is invisible to the other person — usually better "
              "than apologising for something they did not see break.")
    ), when="enabled")

    # --- scope -----------------------------------------------------------
    r = t.reply
    scope = section("Who gets replies", (
        row("Direct messages", select("reply.personal", r.personal,
                                      [("none", "Nobody"), ("all", "Everyone"),
                                       ("allowlist", "Only chosen people")]),
            "Start with a short allowlist. Everyone means every stranger who "
            "messages you gets an automated answer.")
        + row("Chosen people", picker("reply.personal_allowlist",
                                      r.personal_allowlist, "direct"),
              "", when="reply.personal=allowlist", wide=True)
        + row("Groups", select("reply.groups", r.groups,
                               [("none", "Nobody"), ("all", "Every group"),
                                ("allowlist", "Only chosen groups")]),
              "Groups are noisy and a wrong reply is seen by everyone in them.")
        + row("Chosen groups", picker("reply.groups_allowlist",
                                      r.groups_allowlist, "groups"),
              "", when="reply.groups=allowlist", wide=True)
        + row("In groups, only when mentioned",
              toggle("reply.require_mention_in_groups", r.require_mention_in_groups),
              "Strongly recommended. Off, it answers every message in the group.")
        + row("Cooldown per chat (seconds)", num_in("reply.cooldown_seconds",
                                                    r.cooldown_seconds),
              "The shortest gap between two automated replies in the same chat. "
              "It stops a burst of messages from producing a burst of answers, "
              "and it is what breaks a loop if the other end is also a bot. "
              "30 is a sensible floor.")
        + row("Max replies per hour", num_in("reply.max_replies_per_hour",
                                             r.max_replies_per_hour),
              "A ceiling across every chat combined, counted over a rolling "
              "hour. This is the circuit breaker: if something goes wrong, it "
              "caps how much damage is done to your number before you notice. "
              "Once reached, nothing is sent until the hour rolls on.")
        + row("Longest reply (characters)", num_in("reply.max_reply_chars",
                                                   r.max_reply_chars),
              "Anything longer is truncated. WhatsApp is not the place for an "
              "essay.")
        + row("Show typing first", toggle("show_typing", t.show_typing),
              "Sends a typing indicator before the reply.")
    ), when="enabled")

    # --- media -----------------------------------------------------------
    media = section("Media", (
        row("Send files the model links", toggle("send_media", t.send_media),
            "When a reply contains a link to a picture, video, voice note or "
            "document, download it and send it as a real attachment instead of "
            "a link the other person has to tap. Anything unrecognised is sent "
            "as a document.")
        + row("Largest file (bytes)", num_in("max_media_bytes", t.max_media_bytes),
              "Refused above this. The URL comes from a model, so it cannot be "
              "trusted to be small.")
    ), when="enabled")

    # --- notify ----------------------------------------------------------
    n = t.notify
    notify = section("Tell me when", (
        row("Send alerts to", select("notify.route", n.route,
                                     [("off", "Nowhere — alerts off"),
                                      ("me", "My own number"),
                                      ("chat", "Back into the same chat"),
                                      ("number", "Another number")]),
            "My own number puts alerts in your Message-yourself chat, which is "
            "usually what you want on a personal number.\n\nBack into the same "
            "chat means the person who messaged you SEES the alert — it reads "
            "\"Needs you: … Their message: … Reason: …\". Only pick it if that "
            "is genuinely what you want.\n\nAnother number is for a business "
            "line, where the number customers write to is not the one you read.")
        + row("Which number", text_in("notify.jid", n.jid, "919812345678"),
              "With the country code and no +, e.g. 919812345678.",
              when="notify.route=number")
        + row("Alert me when a message contains",
              csv_in("notify.on_keywords", n.on_keywords, "urgent, complaint, cancel"),
              "Comma separated, matched case-insensitively.", wide=True)
        + row("Always alert me about", picker("notify.vip_contacts",
                                              n.vip_contacts, "direct"),
              "These people get through regardless of keywords.", wide=True)
        + row("Watch groups too", toggle("notify.watch_groups", n.watch_groups), "")
        + row("When the assistant asks for a human",
              toggle("notify.on_handoff", n.on_handoff),
              "The model can emit the marker below to escalate.",
              when="enabled")
        + row("When a guardrail refuses", toggle("notify.on_blocked", n.on_blocked),
              "A guardrail is only evaluated while auto-reply is running.",
              when="enabled")
        + row("When the backend fails", toggle("notify.on_error", n.on_error),
              "There is no backend to fail unless auto-reply is on.",
              when="enabled")
        + row("Hand-off marker", text_in("notify.handoff_marker", n.handoff_marker),
              "Stripped from the reply before it is sent.", when="enabled")
        + row("Alert wording", area("notify.template", n.template, rows=4),
              TOKEN_HELP + "\n\n{{reason}} is why the alert fired, and is only "
              "meaningful here.", wide=True)
    ), note="Keyword and contact watching runs with auto-reply switched off, "
            "which is the useful case: watch a number without answering on it. "
            "The alerts about replies appear only once auto-reply is on.")

    body = (f'<div class="wrap"><a href="/{q}">&larr; Back to chats</a>'
            f'<h1>Settings {state}</h1>'
            f'<form id="f">{backend}{model}{webhook}{guards}{fallback}{scope}{media}{notify}</form>'
            f'</div>'
            f'<div class="bar"><button type="button" id="save">Save</button>'
            f'<div id="out"></div></div>'
            f'<div class="mask" id="mask"><div class="modal">'
            f'<header><input class="ctl" id="q" placeholder="Search name or number…" '
            f'autocomplete="off"></header>'
            f'<div class="body" id="opts"></div>'
            f'<footer><span class="count" id="cnt"></span><span>'
            f'<button type="button" class="ghost sm" id="cancel">Cancel</button> '
            f'<button type="button" class="sm" id="done">Add selected</button>'
            f'</span></footer></div></div>')

    return ('<!doctype html><meta charset="utf-8"><title>Settings</title>'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            f"<style>{CSS}</style>{body}"
            f"<script>const Q={json.dumps(q)};{JS}</script>")


# ----------------------------------------------------------------- script

JS = r"""
const $ = s => document.querySelector(s);
const out = $("#out");
function show(msg, good) {
  out.textContent = msg;
  out.className = good === undefined ? "" : (good ? "good" : "bad");
}

/* ---- progressive disclosure ---------------------------------------- */
/* data-when="enabled" shows while that checkbox is on; data-when="a=b" while
   that control equals b. Join with & to require both -- the model and webhook
   blocks are "enabled&backend=model", because with auto-reply off there is no
   backend in play and neither block should be on screen. Rows and whole
   sections use the same rule. */
function sync() {
  for (const el of document.querySelectorAll("[data-when]")) {
    const on = el.dataset.when.split("&").every(cond => {
      const [name, want] = cond.split("=");
      const src = document.querySelector(`[name="${name}"]`);
      if (!src) return true;
      // "name" shows while a switch is on; "name=false" while it is off.
      // Without the negative case a row that belongs to the OFF state renders
      // exactly when it does not apply.
      if (src.type === "checkbox")
        return want === "false" ? !src.checked : src.checked;
      return src.value === want;
    });
    el.classList.toggle("hide", !on);
  }
}
document.addEventListener("change", sync);
sync();

/* ---- collecting the form ------------------------------------------- */
/* Walks the dotted name and creates each parent on the way down. The old
   version pre-declared three parents and threw on anything else, which is
   why Save silently did nothing for every guardrail and notify field. */
function collect() {
  const o = {};
  for (const el of document.querySelectorAll("#f [name]")) {
    let v;
    if (el.type === "checkbox") v = el.checked;
    else if (el.type === "number")
      v = el.dataset.num === "float" ? (parseFloat(el.value) || 0)
                                     : (parseInt(el.value || "0", 10) || 0);
    else if (el.dataset.list === "csv")
      v = el.value.split(",").map(x => x.trim()).filter(Boolean);
    else if (el.dataset.list === "jids")
      v = el.value.split(",").map(x => x.trim()).filter(Boolean);
    else if (el.dataset.list === "headers") {
      v = {};
      for (const line of el.value.split("\n")) {
        const i = line.indexOf(":");
        if (i > 0) v[line.slice(0, i).trim()] = line.slice(i + 1).trim();
      }
    } else v = el.value;

    const parts = el.name.split(".");
    let node = o;
    while (parts.length > 1) {
      const k = parts.shift();
      node = (node[k] ??= {});
    }
    node[parts[0]] = v;
  }
  return o;
}

$("#save").onclick = async () => {
  show("Saving…");
  try {
    const r = await fetch("/api/settings" + Q, {method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify(collect())});
    const d = await r.json();
    if (!d.ok) return show("Error: " + (d.error || r.status), false);
    show(d.would_fire ? "Saved. Replies are live."
                      : "Saved, but not replying yet: "
                        + (d.blocked_by || "reason unavailable"), true);
  } catch (e) { show("Save failed: " + e.message, false); }
};

/* ---- the contact chooser -------------------------------------------- */
const names = {};           /* jid -> display name, learned as we go */
let active = null;          /* the .pick being edited */
let chosen = new Set();

function chipsFor(pick) {
  const input = pick.querySelector("input");
  const jids = input.value.split(",").filter(Boolean);
  pick.querySelector(".chips").innerHTML = jids.map(j =>
    `<span class="chip">${esc(names[j] || j)}<b data-j="${esc(j)}">&times;</b></span>`
  ).join("");
}
const esc = s => String(s).replace(/[&<>"]/g, c =>
  ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;"}[c]));

document.addEventListener("click", e => {
  const x = e.target.closest(".chip b");
  if (x) {
    const pick = x.closest(".pick"), input = pick.querySelector("input");
    input.value = input.value.split(",").filter(j => j && j !== x.dataset.j).join(",");
    chipsFor(pick);
    return;
  }
  if (e.target.closest(".choose")) {
    active = e.target.closest(".pick");
    chosen = new Set(active.querySelector("input").value.split(",").filter(Boolean));
    $("#q").value = "";
    $("#mask").classList.add("on");
    $("#q").focus();
    load("");
  }
});

$("#cancel").onclick = () => $("#mask").classList.remove("on");
$("#mask").onclick = e => { if (e.target === $("#mask")) $("#cancel").click(); };
$("#done").onclick = () => {
  const input = active.querySelector("input");
  input.value = [...chosen].join(",");
  chipsFor(active);
  $("#mask").classList.remove("on");
};

let timer = null;
$("#q").oninput = () => { clearTimeout(timer); timer = setTimeout(() => load($("#q").value), 180); };

/* Search runs against the store, not against whatever this page happened to
   render, so a contact far down a long list is still findable. */
async function load(query) {
  const kind = active.dataset.kind;
  // The endpoint names these `filter` and `q`, not kind/query.
  const url = `/api/chats${Q}${Q ? "&" : "?"}filter=${kind}&limit=200`
            + (query ? "&q=" + encodeURIComponent(query) : "");
  let chats = [];
  try { chats = (await (await fetch(url)).json()).chats || []; } catch (e) {}
  for (const c of chats) names[c.chat_jid] = c.name;
  $("#opts").innerHTML = chats.map(c => `
    <label class="opt">
      <input type="checkbox" value="${esc(c.chat_jid)}"
             ${chosen.has(c.chat_jid) ? "checked" : ""}>
      <span class="nm">${esc(c.name)}</span>
    </label>`).join("") || '<div class="opt">Nothing matches.</div>';
  count();
}

$("#opts").onchange = e => {
  if (e.target.type !== "checkbox") return;
  e.target.checked ? chosen.add(e.target.value) : chosen.delete(e.target.value);
  count();
};
function count() { $("#cnt").textContent = chosen.size + " selected"; }

/* Names for anything already saved. One request, not one per chip. */
(async () => {
  try {
    const d = await (await fetch(`/api/chats${Q}${Q ? "&" : "?"}limit=500`)).json();
    for (const c of d.chats || []) names[c.chat_jid] = c.name;
  } catch (e) {}
  document.querySelectorAll(".pick").forEach(chipsFor);
})();
"""
