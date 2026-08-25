"""Message parsing — text, type, quotes, media, edits and revokes.

Field names here are camelCase (`extendedTextMessage`, not `extended_text_message`).
That is not a style choice: neonize's waE2E .proto declares camelCase field names, so
the generated Python attributes are camelCase and snake_case raises AttributeError.
Verified against neonize 0.4.3 — `Message.DESCRIPTOR.fields` lists 107 camelCase names.

Two subtleties that make naive parsing wrong:

1. Reading `msg.conversation or msg.extendedTextMessage.text` does not raise on an
   image — protobuf returns a default sub-message, so you silently get "" and lose
   every caption, voice note, document and quoted reply.
2. Real payloads arrive wrapped. An ephemeral (disappearing) image is
   ephemeralMessage.message.imageMessage; a view-once is viewOnceMessageV2.message.*;
   your own sent messages arrive as deviceSentMessage.message.*. Parsing the outer
   layer finds nothing at all. So unwrap first, always.
"""
from __future__ import annotations

from typing import Any

# Wrapper messages whose real payload sits in a nested `message` field. Detected
# structurally rather than by a hardcoded list, so a neonize upgrade that adds a
# new wrapper keeps working.
_MAX_UNWRAP_DEPTH = 6

# field name -> attribute holding its human-readable text
_TEXT_FIELDS: dict[str, str] = {
    "extendedTextMessage": "text",
    "imageMessage": "caption",
    "videoMessage": "caption",
    "documentMessage": "caption",
    "ptvMessage": "caption",
    "liveLocationMessage": "caption",
    "reactionMessage": "text",
    "buttonsResponseMessage": "selectedDisplayText",
    "templateButtonReplyMessage": "selectedDisplayText",
    "listResponseMessage": "title",
    "contactMessage": "displayName",
    "contactsArrayMessage": "displayName",
    "eventMessage": "name",
    "pollCreationMessage": "name",
    "pollCreationMessageV2": "name",
    "pollCreationMessageV3": "name",
    "pollCreationMessageV4": "name",
    "pollCreationMessageV5": "name",
    "questionMessage": "text",
}

# field name -> canonical type stored in wa_messages.type
_TYPE_MAP: dict[str, str] = {
    "conversation": "text",
    "extendedTextMessage": "text",
    "imageMessage": "image",
    "videoMessage": "video",
    "ptvMessage": "video_note",
    "audioMessage": "audio",
    "documentMessage": "document",
    "stickerMessage": "sticker",
    "lottieStickerMessage": "sticker",
    "locationMessage": "location",
    "liveLocationMessage": "live_location",
    "contactMessage": "contact",
    "contactsArrayMessage": "contact",
    "reactionMessage": "reaction",
    "pollCreationMessage": "poll",
    "pollCreationMessageV2": "poll",
    "pollCreationMessageV3": "poll",
    "pollUpdateMessage": "poll_vote",
    "eventMessage": "event",
    "albumMessage": "album",
    "orderMessage": "order",
    "productMessage": "product",
    "groupInviteMessage": "group_invite",
    "buttonsResponseMessage": "button_reply",
    "templateButtonReplyMessage": "button_reply",
    "listResponseMessage": "list_reply",
    "interactiveResponseMessage": "interactive_reply",
}

MEDIA_FIELDS = (
    "imageMessage",
    "videoMessage",
    "audioMessage",
    "documentMessage",
    "stickerMessage",
    "ptvMessage",
)


def unwrap(message: Any) -> Any:
    """Peel container messages until the payload is reached.

    Handles ephemeral / view-once / device-sent / edited / document-with-caption
    without naming them, by looking for a singular sub-message field literally
    called `message`.
    """
    current = message
    for _ in range(_MAX_UNWRAP_DEPTH):
        set_fields = [f.name for f, _ in current.ListFields()]
        # A wrapper carries exactly one payload field; anything with real content
        # alongside is already the payload.
        wrapper = None
        for name in set_fields:
            if name in ("messageContextInfo",):
                continue
            try:
                inner = getattr(current, name)
                if hasattr(inner, "message") and inner.HasField("message"):
                    wrapper = inner
                    break
            except (AttributeError, ValueError):
                continue
        if wrapper is None:
            return current
        current = wrapper.message
    return current


def content_field(message: Any) -> str | None:
    """Name of the field carrying the actual content, after unwrapping."""
    inner = unwrap(message)
    for field, _ in inner.ListFields():
        if field.name in ("messageContextInfo", "senderKeyDistributionMessage"):
            continue
        return field.name
    return None


