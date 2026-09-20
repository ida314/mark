"""The append path: ordering, durability class, and the one place events enter the journal.

Two rules decide everything here.

**Order is call order.** `seq` is handed out inside the commit, but the sequence it follows
is the order `append` was called in this process. A buffered event that is still in memory
when a synchronous event arrives is flushed *in the same transaction, ahead of it*, so no
event is ever journaled out of the order it happened. This is also why there is no
`async def append`: two coroutines handing appends to a thread pool would let the pool
decide the order of the run's history.

**A failed journal write raises.** `obs/telemetry.py` swallows its write failures on
purpose - a measurement that can take a turn down is worse than no measurement. This module
is the opposite and deliberately so. The journal is the source of truth; an `effect_intended`
that silently failed to write is exactly the duplicate-side-effect bug the Pass 1 baseline
caught in the wild (two open loops from one request, one of them with no `actions` row).
A turn that cannot journal must fail loudly rather than proceed unrecorded.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any

from ..config import Config
from ..ids import utcnow
from .store import Event, JournalStore, PendingEvent, default_path

# Written synchronously, no matter what the buffer settings say. Everything else may sit in
# memory briefly.
#
# `effect_*` because Pass 3's ledger is a promise that the record precedes the action: the
# whole point of `effect_intended` is to be on disk *before* the side effect runs, so that a
# crash in between is detectable. `checkpoint_written` because Pass 4's snapshot may lag the
# journal but may never disagree with it, and a checkpoint whose own announcement was lost
# is a snapshot nothing points at.
SYNC_PREFIXES = ("effect_",)
SYNC_TYPES = frozenset({"checkpoint_written"})


def is_synchronous(event_type: str) -> bool:
    return event_type in SYNC_TYPES or event_type.startswith(SYNC_PREFIXES)


class JournalWriter:
    """Serializes this process's appends into one journal file.

    Not a singleton and not global: a caller owns one and closes it. `close()` flushes, so
    anything holding one must close it on the way out or lose the buffered tail.
    """

    def __init__(
        self,
        store: JournalStore,
        *,
        buffering: bool = True,
        buffer_max: int = 32,
        buffer_max_age_s: float = 1.0,
    ) -> None:
        self.store = store
        self.buffering = buffering
        self.buffer_max = max(1, buffer_max)
        self.buffer_max_age_s = buffer_max_age_s
        self._buffer: list[PendingEvent] = []
        self._buffered_since: float | None = None
        self._lock = threading.RLock()

    @classmethod
    def open(cls, cfg: Config, *, path: Path | None = None) -> JournalWriter:
        store = JournalStore(
            path or default_path(cfg),
            synchronous=cfg.journal.synchronous,
            busy_timeout_ms=cfg.journal.busy_timeout_ms,
        )
        return cls(
            store,
            buffering=cfg.journal.buffering,
            buffer_max=cfg.journal.buffer_max,
            buffer_max_age_s=cfg.journal.buffer_max_age_s,
        )

    # --- append --------------------------------------------------------------

    def append(
        self,
        run_id: str,
        event_type: str,
        payload: dict[str, Any] | None = None,
        *,
        worker_id: str | None = None,
        step_id: str | None = None,
        ts: str | None = None,
        seq: int | None = None,
        sync: bool | None = None,
    ) -> Event | None:
        """Journal one event.

        Returns the written `Event` when it reached disk during this call, and `None` when
        it was buffered - a buffered event has no `seq` yet, because `seq` is assigned in
        the transaction that commits it and nowhere else.

        `worker_id` and `step_id` are named arguments rather than free payload keys because
        Pass 2b requires them on every event that has them, and naming them here means a
        later decision to promote either to a column does not touch a single call site.
        """
        body = dict(payload or {})
        if worker_id is not None:
            body["worker_id"] = worker_id
        if step_id is not None:
            body["step_id"] = step_id
        pending = PendingEvent(
            run_id=run_id, type=event_type, payload=body,
            ts=ts or utcnow().isoformat(), seq=seq,
        )
        # An explicit seq is an assertion about a position, and the caller gets the answer
        # now rather than at some later flush.
        force = sync if sync is not None else (is_synchronous(event_type) or seq is not None)
        with self._lock:
            if force or not self.buffering:
                written = self.store.append([*self._buffer, pending])
                self._clear()
                return written[-1]
            self._buffer.append(pending)
            if self._buffered_since is None:
                self._buffered_since = time.monotonic()
            if self._should_flush():
                self._flush_locked()
            return None

    def _should_flush(self) -> bool:
        if len(self._buffer) >= self.buffer_max:
            return True
        since = self._buffered_since
        return since is not None and (time.monotonic() - since) >= self.buffer_max_age_s

    def _clear(self) -> None:
        self._buffer = []
        self._buffered_since = None

    def _flush_locked(self) -> int:
        if not self._buffer:
            return 0
        batch = self._buffer
        # Cleared before the append, not after: if the write fails the caller gets the
        # exception, and retrying the same batch on the next append would journal events
        # the caller was already told were lost.
        self._clear()
        self.store.append(batch)
        return len(batch)

    def flush(self) -> int:
        """Force the buffered tail to disk. Returns how many events were written."""
        with self._lock:
            return self._flush_locked()

    @property
    def pending(self) -> int:
        """Events accepted by this process but not yet on disk."""
        with self._lock:
            return len(self._buffer)

    def close(self) -> None:
        with self._lock:
            try:
                self._flush_locked()
            finally:
                self.store.close()

    def __enter__(self) -> JournalWriter:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
