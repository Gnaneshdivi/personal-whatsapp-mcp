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

from ..errors import NotConnected, RateLimited, SendFailed
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
                 session_is_file: bool = True,
                 on_event: Callable[[str, dict], Awaitable[None]] | None = None):
        self.session_dsn = session_dsn
        # Whether the session is a file we may delete, or Postgres tables whose
        # schema this process does not own.
        self.session_is_file = session_is_file
        self.store = store
        self.settings = settings
        # `contacts or ContactBook(...)` is wrong: ContactBook defines
        # __len__, so an empty one is FALSY and the caller's book gets silently
        # replaced by a second instance. The client then loads 2,614 names into
        # an object nobody else holds, and every chat renders as a phone number
        # while the log cheerfully reports the contacts were loaded.
        self.contacts = contacts if contacts is not None else ContactBook(session_dsn)
        self._on_event = on_event

        self.sync = SyncTracker()
        self.self_jid: str = ""      # normalised, for chat keys and display
        self.device_jid = None       # raw protobuf, what whatsmeow needs
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
        self.load_error: str | None = None
        # One pairing attempt at a time, process-wide. Two clients on one
        # session store is the failure this whole codebase is arranged to
        # avoid: they both connect, their QR refs invalidate each other, one
        # of them pairs, and the UI ends up watching the other — which is
        # exactly what happened the first time this ran for real.
        self._pair_lock = asyncio.Lock()
        self._pairing = False

    # ==================================================== lifecycle

    def _build_factory(self):
        from neonize.aioze.client import ClientFactory
        return ClientFactory(self.session_dsn)

    @staticmethod
    def preflight() -> str | None:
        """Why neonize cannot load, in words someone can act on.

        neonize imports python-magic at module load, which dlopens the native
        libmagic. When that is absent the failure surfaces as an ImportError
        deep inside a pairing attempt — which, swallowed, looks exactly like a
        QR that never arrives. Checking at startup turns a mystery into an
        install command.
        """
        try:
            import neonize  # noqa: F401
            return None
        except ImportError as exc:
            if "libmagic" in str(exc):
                import sys
                fix = ("brew install libmagic" if sys.platform == "darwin"
                       else "sudo apt-get install -y libmagic1")
                return (f"libmagic is not installed, so neonize cannot load.\n"
                        f"  Fix it with:  {fix}")
            return f"neonize cannot be imported: {exc}"
        except Exception as exc:
            return f"neonize cannot be imported: {type(exc).__name__}: {exc}"

    def _device_jids(self) -> list:
        """The raw JID protobufs from the session store.

        Raw, and never round-tripped through a string. A device JID carries a
        device NUMBER — 919100828649:9 — and `J.normalise` strips it, because
        for a chat key the suffix is noise that splits one conversation into
        several. Hand that stripped form back to whatsmeow and it finds no such
        device, returns nil, and NewClient dereferences it: a Go panic that
        takes the whole process down, since Python cannot catch one.

        Two correct-looking functions, opposite requirements. Keeping the
        protobuf means the question never comes up.
        """
        try:
            self._factory = self._factory or self._build_factory()
            return [d.JID for d in self._factory.get_all_devices() if d.HasField("JID")]
        except Exception as exc:
            self.load_error = self.preflight() or f"{type(exc).__name__}: {exc}"
            log.error("cannot read the session store: %s", self.load_error)
            return []

    def paired_devices(self) -> list[str]:
        """Human-readable JIDs of linked devices. For display and for tests.

        A failure here used to be logged at debug and returned as [] — which is
        indistinguishable from "not paired yet", so a broken install presented
        as an install that simply never finished pairing.
        """
        return [J.from_obj(j) for j in self._device_jids()]

    async def start(self) -> bool:
        """Connect the already-paired number. False when there is nothing to connect."""
        devices = self._device_jids()
        if not devices:
            self.sync.unpaired()
            return False

        device = devices[0]
        self.device_jid = device                 # raw, for whatsmeow
        self.self_jid = J.from_obj(device)       # normalised, for chats and display
        self._open(device=device)
        self.sync.connecting()
        await self._connect()
        return True

    async def pair(self) -> None:
        """Open a provisional socket with no device, so WhatsApp issues a QR.

        Raises with an actionable message rather than hanging when the native
        dependency is missing.

        `jid=None` is correct here and ONLY here — it is the one case where we
        genuinely have no device yet. Everywhere else an explicit JID is what
        stops a second client hijacking the first.
        """
        async with self._pair_lock:
            if self._pairing:
                # Already offering a code. The caller gets that one rather than
                # a rival socket whose QR would invalidate it.
                return
            blocked = self.preflight()
            if blocked:
                self.load_error = blocked
                raise SendFailed(blocked)
            if self.paired_devices():
                raise SendFailed(
                    "a number is already linked; log out before pairing another")
            self._pairing = True
            self._open(device=None)
            self.sync.pairing()
            await self._connect()

    def _open(self, device=None) -> None:
        """Build the neonize client. `device` is a raw JID protobuf, or None to
        pair a new one — the only case where no device is correct."""
        from neonize.proto.waCompanionReg.WAWebProtobufsCompanionReg_pb2 import DeviceProps

        self._factory = self._factory or self._build_factory()

        props = None
        if self.settings.device_os:
            # neonize announces os="Neonize" by default, which tells WhatsApp
            # the account is automation-driven. Only changeable by pairing
            # afresh, so it is set here rather than anywhere later.
            props = DeviceProps(
                os=self.settings.device_os,
                platformType=DeviceProps.PlatformType.Value(self.settings.device_platform),
                # Ask for history explicitly. The default gives a few recent
                # days, which leaves a freshly paired install showing a chat
                # list of empty conversations. This is a pair-time decision and
                # cannot be revisited without unlinking.
                requireFullSync=True,
                historySyncConfig=DeviceProps.HistorySyncConfig(
                    fullSyncDaysLimit=self.settings.history_days,
                    fullSyncSizeMbLimit=self.settings.history_size_mb,
                    storageQuotaMb=self.settings.history_size_mb,
                ),
            )

        self._client = self._factory.new_client(
            jid=device, uuid=None if device is not None else "pairing", props=props
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

    async def logout(self, purge: bool = True) -> dict[str, Any]:
        """Unlink the device and, by default, delete everything it collected.

        Keeping the archive after unlinking leaves a full copy of somebody's
        conversations on disk for an account this server can no longer reach.
        It cannot be refreshed, cannot be corrected, and is still readable by
        anyone who gets the file — so logging out clears it.

        Irreversible either way: WhatsApp sends history exactly once, at pair
        time, so what is deleted here cannot be fetched again by pairing.
        """
        out: dict[str, Any] = {"status": "logged_out"}
        if self._client is not None:
            try:
                await self._client.logout()
            except Exception as exc:
                # An unlink that fails still has to clear local state, or the
                # data survives a logout the user believes succeeded.
                log.warning("unlink failed, clearing local state anyway: %s", exc)
                out["unlink_error"] = str(exc)
        self.sync.logged_out()
        self.self_jid = ""
        self.push_name = ""

        if purge:
            out["deleted"] = await self.store.purge()
            out["session_cleared"] = self._clear_session()
            self.contacts._names = {}
        return out

    def _clear_session(self) -> bool:
        """Remove whatsmeow's own store, so no device identity is left behind.

        Only when it is a file. On Postgres the session lives in tables this
        process does not own the schema of, and dropping them is not ours to
        do — the unlink above is what matters there.
        """
        from pathlib import Path

        if not self.session_is_file:
            return False
        removed = False
        base = Path(self.session_dsn)
        for p in (base, Path(str(base) + "-wal"), Path(str(base) + "-shm")):
            try:
                if p.exists():
                    p.unlink()
                    removed = True
            except Exception as exc:
                log.warning("could not remove %s: %s", p, exc)
        return removed

    async def _settle_loop(self) -> None:
        """Drives SyncTracker.tick(), which closes a sync that never completed."""
        while not self._stopped.is_set():
            was_ready = self.sync.state.ready
            self.sync.tick()
            if self.sync.state.ready and not was_ready:
                # One last read once everything has landed.
                try:
                    log.info("contacts: %d known after sync",
                             await self.contacts.load())
                    await self.persist_contact_names()
                    fixed = await self.store.rebuild_rollups()
                    if fixed:
                        log.info("repaired %d chat orderings", fixed)
                    await self.propagate_read_status()
                except Exception:
                    log.exception("post-sync repair failed")
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
        self._pairing = False
        self.qr = None
        if not self.self_jid:
            try:
                me = await self._client.get_me()
                self.self_jid = J.from_obj(getattr(me, "JID", None))
                self.push_name = getattr(me, "PushName", "") or ""
            except Exception:
                pass
        if not self.push_name:
            # The device row carries it even when the contact lookup does not.
            # Without a name the prompt reads "answering on behalf of me", so
            # the model has no idea who it is and copies its tone — and its form
            # of address — straight out of the other person's messages.
            self.push_name = self._own_push_name()
        loaded = await self.contacts.load()
        log.info("connected as %s (%d contacts)", self.self_jid or "?", loaded)
        if loaded:
            await self.persist_contact_names()
        # Group names arrive ONLY via group events, which do not always fire.
        # Asking outright is the difference between a readable chat list and 31
        # rows of numeric ids.
        self._tasks.append(asyncio.create_task(self._backfill_group_names()))

    async def _on_paired(self, ev) -> None:
        jid = J.from_obj(getattr(ev, "ID", None))
        if jid:
            self.self_jid = jid
            self.qr = None
            self._pairing = False
            log.info("paired as %s", jid)
            await self._emit("connection.paired", {"phone_jid": jid})

    async def _on_history(self, ev) -> None:
        """Ingest a history chunk: past conversations and their messages.

        Without this a fresh pairing shows a chat list and empty conversations
        until someone happens to write to you — no recents, no ordering, no
        pinned state, because every one of those comes from a message we never
        stored. Live traffic alone cannot reconstruct it: WhatsApp sends your
        history exactly once, here.
        """
        data = getattr(ev, "Data", None)
        if data is None:
            return
        st = getattr(data, "syncType", None)
        try:
            name = data.DESCRIPTOR.fields_by_name["syncType"].enum_type.values_by_number[st].name
        except Exception:
            name = str(st or "")

        conversations = list(getattr(data, "conversations", None) or [])
        self.sync.history_chunk(name, getattr(data, "progress", None), len(conversations))

        stored = 0
        for conv in conversations:
            try:
                stored += await self._ingest_conversation(conv)
            except Exception:
                # One malformed conversation must not abandon the rest of the
                # chunk — this is the only delivery of that history.
                log.exception("history conversation failed")
        if stored:
            log.info("history: %d messages from %d conversations (%s)",
                     stored, len(conversations), name.lower())
            await self.store.rebuild_rollups()
        # whatsmeow fills its contact store while history streams in, so the
        # book has to be re-read as it goes or every chat stays a phone number.
        before = len(self.contacts)
        after = await self.contacts.refresh_if_stale()
        if after > before:
            log.info("contacts: %d known", after)
            await self.persist_contact_names()

    async def _ingest_conversation(self, conv) -> int:
        chat = await self._resolver.canonical(J.normalise(getattr(conv, "ID", "") or ""))
        if not chat or J.is_ignorable(chat):
            return 0

        await self.store.upsert_chat_meta(
            chat,
            name=(getattr(conv, "name", "") or getattr(conv, "displayName", "")) or None,
            is_group=J.is_group(chat),
            archived=bool(getattr(conv, "archived", False)),
            pinned=bool(getattr(conv, "pinned", 0)),
        )

        newest_ts, newest_preview = 0, None
        count = 0
        for item in list(getattr(conv, "messages", None) or []):
            info = getattr(item, "message", None)
            if info is None:
                continue
            key = getattr(info, "key", None)
            body = getattr(info, "message", None)
            if key is None or body is None:
                continue
            if not extract.is_content_message(body):
                continue

            msg_type = extract.message_type(body)
            if msg_type in ("edit", "revoke"):
                continue
            text = extract.extract_text(body) or None
            media = extract.media_descriptor(body)
            ts = to_ms(getattr(info, "messageTimestamp", 0))
            from_me = bool(getattr(key, "fromMe", False))
            sender = J.normalise(getattr(info, "participant", "") or
                                 (self.self_jid if from_me else chat))

            # History carries the real delivery state. Defaulting it to "sent"
            # puts a single tick on a message that was read a year ago, which
            # is worse than showing nothing: it is confidently wrong, and an
            # agent deciding whether to follow up would act on it.
            wa_status = _enum_name(info, "status")
            status = {"DELIVERY_ACK": "delivered", "READ": "read",
                      "PLAYED": "played"}.get(wa_status, "sent")

            if await self.store.upsert_message(Message(
                message_id=getattr(key, "ID", "") or getattr(key, "id", ""),
                chat_jid=chat, sender_jid=sender or None,
                sender_name=(getattr(info, "pushName", "") or "").strip() or None,
                is_from_me=from_me, ts=ts, type=msg_type, text=text,
                media_meta=media or {},
                quoted_id=extract.quoted_message_id(body),
                status=status if from_me else "sent",
                raw_proto=(body.SerializeToString()
                           if (self.settings.store_raw_proto or media) else None),
            )):
                count += 1
            if ts > newest_ts:
                newest_ts = ts
                newest_preview = text or (f"[{msg_type}]" if msg_type != "text" else None)

        # One rollup per conversation rather than per message: touch_chat
        # increments unread, and replaying history through it would invent an
        # unread count of several hundred for a chat you have already read.
        if newest_ts:
            await self.store.touch_chat(chat, newest_ts, True, newest_preview)
        unread = int(getattr(conv, "unreadCount", 0) or 0)
        await self.store.set_unread(chat, unread)
        return count

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

    async def propagate_read_status(self) -> int:
        """Mark everything before a read message in the same chat as read.

        Not a guess: opening a conversation marks it read up to that point, so
        a later message being READ means every earlier one was too. History
        arrives without per-message status for older entries, which left a
        year of messages showing one grey tick beside a reply that is plainly
        marked read — visibly wrong, and misleading to anything deciding
        whether to follow up.

        Written against the port rather than as three dialects of SQL: it runs
        once after a sync, so clarity beats a marginal amount of speed.
        """
        fixed = 0
        for chat in await self.store.list_chats(limit=5000):
            msgs = await self.store.get_messages(chat.chat_jid, limit=500)
            mine = [m for m in msgs if m.is_from_me]
            newest_read = max((m.ts for m in mine if m.status in ("read", "played")),
                              default=None)
            if newest_read is None:
                continue
            behind = [m.message_id for m in mine
                      if m.ts < newest_read and m.status in ("sent", "delivered")]
            if behind:
                fixed += len(await self.store.set_status(behind, "read", newest_read))
        if fixed:
            log.info("inferred read status for %d earlier messages", fixed)
        return fixed

    def _own_push_name(self) -> str:
        """Our own display name, read from whatsmeow's device row."""
        import sqlite3

        try:
            db = sqlite3.connect(f"file:{self.session_dsn}?mode=ro", uri=True,
                                 timeout=3)
            try:
                row = db.execute(
                    "SELECT push_name FROM whatsmeow_device "
                    "WHERE push_name IS NOT NULL AND push_name <> '' LIMIT 1"
                ).fetchone()
            finally:
                db.close()
            return (row[0] if row else "") or ""
        except Exception as exc:
            log.debug("own push name unavailable: %s", exc)
            return ""

    async def persist_contact_names(self) -> int:
        """Write address-book names into chats.name.

        The contact book lives in memory, so a WHERE clause could never see it
        — searching "jyotish" returned nothing while the list plainly showed
        "Vja Jyotish". Rather than filter in Python and have two answers to the
        same question, the resolved name is written down, and SQLite becomes
        the single source of truth for search.

        Only fills blanks. A name that arrived from WhatsApp itself, or that a
        user set, outranks the address book and is left alone.
        """
        written = 0
        for chat in await self.store.list_chats(limit=5000):
            if chat.name:
                continue
            name = self.contacts.get(chat.chat_jid)
            if name:
                await self.store.upsert_chat_meta(chat.chat_jid, name=name)
                written += 1
        if written:
            log.info("named %d chats from the address book", written)
        return written

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
        preview = extract.extract_text(body) or (f"[{msg_type}]" if msg_type != "text" else None)
        # The rollup is refreshed even for a message we already had. A redelivery
        # still tells us the chat is active, and returning early here is how 35
        # conversations ended up holding today's messages behind a NULL
        # timestamp — sorted to the bottom of the list. `from_me=True` on the
        # duplicate path so the unread count is not incremented twice.
        await self.store.touch_chat(chat, ts, bool(src.IsFromMe) or not stored, preview)
        if not stored:
            return
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
        ids = [m for m in (getattr(ev, "MessageIDs", None) or [])]
        log.debug("receipt %s for %d id(s)", kind or "<blank>", len(ids))

        # Delivery status. This is what lets an agent act on "they read it" —
        # a message arriving is one signal, a message being READ is a different
        # and often more useful one.
        status = {"DELIVERED": "delivered", "READ": "read",
                  "READ_SELF": "read", "PLAYED": "played",
                  "PLAYED_SELF": "played"}.get(kind)
        if status and ids:
            ts = to_ms(getattr(ev, "Timestamp", 0))
            moved = await self.store.set_status(ids, status, ts)
            if moved:
                log.info("%s: %d message(s)", status, len(moved))
                # One event per real transition, not per receipt: WhatsApp
                # resends receipts, and an agent setting a reminder on "read"
                # must not get three.
                await self._emit(f"message.{status}", {
                    "message_ids": moved,
                    "chat_jid": await self._resolver.canonical(
                        J.from_obj(getattr(getattr(ev, "MessageSource", None),
                                           "Chat", None))),
                    "status": status, "ts": ts,
                })
            return

        if kind != "RETRY":
            return
        for mid in ids:
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

    async def profile(self, chat_jid: str) -> dict[str, Any]:
        """Whatever WhatsApp will say about someone.

        Separate from the contact book, which holds the name YOU saved. This is
        what they publish: a business name if the account is verified, how many
        devices they have linked, and a photo.

        `about` is in the protobuf and comes back empty for every contact tried
        on a live account — whether whatsmeow does not populate it, or the
        server withholds it, is not something this layer can tell. It is
        returned as it arrives rather than dropped, so it starts working if
        that changes upstream.

        One call for one person. get_user_info takes several JIDs, but the
        result is keyed by a JID protobuf rather than a string, so unpicking a
        batch is fiddler than it is worth for a per-contact lookup.
        """
        self._guard()
        jid = self._jid(chat_jid)
        out: dict[str, Any] = {"chat_jid": J.normalise(chat_jid)}
        try:
            infos = await self._client.get_user_info(jid)
        except Exception as exc:
            raise SendFailed(f"profile lookup failed: {exc}") from exc

        for entry in infos or []:
            info = getattr(entry, "UserInfo", None)
            if info is None:
                continue
            out["about"] = getattr(info, "Status", "") or ""
            verified = getattr(info, "VerifiedName", None)
            # A business account carries its verified name in a nested
            # certificate; an ordinary one has the field but nothing in it.
            name = getattr(getattr(verified, "Details", None), "verifiedName", "")
            if name:
                out["business_name"] = name
            out["devices"] = len(getattr(info, "Devices", []) or [])
            break

        out["name"] = self.contacts.display_name(chat_jid)
        try:
            pic = await self._client.get_profile_picture(jid)
            out["picture_url"] = getattr(pic, "URL", "") or ""
        except Exception:
            # Common and not an error: plenty of people have no photo, or
            # restrict it to contacts.
            out["picture_url"] = ""
        return out

    async def avatar_url(self, chat_jid: str) -> str | None:
        """The profile picture URL for a chat, or None if there is not one.

        Asked of WhatsApp per chat rather than bulk-fetched: most chats in a
        list are never opened, and a request per contact at startup is both slow
        and exactly the traffic pattern that draws rate limiting.
        """
        if self._client is None:
            return None
        try:
            info = await self._client.get_profile_picture(self._jid(chat_jid))
        except Exception as exc:
            log.debug("no avatar for %s: %s", chat_jid, exc)
            return None
        return getattr(info, "URL", None) or getattr(info, "url", None) or None

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
