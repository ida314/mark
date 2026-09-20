"""Subscribing to the journal: one cursor, one delivery mechanism, no queue.

Session 2c's job was to give the frontend a feed and to delete the in-process event bus
that used to carry the same facts. What makes that safe is that there is nothing to lose
here: a subscriber holds an integer, and everything it renders it read from the file. There
is no fan-out list, no in-memory ring, and nothing is delivered that is not already on
disk - so a subscriber that dies and comes back with its last id is in exactly the state it
was in, and a subscriber that has never connected is in exactly the state of one that
reconnected.

Two things follow from that and both are deliberate.

**A local subscriber flushes the writer before it reads.** The writer buffers the chatty
event types and the age check runs on append rather than on a timer (2a), so a process that
goes quiet mid-turn - which is exactly what a process does while a tool runs for thirty
seconds - would hold `tool_started` in memory and the feed would appear to stall at the
moment the user most wants to see something. `drain()` on a tail that owns a writer flushes
it first. The poll interval is therefore also the buffer's real age bound, and the fsync it
costs happens at most once per interval.

**`drain()` is synchronous and deterministic.** A caller that is itself driving the turn -
`subagents.py` interleaving a worker's tool failures into its transcript - calls `drain()`
between yields and gets everything the turn has emitted up to that point, in order, with no
dependence on a timer having fired. `follow()` is `drain()` in a loop for callers that need
liveness while nothing is yielding.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from pathlib import Path

from ..config import Config, get_config
from .store import Event, JournalStore, default_path
from .writer import JournalWriter

# 50ms is below the threshold where a status line reads as laggy, and it bounds how long a
# buffered event can sit in memory while somebody is watching. It is not a latency budget
# for the runtime: nothing in the turn loop waits on it.
POLL_INTERVAL_S = 0.05


class JournalTail:
    """A cursor over the journal, optionally narrowed to one run or one worker.

    `last_id` is the whole of the subscriber's state. Hand it back to `since=` after a
    disconnect and the feed resumes with no gap and no duplicate, which is the property the
    id column exists for (`store.py`: rowids are reused after a prune, `AUTOINCREMENT` ids
    are not).
    """

    def __init__(
        self,
        store: JournalStore,
        *,
        run_id: str | None = None,
        worker_id: str | None = None,
        since: int = 0,
        writer: JournalWriter | None = None,
    ) -> None:
        self.store = store
        self.run_id = run_id
        self.worker_id = worker_id
        self.writer = writer
        self.last_id = since

    @classmethod
    def on(
        cls,
        writer: JournalWriter,
        *,
        run_id: str | None = None,
        worker_id: str | None = None,
        since: int | None = None,
    ) -> JournalTail:
        """A tail on a writer this caller already holds.

        `since=None` means "from the end of the file as it is now", which is what a caller
        watching one turn wants: the history of every previous run is not this turn. The
        writer is flushed before that end is read, because an id taken while events sit in
        the buffer would be re-read as new the moment somebody flushed.
        """
        if since is None:
            writer.flush()
            since = writer.store.last_id()
        return cls(
            writer.store, run_id=run_id, worker_id=worker_id, since=since, writer=writer
        )

    @classmethod
    def local(
        cls,
        *,
        run_id: str | None = None,
        worker_id: str | None = None,
        since: int | None = None,
        cfg: Config | None = None,
    ) -> JournalTail:
        """A tail on this process's own journal writer."""
        from .runtime import get_writer

        return cls.on(
            get_writer(cfg or get_config()),
            run_id=run_id,
            worker_id=worker_id,
            since=since,
        )

    @classmethod
    def attach(
        cls,
        path: Path | str | None = None,
        *,
        run_id: str | None = None,
        since: int = 0,
        cfg: Config | None = None,
    ) -> JournalTail:
        """A tail from another process. Opens its own connection and never writes.

        No writer, so nothing is flushed on this side: what a reader in another process can
        see is what the writing process has committed, which is the honest definition of
        the feed.
        """
        cfg = cfg or get_config()
        return cls(JournalStore(path or default_path(cfg)), run_id=run_id, since=since)

    # --- reading -------------------------------------------------------------

    def drain(self, limit: int | None = None) -> list[Event]:
        """Everything that has appeared since the last call, in feed order.

        The cursor advances past events this tail filtered out, so a narrowed tail does not
        re-read another run's events on every poll.
        """
        if self.writer is not None:
            self.writer.flush()
        batch = self.store.read_all(after_id=self.last_id, limit=limit)
        if batch:
            self.last_id = batch[-1].id
        return [e for e in batch if self._wanted(e)]

    def _wanted(self, event: Event) -> bool:
        if self.run_id is not None and event.run_id != self.run_id:
            return False
        if self.worker_id is not None and event.payload.get("worker_id") != self.worker_id:
            return False
        return True

    async def follow(
        self,
        *,
        stop: asyncio.Event | None = None,
        poll_interval: float = POLL_INTERVAL_S,
        until: Callable[[Event], bool] | None = None,
    ) -> AsyncIterator[Event]:
        """Live events until `stop` is set or `until` says this was the last one.

        `stop` is checked *after* a drain, never before, so setting it does not cut off
        events that were already on disk when it was set. A caller that cancels this
        iterator instead can still call `drain()` afterwards for the same reason: the
        cursor is the only state, and it is in the caller's object.
        """
        while True:
            batch = self.drain()
            for event in batch:
                yield event
                if until is not None and until(event):
                    return
            if stop is not None and stop.is_set():
                return
            if not batch:
                await asyncio.sleep(poll_interval)


def turn_ended(run_id: str) -> Callable[[Event], bool]:
    """`until=` for "this run's own turn has finished".

    A worker's `agent_finished` carries a `worker_id` and does not end the run; the
    top-level turn's does not. Without that distinction a frontend watching a delegating
    turn would stop rendering at the first worker that came back.
    """

    def done(event: Event) -> bool:
        return (
            event.type == "agent_finished"
            and event.run_id == run_id
            and event.payload.get("worker_id") is None
        )

    return done


__all__ = ["POLL_INTERVAL_S", "JournalTail", "turn_ended"]