def extract_text(message: Any) -> str:
    """Best-effort human-readable text for any message variant.

    Returns "" rather than None so the column is consistently searchable — the
    tsvector index treats "" and NULL differently and we do not want both.
    """
    inner = unwrap(message)

    if inner.HasField("conversation") if _has(inner, "conversation") else False:
        return inner.conversation or ""
    # `conversation` is a scalar string field, so HasField is unavailable on some
    # protobuf builds; fall back to a truthiness check.
    if getattr(inner, "conversation", ""):
        return inner.conversation

    for field, attr in _TEXT_FIELDS.items():
        if _has(inner, field) and inner.HasField(field):
            value = getattr(getattr(inner, field), attr, "") or ""
            if value:
                return value

    # Documents often carry no caption but always carry a filename, which is the
    # only searchable text a user would recognise.
    if _has(inner, "documentMessage") and inner.HasField("documentMessage"):
        return inner.documentMessage.fileName or ""

    if _has(inner, "locationMessage") and inner.HasField("locationMessage"):
        loc = inner.locationMessage
        return (loc.name or loc.address or "").strip()

    return ""


def message_type(message: Any) -> str:
    """Canonical type string for wa_messages.type."""
    inner = unwrap(message)

    # protocolMessage carries edits and revokes rather than content.
    if _has(inner, "protocolMessage") and inner.HasField("protocolMessage"):
        proto = inner.protocolMessage
        if proto.HasField("editedMessage"):
            return "edit"
        type_name = _enum_name(proto, "type")
        if type_name and "REVOKE" in type_name:
            return "revoke"
        return "system"

    if getattr(inner, "conversation", ""):
        return "text"

    field = content_field(inner)
    if field is None:
        return "unknown"
    return _TYPE_MAP.get(field, "unknown")


def is_content_message(message: Any) -> bool:
    """Does this carry something a user would recognise as a message?

    WhatsApp delivers protocol artifacts through the same MessageEv as real
    messages. Two kinds show up constantly and must not become wa_messages rows:

      * senderKeyDistributionMessage — group key material, sent whenever a group's
        sender key rotates. Observed in live traffic bumping a group's unread count
        with an empty "unknown" message.
      * protocolMessage — app-state sync, receipts, pairing artifacts. Edits and
        revokes also arrive this way, but those mutate an existing row rather than
        creating one, so they are excluded here too.

    These are still FORWARDED as events; the trigger service may care. They just do
    not belong in message history, which is the LLM's context window.
    """
    inner = unwrap(message)
    field = content_field(inner)
    if field is None:
        return False
    if field in ("senderKeyDistributionMessage", "protocolMessage"):
        return False
    return True


def quoted_message_id(message: Any) -> str | None:
    """The message this one replies to, if any.

    Lives in contextInfo.stanzaID, which hangs off whichever content field is set —
    there is no single top-level place to read it from.
    """
    inner = unwrap(message)
    for field, _ in inner.ListFields():
        value = getattr(inner, field.name, None)
        ctx = getattr(value, "contextInfo", None)
        if ctx is not None and getattr(ctx, "stanzaID", ""):
            return ctx.stanzaID
    return None


def revoked_message_id(message: Any) -> str | None:
    """For a revoke, the id of the message being deleted."""
    inner = unwrap(message)
    if _has(inner, "protocolMessage") and inner.HasField("protocolMessage"):
        proto = inner.protocolMessage
        type_name = _enum_name(proto, "type")
        if type_name and "REVOKE" in type_name and proto.HasField("key"):
            return proto.key.ID or None
    return None


def edited_payload(message: Any) -> tuple[str, str] | None:
    """For an edit, returns (target_message_id, new_text)."""
    inner = unwrap(message)
    if not (_has(inner, "protocolMessage") and inner.HasField("protocolMessage")):
        return None
    proto = inner.protocolMessage
    if not proto.HasField("editedMessage"):
        return None
    target = proto.key.ID if proto.HasField("key") else ""
    return target, extract_text(proto.editedMessage)


def media_descriptor(message: Any) -> dict | None:
    """Everything needed to download and store the attachment, or None."""
    inner = unwrap(message)
    for field in MEDIA_FIELDS:
        if _has(inner, field) and inner.HasField(field):
            media = getattr(inner, field)
            return {
                "field": field,
                "kind": _TYPE_MAP.get(field, "document"),
                "mimetype": getattr(media, "mimetype", "") or "",
                "file_length": int(getattr(media, "fileLength", 0) or 0),
                "file_name": getattr(media, "fileName", "") or "",
                "sha256": bytes(getattr(media, "fileSHA256", b"") or b""),
                "seconds": int(getattr(media, "seconds", 0) or 0),
            }
    return None


def _has(message: Any, field: str) -> bool:
    """Does this protobuf build actually declare the field?

    Guards against neonize/proto version drift — a field we reference disappearing
    should degrade to "not present", not crash the whole ingest path.
    """
    try:
        return field in message.DESCRIPTOR.fields_by_name
    except AttributeError:
        return False


def _enum_name(message: Any, field: str) -> str | None:
    try:
        descriptor = message.DESCRIPTOR.fields_by_name[field]
        return descriptor.enum_type.values_by_number[getattr(message, field)].name
    except (AttributeError, KeyError, TypeError):
        return None
