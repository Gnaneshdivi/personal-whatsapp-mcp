"""Event registry and the canonical webhook envelope.

Deliberately imports nothing from neonize: the MCP web tier installs the core
package without neonize installed, and must still be able to parse envelopes and
reason about event types. Events are keyed by class NAME (a string), so this module
stays importable with no compiled Go binary present.

THE EVENT NAMES BELOW ARE VERIFIED AGAINST neonize 0.4.3 (EVENT_TO_INT, 41 entries).
Several names in the original design doc do not exist in this version — see
UNAVAILABLE_IN_0_4_3 at the bottom before adding a subscription.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

# ---------------------------------------------------------------------------
# neonize event class name -> canonical event_type on the wire.
#
# The envelope's event_type is OUR contract with the trigger service and must stay
# stable even if neonize renames a class, so the two namespaces are mapped
# explicitly rather than derived from the class name.
# ---------------------------------------------------------------------------

EVENT_TYPES: dict[str, str] = {
    # --- messages -----------------------------------------------------------
    # MessageEv is refined at emit time — see client._enrich. One neonize event
    # becomes one of three wire types, because consumers filter on event_type and
    # these three want completely different handling:
    #   message.received  inbound, has content         -> a rule may reply
    #   message.sent      outbound (is_from_me)        -> never reply
    #   message.protocol  sender-key rotation, app-state sync, and other artifacts
    #                     that arrive through MessageEv but carry no user content
    #                     -> never reply. 40% of raw MessageEv traffic, observed.
    "MessageEv": "message.received",
    "UndecryptableMessageEv": "message.undecryptable",
    "ReceiptEv": "receipt.updated",           # refined by receipt type at build time
    # --- presence -----------------------------------------------------------
    "ChatPresenceEv": "presence.chat",
    "PresenceEv": "presence.user",
    # --- groups -------------------------------------------------------------
    "JoinedGroupEv": "group.joined",
    "GroupInfoEv": "group.updated",
    # --- identity / profile -------------------------------------------------
    "PictureEv": "profile.picture_changed",
    "IdentityChangeEv": "identity.changed",
    "PrivacySettingsEv": "privacy.updated",
    # --- blocklist ----------------------------------------------------------
    "BlocklistEv": "blocklist.synced",
    "BlocklistChangeEv": "blocklist.changed",
    # --- newsletters --------------------------------------------------------
    "NewsletterJoinEv": "newsletter.joined",
    "NewsletterLeaveEv": "newsletter.left",
    "NewsletterMuteChangeEv": "newsletter.mute_changed",
    "NewsletterLiveUpdateEV": "newsletter.live_update",
    "NewsLetterMessageMetaEv": "newsletter.message_meta",
    # --- calls --------------------------------------------------------------
    "CallOfferEv": "call.offer",
    "CallAcceptEv": "call.accept",
    "CallPreAcceptEv": "call.pre_accept",
    "CallTransportEv": "call.transport",
    "CallOfferNoticeEv": "call.offer_notice",
    "CallRelayLatencyEV": "call.relay_latency",
    "CallTerminateEv": "call.terminate",
    "UnknownCallEventEV": "call.unknown",
    # --- connection lifecycle ----------------------------------------------
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
    # --- sync ---------------------------------------------------------------
    "HistorySyncEv": "sync.history",
    "OfflineSyncPreviewEv": "sync.offline_preview",
    "OfflineSyncCompletedEv": "sync.offline_completed",
}

# Every event we subscribe to. Subscribe to everything: deciding what matters is
# decide what matters; the trigger service does. Broad subscription is what lets
# cheap here and impossible to add later without re-pairing, so watch rules and
# automations can grow without touching this layer again.
SUBSCRIBED = tuple(EVENT_TYPES.keys())
# Must never be swallowed. A ban that only shows up in a log is a ban nobody acts on.
CRITICAL = frozenset({"TemporaryBanEv", "LoggedOutEv", "StreamReplacedEv", "ClientOutdatedEv"})

# Named in the design doc but ABSENT from neonize 0.4.3's EVENT_TO_INT registry.
# whatsmeow emits these upstream, but this binding does not surface them as
# separate proto events — chat-state changes arrive folded into app-state sync.
# Subscribing to any of them raises UnsupportedEvent at decorator time, so they are
# recorded here rather than silently dropped from the list above.
UNAVAILABLE_IN_0_4_3 = (
    "MutedEv", "ArchiveEv", "PinEv", "StarEv",
    "MarkChatAsReadEv", "DeleteChatEv", "DeleteForMeEv",
    "ContactEv", "PushNameEv", "BusinessNameEv",
    # The doc also proposed PairSuccessEv / PairErrorEv; 0.4.3 exposes the single
    # PairStatusEv instead, alongside QREv for QR emission.
    "PairSuccessEv", "PairErrorEv",
)


@dataclass
class Envelope:
    """The frozen contract with the trigger service.

    Consumers are built against this shape, so fields may be ADDED but never
    renamed or removed without a package major version.
    """

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
    """Canonical event_type for a neonize event class name."""
    return EVENT_TYPES.get(class_name, f"unknown.{class_name}")


_EVENT_NS = uuid.UUID("6f9619ff-8b86-d011-b42d-00c04fc964ff")
