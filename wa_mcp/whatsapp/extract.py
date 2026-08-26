from __future__ import annotations

from typing import Any

_MAX_UNWRAP_DEPTH = 6

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
    current = message
    for _ in range(_MAX_UNWRAP_DEPTH):
        set_fields = [f.name for f, _ in current.ListFields()]
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
    inner = unwrap(message)
    for field, _ in inner.ListFields():
        if field.name in ("messageContextInfo", "senderKeyDistributionMessage"):
            continue
        return field.name
    return None


def extract_text(message: Any) -> str:
    inner = unwrap(message)

    if inner.HasField("conversation") if _has(inner, "conversation") else False:
        return inner.conversation or ""
    if getattr(inner, "conversation", ""):
        return inner.conversation

    for field, attr in _TEXT_FIELDS.items():
        if _has(inner, field) and inner.HasField(field):
            value = getattr(getattr(inner, field), attr, "") or ""
            if value:
                return value

    if _has(inner, "documentMessage") and inner.HasField("documentMessage"):
        return inner.documentMessage.fileName or ""

    if _has(inner, "locationMessage") and inner.HasField("locationMessage"):
        loc = inner.locationMessage
        return (loc.name or loc.address or "").strip()

    return ""


def message_type(message: Any) -> str:
    inner = unwrap(message)

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
    inner = unwrap(message)
    field = content_field(inner)
    if field is None:
        return False
    if field in ("senderKeyDistributionMessage", "protocolMessage"):
        return False
    return True


def quoted_message_id(message: Any) -> str | None:
    inner = unwrap(message)
    for field, _ in inner.ListFields():
        value = getattr(inner, field.name, None)
        ctx = getattr(value, "contextInfo", None)
        if ctx is not None and getattr(ctx, "stanzaID", ""):
            return ctx.stanzaID
    return None


def revoked_message_id(message: Any) -> str | None:
    inner = unwrap(message)
    if _has(inner, "protocolMessage") and inner.HasField("protocolMessage"):
        proto = inner.protocolMessage
        type_name = _enum_name(proto, "type")
        if type_name and "REVOKE" in type_name and proto.HasField("key"):
            return proto.key.ID or None
    return None


def edited_payload(message: Any) -> tuple[str, str] | None:
    inner = unwrap(message)
    if not (_has(inner, "protocolMessage") and inner.HasField("protocolMessage")):
        return None
    proto = inner.protocolMessage
    if not proto.HasField("editedMessage"):
        return None
    target = proto.key.ID if proto.HasField("key") else ""
    return target, extract_text(proto.editedMessage)


def media_descriptor(message: Any) -> dict | None:
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
                "seconds": int(getattr(media, "seconds", 0) or 0),
            }
    return None


def _has(message: Any, field: str) -> bool:
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
