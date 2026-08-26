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

    def __init__(self, session_dsn: str, is_file: bool = True):
        self._dsn = session_dsn
        self._is_file = is_file
        self._names: dict[str, str] = {}
        self.loaded = False
        self.error: str | None = None
        self.loaded_at = 0.0

    async def load(self) -> int:
        try:
            rows = await (self._load_sqlite() if self._is_file else self._load_postgres())
        except Exception as exc:
            self.error = f"{type(exc).__name__}: {exc}"
            log.debug("contact book unavailable: %s", self.error)
            return 0

        names: dict[str, str] = {}
        for their_jid, full, first, push, business in rows:
            name = _first_nonempty(full, first, push, business)
            if not name:
                continue
            names[J.normalise(their_jid or "")] = name.strip()
        if names or not self._names:
            self._names = names
        self.loaded = bool(self._names)
        self.loaded_at = time.monotonic()
        self.error = None
        return len(names)

    async def refresh_if_stale(self, max_age: float = 30.0) -> int:
        if self._names and (time.monotonic() - self.loaded_at) < max_age:
            return len(self._names)
        return await self.load()

    def __len__(self) -> int:
        return len(self._names)

    def search(self, query: str, limit: int = 20) -> list[tuple[str, str]]:
        q = (query or "").strip().lower()
        if not q:
            return []
        exact = [(j, n) for j, n in self._names.items() if n.lower() == q]
        if exact:
            return exact[:limit]
        return [(j, n) for j, n in self._names.items() if q in n.lower()][:limit]

    def get(self, chat_jid: str) -> str | None:
        return self._names.get(J.normalise(chat_jid))

    def display_name(self, chat_jid: str, *, chat_name: str | None = None,
                     push_name: str | None = None) -> str:
        for candidate in (chat_name if not is_placeholder(chat_name) else None,
                          self.get(chat_jid), push_name, chat_name):
            if candidate and candidate.strip():
                return candidate.strip()
        phone = J.phone(chat_jid)
        return phone or chat_jid


    async def _load_sqlite(self):
        import aiosqlite

        path = Path(self._dsn)
        if not path.exists():
            raise FileNotFoundError(path)
        async with aiosqlite.connect(f"file:{path}?mode=ro", uri=True, timeout=5) as db:
            return await (await db.execute(_SQL)).fetchall()

    async def _load_postgres(self):
        import asyncpg

        dsn = self._dsn.replace("postgres://", "postgresql://", 1).split("?")[0]
        conn = await asyncpg.connect(dsn, timeout=5)
        try:
            return [tuple(r) for r in await conn.fetch(_SQL)]
        finally:
            await conn.close()


_MASK_CHARS = "\u2219\u2022\u00b7\u2027"


def is_placeholder(name: str | None) -> bool:
    n = (name or "").strip()
    if not n:
        return True
    if any(ch in n for ch in _MASK_CHARS):
        return True
    return n.lstrip("+").replace(" ", "").isdigit()


def _first_nonempty(*values: str | None) -> str:
    for v in values:
        if v and v.strip():
            return v
    return ""
