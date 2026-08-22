"""Display names — reading the address book whatsmeow already has.

The problem this solves, measured on a live account: **0 of 100 chats had a
name**, so every conversation rendered as a raw JID. Meanwhile whatsmeow's own
`whatsmeow_contacts` table held 4,092 contacts, 1,812 with a full name — enough
to name 68 of the 72 personal chats. The data was there; nothing read it.

neonize exposes no contact-store accessor, so this reads the table directly.
That is crossing into Go's schema, which we otherwise avoid, so it is done
defensively: read-only, cached, and every failure degrades to the next name
source rather than propagating. The fallback chain is what makes that safe —
even if a whatsmeow upgrade renames the table, active conversations stay named
because `push_name` arrives on every incoming message.

    1. chats.name          group names from events, or a user override
    2. whatsmeow_contacts  full_name -> first_name -> push_name -> business_name
    3. messages.sender_name  PushName, which covers strangers and schema drift
    4. the phone number
    5. the raw JID
"""
from __future__ import annotations

import logging
import time
from pathlib import Path

from . import jid as J

log = logging.getLogger(__name__)

_SQL = """
SELECT their_jid, full_name, first_name, push_name, business_name
FROM whatsmeow_contacts
"""


class ContactBook:
    """Best-effort name lookup over whatsmeow's contact store.

    Loaded once and refreshed on demand rather than queried per message: 4,000
    rows is a few hundred kilobytes, and a per-message query against another
    process's database during history sync would be thousands of round-trips
    for data that barely changes.
    """

    def __init__(self, session_dsn: str, is_file: bool = True):
        self._dsn = session_dsn
        self._is_file = is_file
        self._names: dict[str, str] = {}
        self.loaded = False
        self.error: str | None = None
        self.loaded_at = 0.0

    async def load(self) -> int:
        """Populate the cache. Never raises — a missing address book is survivable."""
        try:
            rows = await (self._load_sqlite() if self._is_file else self._load_postgres())
        except Exception as exc:
            # Expected on a fresh install: the table does not exist until
            # whatsmeow has connected once.
            self.error = f"{type(exc).__name__}: {exc}"
            log.debug("contact book unavailable: %s", self.error)
            return 0

        names: dict[str, str] = {}
        for their_jid, full, first, push, business in rows:
            name = _first_nonempty(full, first, push, business)
            if not name:
                continue
            # Contacts are stored per-device; normalise so a lookup by chat jid
            # hits regardless of which device the row came from.
            names[J.normalise(their_jid or "")] = name.strip()
        # Never replace a populated book with an empty one. whatsmeow writes
        # contacts asynchronously during history sync, so a reload landing at
        # the wrong moment can read zero rows, and losing the names we already
        # had would be worse than keeping slightly stale ones.
        if names or not self._names:
            self._names = names
        self.loaded = bool(self._names)
        self.loaded_at = time.monotonic()
        self.error = None
        return len(names)

    async def refresh_if_stale(self, max_age: float = 30.0) -> int:
        """Reload when the book is empty or old.

        The first load happens on ConnectedEv, which is BEFORE history sync
        populates whatsmeow's contact store — measured at 0 contacts on connect
        and 8,478 a minute later. Loading once means every chat renders as a
        phone number forever.
        """
        if self._names and (time.monotonic() - self.loaded_at) < max_age:
            return len(self._names)
        return await self.load()

    def __len__(self) -> int:
        return len(self._names)

    def get(self, chat_jid: str) -> str | None:
        return self._names.get(J.normalise(chat_jid))

    def display_name(self, chat_jid: str, *, chat_name: str | None = None,
                     push_name: str | None = None) -> str:
        """The full fallback chain, in one place so every caller agrees."""
        for candidate in (chat_name, self.get(chat_jid), push_name):
            if candidate and candidate.strip():
                return candidate.strip()
        phone = J.phone(chat_jid)
        return phone or chat_jid

    # ------------------------------------------------------------- backends

    async def _load_sqlite(self):
        import aiosqlite

        path = Path(self._dsn)
        if not path.exists():
            raise FileNotFoundError(path)
        # mode=ro so we can never write to, lock, or migrate a database that
        # belongs to the Go layer. WAL allows concurrent readers, so this is
        # safe while the socket is live.
        async with aiosqlite.connect(f"file:{path}?mode=ro", uri=True, timeout=5) as db:
            return await (await db.execute(_SQL)).fetchall()

    async def _load_postgres(self):
        import asyncpg  # optional dependency, only needed on the postgres path

        dsn = self._dsn.replace("postgres://", "postgresql://", 1).split("?")[0]
        conn = await asyncpg.connect(dsn, timeout=5)
        try:
            return [tuple(r) for r in await conn.fetch(_SQL)]
        finally:
            await conn.close()


def _first_nonempty(*values: str | None) -> str:
    for v in values:
        if v and v.strip():
            return v
    return ""
