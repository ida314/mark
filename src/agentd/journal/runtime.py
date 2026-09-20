"""How the runtime reaches the journal: one writer per process, one handle per run.

Two things live here and they answer two different questions.

`get_writer(cfg)` answers *who owns the file*. One process, one `JournalWriter`, cached by
resolved path. Not because a singleton is tidy: `seq` is assigned inside the transaction
that reads `MAX(seq)`, so two writers on one file are correct but race for positions, and a
sub-agent's `AgentLoop` - a second `AgentLoop` object inside the same turn - would otherwise
open a second store and lose the race against its own caller. One writer per file also means
one lock, so ordering inside a process is call order, as `writer.py` promises.

`RunJournal` answers *what a caller is allowed to write*. It binds a `run_id` (and, for a
worker, a `worker_id`) and validates every payload against `events.EVENTS` before it goes
near the disk. This is the door the runtime uses; `JournalWriter.append` stays usable
directly for durability tests that deliberately write nonsense payloads.
"""

from __future__ import annotations

import atexit
import threading
from pathlib import Path
from typing import Any

from ..config import Config, get_config
from . import events as vocab
from .store import Event, default_path
from .writer import JournalWriter

_lock = threading.Lock()
_writer: JournalWriter | None = None
_writer_path: Path | None = None


def get_writer(cfg: Config | None = None) -> JournalWriter:
    """The process's journal writer, opening it on first use.

    Cached by path rather than unconditionally: the test suite points each test at its own
    `data_dir`, and a writer cached without regard to path would write the second test's
    events into the first test's file. Switching paths closes the old writer, which flushes
    its tail.
    """
    global _writer, _writer_path
    cfg = cfg or get_config()
    path = default_path(cfg).resolve()
    with _lock:
        if _writer is not None and _writer_path == path:
            return _writer
        if _writer is not None:
            _writer.close()
        _writer = JournalWriter.open(cfg, path=path)
        _writer_path = path
        return _writer


def close_writer() -> None:
    """Flush and close. Registered with atexit, and safe to call twice."""
    global _writer, _writer_path
    with _lock:
        writer, _writer, _writer_path = _writer, None, None
    if writer is not None:
        writer.close()


atexit.register(close_writer)


class RunJournal:
    """One run's view of the journal. Every runtime event goes through here.

    Cheap to construct and not thread-affine: the writer underneath owns the lock. A worker
    gets one of these from `for_worker`, which keeps the run and changes the tag, because a
    worker's events belong to the run that created it.
    """

    def __init__(
        self, writer: JournalWriter, run_id: str, *, worker_id: str | None = None
    ) -> None:
        self.writer = writer
        self.run_id = run_id
        self.worker_id = worker_id

    @classmethod
    def open(
        cls, run_id: str, *, cfg: Config | None = None, worker_id: str | None = None
    ) -> RunJournal:
        return cls(get_writer(cfg), run_id, worker_id=worker_id)

    def for_worker(self, worker_id: str) -> RunJournal:
        return RunJournal(self.writer, self.run_id, worker_id=worker_id)

    def step_id(self, step: int) -> str:
        return vocab.step_id(step, worker_id=self.worker_id)

    def emit(
        self,
        event_type: str,
        payload: dict[str, Any] | None = None,
        *,
        step_id: str | None = None,
        worker_id: str | None = None,
        sync: bool | None = None,
    ) -> Event | None:
        """Validate and append one event.

        Raises `UnknownEventType` or `EventSchemaError` rather than writing something the
        fold has no case for, and does not swallow a write failure - see `writer.py` on why
        the journal is the one place in this codebase that fails loudly.

        Returns the written `Event` when it reached disk during this call and `None` when it
        was buffered.
        """
        body = dict(payload or {})
        worker = worker_id or self.worker_id
        # Validated as the reader will see it: `worker_id` and `step_id` are payload keys on
        # the way out, even though they travel as named arguments so that promoting either
        # to a column stays a one-module change.
        merged = dict(body)
        if worker is not None:
            merged["worker_id"] = worker
        if step_id is not None:
            merged["step_id"] = step_id
        vocab.validate_payload(event_type, merged)
        return self.writer.append(
            self.run_id, event_type, body, worker_id=worker, step_id=step_id, sync=sync
        )


__all__ = ["RunJournal", "close_writer", "get_writer"]
