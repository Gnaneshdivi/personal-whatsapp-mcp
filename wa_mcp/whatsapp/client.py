"""The WhatsApp socket — one number, one process.

What this is not, compared to the multi-pod design it replaces: there is no
ownership lease, no fencing token, no Redis, no heartbeat and no pod registry.
All of that existed to stop two processes opening a socket on one number. With
a single process the guarantee is structural, and ~800 lines of coordination go
away.

What survives, because none of it was about scale:

  * the send-side token bucket — ban protection is a WhatsApp concern
  * the RETRY-receipt resend — whatsmeow's retry buffer is never populated
  * JID normalisation and LID resolution — or one chat becomes three
  * every handler individually guarded — one bad payload must not take the
    socket down

Device selection uses ClientFactory with an EXPLICIT JID. `NewAClient(dsn)` with
no jid loads whichever device the store happens to hold, which is how a second
client ends up authenticating as the first, drawing StreamReplaced and panicking
the Go layer — taking the whole process with it, since a Go panic cannot be
caught from Python.
"""
from __future__ import annotations

import asyncio
import base64
import logging
import time
from typing import Any, Awaitable, Callable

from ..errors import LoggedOut, NotConnected, RateLimited, SendFailed
from ..store.base import Message, to_ms
from . import extract
from . import jid as J
from .contacts import ContactBook
from .events import SUBSCRIBED, event_type_for
from .sync import Phase, SyncTracker

log = logging.getLogger(__name__)

# Ban protection. Deliberately not configurable: an open-source tool whose
# first-run default lets someone blast a contact list is a tool that gets its
# users banned. Bursts are refused rather than queued, because a queue that
# drains later is the same traffic pattern with a delay.
SEND_PER_SECOND = 1.0
SEND_BURST = 5


class _TokenBucket:
    def __init__(self, rate: float, burst: int):
        self._rate, self._burst = rate, burst
        self._tokens = float(burst)
        self._last = time.monotonic()

    def take(self) -> float:
        """0 if allowed, else seconds to wait."""
        now = time.monotonic()
        self._tokens = min(self._burst, self._tokens + (now - self._last) * self._rate)
        self._last = now
        if self._tokens >= 1:
            self._tokens -= 1
            return 0.0
        return (1 - self._tokens) / self._rate


