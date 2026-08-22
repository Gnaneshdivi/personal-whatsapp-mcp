"""The single process: storage, socket and settings, wired together once.

Everything that used to be spread across a worker, a gateway and an MCP replica
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
        self._subscribers: list = []

    # ------------------------------------------------------------- lifecycle

    async def start(self) -> None:
        await self.store.connect()
        from .whatsapp.client import WhatsApp

        self.wa = WhatsApp(
            session_dsn=self.storage.session_dsn,
            store=self.store,
            settings=self.settings,
            contacts=self.contacts,
            on_event=self._fanout,
        )
        connected = await self.wa.start()
        if connected:
            log.info("connecting existing session")
        else:
            log.info("no number linked yet — open the web UI to pair")

    async def stop(self) -> None:
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
        for q in list(self._subscribers):
            try:
                q.put_nowait({"type": event_type, **payload})
            except asyncio.QueueFull:
                try:
                    q.get_nowait()
                    q.put_nowait({"type": event_type, **payload})
                except Exception:
                    pass

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
            "contacts_loaded": self.contacts.loaded,
        }
