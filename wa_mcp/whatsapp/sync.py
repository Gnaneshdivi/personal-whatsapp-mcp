from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum


class Phase(str, Enum):
    UNPAIRED = "unpaired"
    PAIRING = "pairing"
    CONNECTING = "connecting"
    SYNCING = "syncing"
    READY = "ready"
    LOGGED_OUT = "logged_out"


SETTLE_SECONDS = 90.0


@dataclass
class SyncState:
    phase: Phase = Phase.UNPAIRED
    total: int = 0
    done: int = 0
    history_type: str = ""
    history_percent: float = 0.0
    chats_synced: int = 0
    started_at: float = 0.0
    detail: str = ""

    @property
    def percent(self) -> float:
        if self.phase is Phase.READY:
            return 100.0
        if self.total > 0:
            return min(99.0, round(100.0 * self.done / self.total, 1))
        if self.history_percent:
            return min(99.0, round(self.history_percent, 1))
        return 0.0

    @property
    def ready(self) -> bool:
        return self.phase is Phase.READY

    def public(self) -> dict:
        return {
            "phase": self.phase.value,
            "ready": self.ready,
            "percent": self.percent,
            "done": self.done,
            "total": self.total,
            "chats_synced": self.chats_synced,
            "history_type": self.history_type,
            "detail": self.detail,
            "elapsed_seconds": round(time.monotonic() - self.started_at, 1)
            if self.started_at else 0.0,
        }


@dataclass
class SyncTracker:

    state: SyncState = field(default_factory=SyncState)
    _clock: object = time.monotonic


    def unpaired(self) -> None:
        self.state = SyncState(phase=Phase.UNPAIRED)

    def pairing(self) -> None:
        self.state = SyncState(phase=Phase.PAIRING)

    def connecting(self) -> None:
        self.state = SyncState(phase=Phase.CONNECTING, started_at=self._now())

    def logged_out(self) -> None:
        self.state = SyncState(phase=Phase.LOGGED_OUT, detail="device unlinked")

    def connected(self) -> None:
        if self.state.phase in (Phase.READY, Phase.SYNCING):
            return
        self.state.phase = Phase.SYNCING
        self.state.started_at = self.state.started_at or self._now()
        self.state.detail = "waiting for the server"


    def offline_preview(self, total: int, **parts: int) -> None:
        self.state.phase = Phase.SYNCING
        self.state.total = max(self.state.total, int(total or 0))
        self.state.started_at = self.state.started_at or self._now()
        bits = ", ".join(f"{k} {v}" for k, v in parts.items() if v)
        self.state.detail = f"queued: {bits}" if bits else "syncing"

    def history_chunk(self, sync_type: str, progress: float | None,
                      conversations: int = 0) -> None:
        self.state.phase = Phase.SYNCING
        self.state.started_at = self.state.started_at or self._now()
        self.state.history_type = sync_type or self.state.history_type
        if progress:
            self.state.history_percent = float(progress)
        self.state.chats_synced += int(conversations or 0)
        self.state.detail = f"history: {sync_type.lower().replace('_', ' ')}"

    def saw_event(self, n: int = 1) -> None:
        if self.state.phase is Phase.SYNCING:
            self.state.done += n

    def offline_completed(self, count: int = 0) -> None:
        self.state.done = max(self.state.done, int(count or 0))
        self._settle("offline queue drained")


    def tick(self) -> None:
        if self.state.phase is Phase.SYNCING and self.state.started_at:
            if self._now() - self.state.started_at > SETTLE_SECONDS:
                self._settle("settled on timeout")

    def _settle(self, detail: str) -> None:
        self.state.phase = Phase.READY
        self.state.detail = detail

    def _now(self) -> float:
        return self._clock()