class WhatsApp:
    """Owns the socket, the event pipeline and the send path for one number."""

    def __init__(self, *, session_dsn: str, store, settings,
                 contacts: ContactBook | None = None,
                 on_event: Callable[[str, dict], Awaitable[None]] | None = None):
        self.session_dsn = session_dsn
        self.store = store
        self.settings = settings
        self.contacts = contacts or ContactBook(session_dsn)
        self._on_event = on_event

        self.sync = SyncTracker()
        self.self_jid: str = ""
        self.push_name: str = ""
        self.qr: str | None = None
        self.qr_issued_at: float = 0.0

        self._client = None          # neonize NewAClient
        self._factory = None
        self._bucket = _TokenBucket(SEND_PER_SECOND, SEND_BURST)
        self._resolver = J.Resolver()
        self._retried: dict[str, float] = {}   # bounded, see _mark_retried
        self._tasks: list[asyncio.Task] = []
        self._stopped = asyncio.Event()

    # ==================================================== lifecycle

    def _build_factory(self):
        from neonize.aioze.client import ClientFactory
        return ClientFactory(self.session_dsn)

    def paired_devices(self) -> list[str]:
        """JIDs already in the session store. Empty means we have never paired."""
        try:
            self._factory = self._factory or self._build_factory()
            return [J.from_obj(getattr(d, "JID", None)) for d in self._factory.get_all_devices()]
        except Exception as exc:
            log.debug("device enumeration failed: %s", exc)
            return []

    async def start(self) -> bool:
        """Connect the already-paired number. False when there is nothing to connect."""
        devices = self.paired_devices()
        if not devices:
            self.sync.unpaired()
            return False

        self.self_jid = devices[0]
        self._open(jid_str=self.self_jid)
        self.sync.connecting()
        await self._connect()
        return True

    async def pair(self) -> None:
        """Open a provisional socket with no device, so WhatsApp issues a QR.

        `jid=None` is correct here and ONLY here — it is the one case where we
        genuinely have no device yet. Everywhere else an explicit JID is what
        stops a second client hijacking the first.
        """
        if self.paired_devices():
            raise SendFailed("a number is already linked; log out before pairing another")
        self._open(jid_str=None)
        self.sync.pairing()
        await self._connect()

    def _open(self, jid_str: str | None) -> None:
        from neonize.proto.waCompanionReg.WAWebProtobufsCompanionReg_pb2 import DeviceProps
        from neonize.utils import build_jid

        self._factory = self._factory or self._build_factory()

        props = None
        if self.settings.device_os:
            # neonize announces os="Neonize" by default, which tells WhatsApp
            # the account is automation-driven. Only changeable by pairing
            # afresh, so it is set here rather than anywhere later.
            props = DeviceProps(
                os=self.settings.device_os,
                platformType=DeviceProps.PlatformType.Value(self.settings.device_platform),
            )

        jid = None
        if jid_str:
            user, _, server = jid_str.partition("@")
            jid = build_jid(user, server or J.USER_SERVER)

        self._client = self._factory.new_client(
            jid=jid, uuid=None if jid else "pairing", props=props
        )
        self._resolver = J.Resolver(self._lookup_pn)
        self._wire()

    async def _connect(self) -> None:
        self._tasks.append(asyncio.create_task(self._client.connect()))
        self._tasks.append(asyncio.create_task(self._settle_loop()))

    async def stop(self) -> None:
        self._stopped.set()
        for t in self._tasks:
            t.cancel()
        self._tasks.clear()
        if self._client is not None:
            try:
                await self._client.disconnect()
            except Exception as exc:
                log.warning("disconnect: %s", exc)

    async def logout(self) -> dict[str, Any]:
        if self._client is None:
            raise NotConnected("not connected")
        await self._client.logout()
        self.sync.logged_out()
        return {"status": "logged_out"}

    async def _settle_loop(self) -> None:
        """Drives SyncTracker.tick(), which closes a sync that never completed."""
        while not self._stopped.is_set():
            self.sync.tick()
            try:
                await asyncio.wait_for(self._stopped.wait(), timeout=5)
            except asyncio.TimeoutError:
                pass

    # ==================================================== event wiring

    def _wire(self) -> None:
        import neonize.events as NE

        for name in SUBSCRIBED:
            cls = getattr(NE, name, None)
            if cls is None:
                continue          # absent from this neonize build
            self._register(cls, name)

        # The QR does NOT arrive through @event(QREv). neonize routes it to a
        # separate callback whose default implementation prints ASCII art to a
        # terminal — useless to a server.
        @self._client.qr
        async def _on_qr(_c, data: bytes):
            try:
                self.qr = data.decode() if isinstance(data, bytes) else str(data)
                self.qr_issued_at = time.monotonic()
                self.sync.pairing()
                log.info("QR issued (%d chars)", len(self.qr))
                await self._emit("connection.qr", {"qr": self.qr})
            except Exception:
                log.exception("qr callback failed")

    def _register(self, cls, name: str) -> None:
        @self._client.event(cls)
        async def _handler(_c, ev, _name=name):
            try:
                await self._dispatch(_name, ev)
            except Exception:
                # One malformed payload must never take the number offline. The
                # exception would otherwise be orphaned in a future nobody
                # awaits, since these arrive via run_coroutine_threadsafe.
                log.exception("handler %s failed", _name)

    async def _dispatch(self, name: str, ev) -> None:
        self.sync.saw_event()

        if name == "MessageEv":
            await self._on_message(ev)
        elif name == "ReceiptEv":
            await self._on_receipt(ev)
        elif name == "ConnectedEv":
            await self._on_connected(ev)
        elif name == "PairStatusEv":
            await self._on_paired(ev)
        elif name in ("LoggedOutEv", "StreamReplacedEv"):
            self.sync.logged_out()
        elif name == "TemporaryBanEv":
            log.critical("TEMPORARY BAN code=%s expires=%s",
                         getattr(ev, "Code", None), getattr(ev, "Expire", None))
        elif name in ("GroupInfoEv", "JoinedGroupEv"):
            await self._on_group(name, ev)
        elif name == "OfflineSyncPreview":
            self.sync.offline_preview(
                getattr(ev, "Total", 0), Message=getattr(ev, "Message", 0),
                Receipts=getattr(ev, "Receipts", 0),
                Notifications=getattr(ev, "Notifications", 0),
            )
        elif name == "OfflineSyncCompleted":
            self.sync.offline_completed(getattr(ev, "Count", 0))
        elif name == "HistorySyncEv":
            await self._on_history(ev)

        await self._emit(event_type_for(name), {})

    # ------------------------------------------------------------- handlers

    async def _on_connected(self, _ev) -> None:
        self.sync.connected()
        if not self.self_jid:
            try:
                me = await self._client.get_me()
                self.self_jid = J.from_obj(getattr(me, "JID", None))
                self.push_name = getattr(me, "PushName", "") or ""
            except Exception:
                pass
        loaded = await self.contacts.load()
        log.info("connected as %s (%d contacts)", self.self_jid or "?", loaded)
        # Group names arrive ONLY via group events, which do not always fire.
        # Asking outright is the difference between a readable chat list and 31
        # rows of numeric ids.
        self._tasks.append(asyncio.create_task(self._backfill_group_names()))

    async def _on_paired(self, ev) -> None:
        jid = J.from_obj(getattr(ev, "ID", None))
        if jid:
            self.self_jid = jid
            self.qr = None
            log.info("paired as %s", jid)
            await self._emit("connection.paired", {"phone_jid": jid})

    async def _on_history(self, ev) -> None:
        data = getattr(ev, "Data", None)
        if data is None:
            return
        st = getattr(data, "syncType", None)
        name = ""
        try:
            name = data.DESCRIPTOR.fields_by_name["syncType"].enum_type.values_by_number[st].name
        except Exception:
            name = str(st or "")
        self.sync.history_chunk(
            name, getattr(data, "progress", None), len(getattr(data, "conversations", []) or [])
        )

    async def _on_group(self, name: str, ev) -> None:
        """Group names nest differently on the two events that carry them."""
        try:
            if name == "GroupInfoEv":
                chat = J.from_obj(getattr(ev, "JID", None))
                gname = getattr(getattr(ev, "Name", None), "Name", "") or None
            else:
                info = getattr(ev, "GroupInfo", None)
                chat = J.from_obj(getattr(info, "JID", None))
                gname = getattr(getattr(info, "GroupName", None), "Name", "") or None
            if chat:
                await self.store.upsert_chat_meta(chat, name=gname, is_group=True)
        except Exception:
            log.exception("group event %s", name)

    async def _backfill_group_names(self) -> None:
        try:
            groups = await self._client.get_joined_groups()
        except Exception as exc:
            log.debug("get_joined_groups failed: %s", exc)
            return
        n = 0
        for g in groups or []:
            chat = J.from_obj(getattr(g, "JID", None))
            gname = getattr(g, "GroupName", None)
            gname = getattr(gname, "Name", None) if gname is not None else getattr(g, "Name", None)
            if chat and gname:
                await self.store.upsert_chat_meta(chat, name=str(gname), is_group=True)
                n += 1
        if n:
            log.info("named %d groups", n)

    async def _on_message(self, ev) -> None:
        info = ev.Info
        src = info.MessageSource
        chat = await self._resolver.canonical(J.from_obj(src.Chat))
        sender = await self._resolver.canonical(J.from_obj(src.Sender))
        message_id = info.ID
        ts = to_ms(getattr(info, "Timestamp", 0))
        body = ev.Message

        msg_type = extract.message_type(body)

        if msg_type == "edit":
            edited = extract.edited_payload(body)
            if edited:
                await self.store.apply_edit(edited[0], edited[1], ts)
            return
        if msg_type == "revoke":
            target = extract.revoked_message_id(body)
            if target:
                await self.store.apply_revoke(target, ts)
            return

        # Protocol artifacts — sender-key rotation, app-state sync — arrive
        # through the same event as real messages and were ~40% of raw traffic.
        # Forwarded as events, never stored as messages.
        if not extract.is_content_message(body):
            return
        if J.is_ignorable(chat):
            return

        media = extract.media_descriptor(body)
        stored = await self.store.upsert_message(Message(
            message_id=message_id,
            chat_jid=chat,
            sender_jid=sender,
            sender_name=(getattr(info, "PushName", "") or "").strip() or None,
            is_from_me=bool(src.IsFromMe),
            ts=ts,
            type=msg_type,
            text=extract.extract_text(body) or None,
            media_meta=media or {},
            quoted_id=extract.quoted_message_id(body),
            # Media messages ALWAYS keep their bytes, regardless of the flag.
            # Decrypting an attachment later needs the original protobuf — the
            # keys live in it — so without this, download_media can never work
            # and every image in the history is permanently unreachable.
            # Measured on a real account: media is 16% of messages, so this
            # costs little, while storing every text message's protobuf was
            # half the table.
            raw_proto=(body.SerializeToString()
                       if (self.settings.store_raw_proto or media) else None),
        ))
        if not stored:
            return

        preview = extract.extract_text(body) or (f"[{msg_type}]" if msg_type != "text" else None)
        await self.store.touch_chat(chat, ts, bool(src.IsFromMe), preview)
        await self.store.upsert_chat_meta(chat, is_group=J.is_group(chat))

        await self._emit("message.received" if not src.IsFromMe else "message.sent", {
            "message_id": message_id, "chat_jid": chat, "sender_jid": sender,
            "text": extract.extract_text(body), "type": msg_type,
            "from_me": bool(src.IsFromMe), "ts": ts,
            "mentioned_me": self._mentions_me(body),
        })

    def _mentions_me(self, body) -> bool:
        """Whether this message @-mentions us.

        Group replies are gated on this by default, so a false negative means a
        silent bot and a false positive means an unwanted one. Mentions live in
        contextInfo.mentionedJID on whichever sub-message carries the text, so
        the unwrapped body is what has to be inspected.
        """
        if not self.self_jid:
            return False
        me = J.normalise(self.self_jid).split("@")[0]
        try:
            inner = extract.unwrap(body)
        except Exception:
            inner = body
        for _field, value in getattr(inner, "ListFields", lambda: [])():
            ctx = getattr(value, "contextInfo", None)
            for mentioned in list(getattr(ctx, "mentionedJID", None) or []):
                if J.normalise(str(mentioned)).split("@")[0] == me:
                    return True
        return False

    async def _on_receipt(self, ev) -> None:
        """Answer RETRY receipts by resending from our own store.

        whatsmeow is meant to re-encrypt the original from whatsmeow_retry_buffer,
        but neonize never populates that table — verified at 0 rows against an
        account with 1,920 messages. With nothing to resend, the first message to
        a new contact sits on one tick forever.
        """
        kind = _enum_name(ev, "Type")
        if kind != "RETRY":
            return
        for mid in list(getattr(ev, "MessageIDs", None) or []):
            if mid in self._retried:
                continue
            self._mark_retried(mid)
            row = await self.store.get_message(mid)
            if row is None or not row.text or not row.is_from_me:
                continue
            log.info("RETRY for %s — resending to %s", mid, row.chat_jid)
            try:
                sent = await self.send_text(row.chat_jid, row.text)
            except Exception as exc:
                log.error("resend failed: %s", exc)
                continue
            # The resend gets a NEW id and will draw its own RETRY. Without
            # marking it too, one message became five in seven seconds.
            if sent.get("message_id"):
                self._mark_retried(sent["message_id"])

    def _mark_retried(self, message_id: str) -> None:
        """Bounded. The old implementation grew this set for process lifetime."""
        self._retried[message_id] = time.monotonic()
        if len(self._retried) > 2000:
            for k, _ in sorted(self._retried.items(), key=lambda kv: kv[1])[:1000]:
                self._retried.pop(k, None)

    async def _emit(self, event_type: str, payload: dict) -> None:
        if self._on_event is None:
            return
        try:
            await self._on_event(event_type, payload)
        except Exception:
            log.exception("event subscriber failed")

    def _lookup_pn(self, lid: str):
        from neonize.utils import build_jid
        user = lid.split("@")[0]
        return self._client.get_pn_from_lid(build_jid(user, J.LID_SERVER))

    # ==================================================== send path

    def _guard(self) -> None:
        if self._client is None or self.sync.state.phase in (Phase.UNPAIRED, Phase.LOGGED_OUT):
            raise NotConnected("no live WhatsApp session")
        wait = self._bucket.take()
        if wait > 0:
            raise RateLimited(wait)

    def _jid(self, value: str):
        from neonize.utils import build_jid
        target = J.to_jid(value)
        if not target:
            raise SendFailed("no recipient")
        user, _, server = target.partition("@")
        return build_jid(user, server or J.USER_SERVER)

    async def send_text(self, to: str, text: str, reply_to: str | None = None) -> dict:
        self._guard()
        jid = self._jid(to)
        try:
            if reply_to:
                quoted = await self.store.get_message(reply_to)
                built = await self._client.build_reply_message(
                    text, quoted_message_id=reply_to,
                    quoted_message_sender=self._jid(quoted.sender_jid or to),
                ) if quoted else text
                resp = await self._client.send_message(jid, built)
            else:
                resp = await self._client.send_message(jid, text)
        except Exception as exc:
            raise SendFailed(str(exc)) from exc
        return await self._record(jid, resp, text, "text")

    async def send_media(self, to: str, data_b64: str, kind: str = "image",
                         caption: str | None = None, filename: str | None = None) -> dict:
        self._guard()
        jid = self._jid(to)
        try:
            raw = base64.b64decode(data_b64, validate=True)
        except Exception:
            raise SendFailed("media is not valid base64")
        senders = {
            "image": self._client.send_image, "video": self._client.send_video,
            "audio": self._client.send_audio, "document": self._client.send_document,
            "sticker": self._client.send_sticker,
        }
        fn = senders.get(kind)
        if fn is None:
            raise SendFailed(f"unsupported kind {kind!r}")
        kwargs: dict[str, Any] = {}
        if kind in ("image", "video", "document") and caption:
            kwargs["caption"] = caption
        if kind == "document" and filename:
            kwargs["filename"] = filename
        try:
            resp = await fn(jid, raw, **kwargs) if kwargs else await fn(jid, raw)
        except Exception as exc:
            raise SendFailed(str(exc)) from exc
        return await self._record(jid, resp, caption or "", kind)

    async def react(self, chat_jid: str, message_id: str, emoji: str) -> dict:
        self._guard()
        chat = self._jid(chat_jid)
        me = self._jid(self.self_jid or chat_jid)
        reaction = await self._client.build_reaction(chat, me, message_id, emoji)
        resp = await self._client.send_message(chat, reaction)
        return {"message_id": getattr(resp, "ID", None), "status": "sent"}

    async def mark_read(self, chat_jid: str, message_ids: list[str] | None = None) -> dict:
        self._guard()
        from neonize.utils.enum import ReceiptType
        chat = self._jid(chat_jid)
        ids = message_ids or []
        if ids:
            await self._client.mark_read(*ids, chat=chat,
                                         sender=self._jid(self.self_jid or chat_jid),
                                         receipt=ReceiptType.READ)
        await self.store.set_unread(J.to_jid(chat_jid), 0)
        return {"status": "ok", "marked": len(ids)}

    async def set_typing(self, chat_jid: str, typing: bool = True) -> dict:
        """Typing indicator. Cheap, and it is what makes an auto-reply read as human."""
        self._guard()
        from neonize.utils.enum import ChatPresence, ChatPresenceMedia
        await self._client.send_chat_presence(
            self._jid(chat_jid),
            ChatPresence.CHAT_PRESENCE_COMPOSING if typing else ChatPresence.CHAT_PRESENCE_PAUSED,
            ChatPresenceMedia.CHAT_PRESENCE_MEDIA_TEXT,
        )
        return {"status": "ok"}

    async def check_number(self, phone: str) -> dict:
        self._guard()
        results = await self._client.is_on_whatsapp(phone.lstrip("+"))
        entry = results[0] if results else None
        jid = J.from_obj(getattr(entry, "JID", None)) if entry else ""
        if entry is not None and not getattr(entry, "IsIn", True):
            jid = ""
        return {"phone": phone, "jid": jid or None, "on_whatsapp": bool(jid)}

    async def group_info(self, chat_jid: str) -> dict:
        """Name, topic and participants, asked of WhatsApp directly."""
        self._guard()
        info = await self._client.get_group_info(self._jid(chat_jid))
        name = getattr(getattr(info, "GroupName", None), "Name", None) or getattr(info, "Name", "")
        topic = getattr(getattr(info, "GroupTopic", None), "Topic", "") or ""
        people = []
        for p in list(getattr(info, "Participants", None) or []):
            people.append({"jid": J.from_obj(getattr(p, "JID", None)),
                           "admin": bool(getattr(p, "IsAdmin", False))})
        return {"chat_jid": J.to_jid(chat_jid), "name": str(name), "topic": str(topic),
                "participants": people, "participant_count": len(people)}

    async def download_media(self, message_id: str) -> bytes | None:
        """Fetch media on demand.

        On demand rather than eagerly: the first user with a 4 GB history would
        otherwise fill their disk before the UI finished loading.
        """
        row = await self.store.get_message(message_id)
        if row is None or not row.media_meta:
            return None
        if self._client is None:
            raise NotConnected("no live session")
        from neonize.proto.waE2E import WAWebProtobufsE2E_pb2 as E2E
        if not row.raw_proto:
            raise SendFailed(
                "this message predates media support — its original bytes were "
                "not stored, so the attachment can no longer be decrypted"
            )
        body = E2E.Message()
        body.ParseFromString(row.raw_proto)
        return await self._client.download_any(body)

    async def _record(self, jid, resp, text: str, kind: str) -> dict:
        """Persist our own send.

        WhatsApp does not echo a device's own messages back to that device, so
        without this the model reads a conversation containing only one side.
        """
        message_id = getattr(resp, "ID", "") or ""
        ts = to_ms(getattr(resp, "Timestamp", 0))
        chat = J.from_obj(jid)
        if message_id:
            await self.store.upsert_message(Message(
                message_id=message_id, chat_jid=chat, sender_jid=self.self_jid,
                is_from_me=True, ts=ts, type=kind, text=text or None,
            ))
            await self.store.touch_chat(chat, ts, True, text or f"[{kind}]")
        return {"message_id": message_id, "to": chat, "type": kind,
                "text": text, "status": "sent"}


def _enum_name(message, field: str) -> str:
    """Protobuf enums are ints on the attribute even though MessageToDict renders
    them as names. Calling .upper() on one raised on every receipt, which is why
    the RETRY handler had never run."""
    value = getattr(message, field, None)
    if isinstance(value, str):
        return value.upper()
    try:
        d = message.DESCRIPTOR.fields_by_name[field]
        return d.enum_type.values_by_number[value].name.upper()
    except Exception:
        return str(value or "").upper()
