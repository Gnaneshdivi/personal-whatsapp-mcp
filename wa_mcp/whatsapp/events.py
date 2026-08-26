from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


EVENT_TYPES: dict[str, str] = {
    "MessageEv": "message.received",
    "UndecryptableMessageEv": "message.undecryptable",
    "ReceiptEv": "receipt.updated",
    "ChatPresenceEv": "presence.chat",
    "PresenceEv": "presence.user",
    "JoinedGroupEv": "group.joined",
    "GroupInfoEv": "group.updated",
    "PictureEv": "profile.picture_changed",
    "IdentityChangeEv": "identity.changed",
    "PrivacySettingsEv": "privacy.updated",
    "BlocklistEv": "blocklist.synced",
    "BlocklistChangeEv": "blocklist.changed",
    "NewsletterJoinEv": "newsletter.joined",
    "NewsletterLeaveEv": "newsletter.left",
    "NewsletterMuteChangeEv": "newsletter.mute_changed",
    "NewsletterLiveUpdateEV": "newsletter.live_update",
    "NewsLetterMessageMetaEv": "newsletter.message_meta",
    "CallOfferEv": "call.offer",
    "CallAcceptEv": "call.accept",
    "CallPreAcceptEv": "call.pre_accept",
    "CallTransportEv": "call.transport",
    "CallOfferNoticeEv": "call.offer_notice",
    "CallRelayLatencyEV": "call.relay_latency",
    "CallTerminateEv": "call.terminate",
    "UnknownCallEventEV": "call.unknown",
    "QREv": "connection.qr",
    "PairStatusEv": "connection.pair_status",
    "ConnectedEv": "connection.connected",
    "DisconnectedEv": "connection.disconnected",
    "LoggedOutEv": "connection.logged_out",
    "StreamReplacedEv": "connection.stream_replaced",
    "StreamErrorEv": "connection.stream_error",
    "ConnectFailureEv": "connection.connect_failure",
    "ClientOutdatedEv": "connection.client_outdated",
    "KeepAliveTimeoutEv": "connection.keepalive_timeout",
    "KeepAliveRestoredEv": "connection.keepalive_restored",
    "TemporaryBanEv": "connection.temporary_ban",
    "HistorySyncEv": "sync.history",
    "OfflineSyncPreviewEv": "sync.offline_preview",
    "OfflineSyncCompletedEv": "sync.offline_completed",
}

SUBSCRIBED = tuple(EVENT_TYPES.keys())
CRITICAL = frozenset({"TemporaryBanEv", "LoggedOutEv", "StreamReplacedEv", "ClientOutdatedEv"})

UNAVAILABLE_IN_0_4_3 = (
    "MutedEv", "ArchiveEv", "PinEv", "StarEv",
    "MarkChatAsReadEv", "DeleteChatEv", "DeleteForMeEv",
    "ContactEv", "PushNameEv", "BusinessNameEv",
    "PairSuccessEv", "PairErrorEv",
)


@dataclass
class Envelope:

    event_type: str
    connection_id: str | None = None
    tenant_id: str | None = None
    chat_jid: str | None = None
    sender_jid: str | None = None
    message_id: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)
    event_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    occurred_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "event_type": self.event_type,
            "connection_id": self.connection_id,
            "tenant_id": self.tenant_id,
            "chat_jid": self.chat_jid,
            "sender_jid": self.sender_jid,
            "message_id": self.message_id,
            "occurred_at": self.occurred_at.astimezone(timezone.utc)
            .isoformat()
            .replace("+00:00", "Z"),
            "payload": self.payload,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Envelope":
        occurred = data.get("occurred_at")
        if isinstance(occurred, str):
            occurred = datetime.fromisoformat(occurred.replace("Z", "+00:00"))
        return cls(
            event_type=data["event_type"],
            connection_id=data.get("connection_id"),
            tenant_id=data.get("tenant_id"),
            chat_jid=data.get("chat_jid"),
            sender_jid=data.get("sender_jid"),
            message_id=data.get("message_id"),
            payload=data.get("payload") or {},
            event_id=data.get("event_id") or str(uuid.uuid4()),
            occurred_at=occurred or datetime.now(timezone.utc),
        )


def event_type_for(class_name: str) -> str:
    return EVENT_TYPES.get(class_name, f"unknown.{class_name}")


_EVENT_NS = uuid.UUID("6f9619ff-8b86-d011-b42d-00c04fc964ff")
