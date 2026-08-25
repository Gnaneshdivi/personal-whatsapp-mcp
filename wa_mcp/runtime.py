"""The single process: storage, socket and settings, wired together once.

Everything that a multi-process design would spread across a worker and a gateway
lives here. There is one of these per process and it is created at startup, so
nothing below ever has to ask "is the client up yet" — it either is, or the
phase says why not.
"""
from __future__ import annotations

import asyncio
import logging

from .config import Settings, Storage, resolve_storage
from .store.base import Store
from .whatsapp.contacts import ContactBook
from .whatsapp.sync import Phase

log = logging.getLogger(__name__)


def build_store(storage: Storage) -> Store:
    """Pick an adapter. Import inside the branch so an unused backend's driver
    never has to be installed — `pip install suprai-whatsapp-mcp` pulls no
    database client at all."""
    if storage.backend == "sqlite":
        from .store.sqlite import SQLiteStore
        return SQLiteStore(storage.app_url.split("///", 1)[1])
    if storage.backend == "postgres":
        try:
            from .store.postgres import PostgresStore
        except ImportError as exc:
            raise SystemExit(
                "WA_DATABASE_URL points at Postgres but the driver is missing.\n"
                "  pip install 'suprai-whatsapp-mcp[postgres]'"
            ) from exc
        return PostgresStore(storage.app_url)
    if storage.backend == "mongo":
        try:
            from .store.mongo import MongoStore
        except ImportError as exc:
            raise SystemExit(
                "WA_DATABASE_URL points at Mongo but the driver is missing.\n"
                "  pip install 'suprai-whatsapp-mcp[mongo]'"
            ) from exc
        return MongoStore(storage.app_url)
    raise SystemExit(f"unknown storage backend {storage.backend!r}")


class Runtime:
    def __init__(self, settings: Settings | None = None, storage: Storage | None = None):
        self.settings = settings or Settings.from_env()
        self.storage = storage or resolve_storage()
        self.store: Store = build_store(self.storage)
        self.contacts = ContactBook(self.storage.session_dsn, self.storage.session_is_file)
        self.wa = None            # built lazily: importing neonize loads 21MB of Go
        self.trigger = None       # built in start(), needs the store connected
        self._subscribers: list = []
        self._have_history = False   # set in start(); see status()
        self._summary_task = None

    # ------------------------------------------------------------- lifecycle

    async def start(self) -> None:
        await self.store.connect()
        # One row is enough to answer "is this a first pair or a restart?".
        self._have_history = bool(await self.store.list_chats(limit=1))

        from .trigger.engine import TriggerEngine

        self.trigger = TriggerEngine(self)
        settings = await self.trigger.load()
        if settings.enabled:
            ok, why = settings.ready()
            log.info("auto-reply enabled" if ok else "auto-reply enabled but idle: %s", why)

        from .trigger.summary import loop as summary_loop

        self._summary_task = asyncio.create_task(summary_loop(self))

        from .whatsapp.client import WhatsApp

        self.wa = WhatsApp(
            session_dsn=self.storage.session_dsn,
            store=self.store,
            settings=self.settings,
            contacts=self.contacts,
            session_is_file=self.storage.session_is_file,
            on_event=self._fanout,
        )
        blocked = self.wa.preflight()
        if blocked:
            # Loud, and at startup — not at the moment someone is staring at a
            # spinner wondering why no QR appeared.
            for line in blocked.splitlines():
                log.error("%s", line)

        connected = await self.wa.start()
        if connected:
            log.info("connecting existing session")
        else:
            log.info("no number linked yet — open the web UI to pair")

    async def stop(self) -> None:
        # Cancelled before the store closes, or its next tick reads a closed
        # connection and logs an error on every shutdown.
        if self._summary_task is not None:
            self._summary_task.cancel()
            try:
                await self._summary_task
            except (asyncio.CancelledError, Exception):
                pass
            self._summary_task = None
        if self.wa is not None:
            await self.wa.stop()
        await self.store.close()

    # ------------------------------------------------------------- events

    def subscribe(self) -> asyncio.Queue:
        """A queue per SSE client. Bounded, and drops oldest when a slow browser
        tab stops draining — a stalled reader must not grow memory without limit."""
        q: asyncio.Queue = asyncio.Queue(maxsize=200)
        self._subscribers.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        if q in self._subscribers:
            self._subscribers.remove(q)

    async def _fanout(self, event_type: str, payload: dict) -> None:
        if event_type in ("message.received", "message.sent"):
            # A first pair stops being one the moment anything lands.
            self._have_history = True
        if event_type == "message.received":
            await self._maybe_reply(payload)
        for q in list(self._subscribers):
            try:
                q.put_nowait({"type": event_type, **payload})
            except asyncio.QueueFull:
                try:
                    q.get_nowait()
                    q.put_nowait({"type": event_type, **payload})
                except Exception:
                    pass

    async def _maybe_reply(self, payload: dict) -> None:
        """Hand an inbound message to the reply engine.

        Guarded and awaited rather than fired into a task: the engine already
        holds the cooldown and hourly cap, and running concurrently would let a
        burst slip past both before either was recorded.
        """
        if self.trigger is None:
            return
        from .trigger.engine import Inbound
        from .whatsapp import jid as J

        try:
            await self.trigger.consider(Inbound(
                chat_jid=payload.get("chat_jid", ""),
                message_id=payload.get("message_id", ""),
                sender_jid=payload.get("sender_jid", ""),
                text=payload.get("text") or "",
                is_from_me=bool(payload.get("from_me")),
                is_group=J.is_group(payload.get("chat_jid", "")),
                mentioned_me=bool(payload.get("mentioned_me")),
            ))
        except Exception:
            log.exception("reply engine failed")

    # ------------------------------------------------------------- status

    def status(self) -> dict:
        sync = self.wa.sync.state.public() if self.wa else {"phase": Phase.UNPAIRED.value,
                                                            "ready": False, "percent": 0.0}
        return {
            "phase": sync["phase"],
            "ready": sync["ready"],
            "sync": sync,
            "number": (self.wa.self_jid.split("@")[0] if self.wa and self.wa.self_jid else None),
            "push_name": getattr(self.wa, "push_name", "") if self.wa else "",
            "storage": {
                "backend": self.storage.backend,
                "session_persisted_as_file": self.storage.session_is_file,
            },
            # Whether the browser has anything to show yet, which is NOT the
            # same question as whether the auto-reply gate has opened. History
            # arrives once, at pair time; every later connect re-reads a store
            # that is already full. Blocking the UI behind the 90s settle on
            # those connects showed a "Syncing" bar over a complete, usable
            # chat list for a minute and a half after every restart.
            "have_local_history": self._have_history,
            "contacts_loaded": self.contacts.loaded,
            "contacts_known": len(self.contacts),
            "blocked": getattr(self.wa, "load_error", None) if self.wa else None,
            "auto_reply": self._auto_reply_status(),
        }

    def _auto_reply_status(self) -> dict:
        if self.trigger is None:
            return {"enabled": False, "active": False, "reason": "starting"}
        ok, why = self.trigger.settings.ready()
        return {
            "enabled": self.trigger.settings.enabled,
            "backend": self.trigger.settings.backend,
            "active": ok and bool(self.wa and self.wa.sync.state.ready),
            "reason": why or ("" if self.wa and self.wa.sync.state.ready
                              else "waiting for sync"),
        }
