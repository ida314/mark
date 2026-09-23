"""The working bucket: what a note is, who may read it, and when it stops existing.

Session 7b. `journal/working_memory.py` is the other half - the fold and the scoped query.
This is the half that knows what a note means, and it is the only thing a tool touches.

Working memory is one of the three buckets `memory/scopes.py` declares, and the only one
that is **execution state rather than memory**: task-local, agent-local, short-lived, and
gone when its task scope ends. It is not stored with the other two and it is not retrieved
with them. Nothing here writes to Postgres, and nothing here renders into
`memory/retrieval.pack()` - see `memory/scopes.py` for why that was decided rather than
discovered.

## The handle carries its scope; it is not passed one per call

`WorkingMemory` is built from the `RunJournal` the turn already has, so its scope is fixed
at construction from the run and the worker that owns the turn. A worker's `AgentLoop`
builds its own, so worker A physically cannot name worker B's scope: there is no argument
for it to pass. That is isolation by construction, and it is deliberately *not* the only
thing holding the boundary up - the scope is also an equality in the query
(`journal/working_memory.notes_at`), because a construction-only guarantee is one refactor
from being nothing, and nothing in the source would show it had gone.

## Crossing the boundary

The pass file allows exactly three ways for one agent's scratch state to reach another: an
explicit structured message, a result, or runtime-managed shared task state. A worker's
`WorkerResult` is the second of those and already exists; this module adds no fourth. In
particular there is no "read another scope" argument, and the unscoped audit view
(`scopes_with_notes`) lives in the journal module where no tool can reach it.

## Both provenance flags travel with the note

A note the model wrote while a web page was in context is untrusted text, and handing it
back later as the runtime's own prose would launder it. `tainted` is recorded per note from
the tool context at write time and raised again on read.

`private` is recorded beside it and is a different claim - "the user's own data was in
context", the flag that shuts the egress door rather than the one that says not to obey what
is written. Both are stored now rather than when somebody needs them, because they can only
be observed at the moment the note is written: a promotion pass looking at a note a week
later has no way to find out, and an absent flag would read as a clean one.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..journal.runtime import RunJournal
from ..journal.working_memory import (
    DISCARD_REASONS,
    DISCARDED,
    ENTRY_VERSION,
    NOTE_MAX_CHARS,
    NOTED,
    ORCHESTRATOR,
    note_at,
    notes_at,
)


class WorkingMemoryError(RuntimeError):
    """A note that could not be written. Raised, never degraded into a note that is not there."""


@dataclass(frozen=True)
class Note:
    """One piece of scratch state, as it is read back."""

    key: str
    text: str
    tainted: bool
    private: bool

    @classmethod
    def from_entry(cls, entry: dict[str, Any]) -> Note:
        """Every field by key, with no fallback.

        An entry missing one is a bug in whoever wrote it, and each of the values a
        `.get` would supply here - an empty note, an untainted one, one not derived from the
        user's private data - is plausible enough to survive review and wrong.
        """
        return cls(
            key=entry["key"],
            text=entry["text"],
            tainted=bool(entry["tainted"]),
            private=bool(entry["private"]),
        )


@dataclass(frozen=True)
class WorkingMemory:
    """One scope's scratch state, inside one run.

    Cheap to construct and bound to a `RunJournal`, which already knows the run and the
    worker. Construct it with `for_turn`; the constructor is available for a test that wants
    to name a scope explicitly, which is also how the isolation tests demonstrate that
    naming someone else's scope is the only way in and that it is not reachable from a tool.
    """

    rj: RunJournal
    scope: str

    @classmethod
    def for_turn(cls, rj: RunJournal) -> WorkingMemory:
        """The scope of the agent whose turn this is.

        A worker's own id, or the literal `ORCHESTRATOR` for a turn that is not a worker's.
        Not the session id: `run_subagent` hands a worker `Session(id=parent_session_id)`,
        so a worker and its caller share one, and a bucket keyed on it would be a bucket
        they share. Not a bare `worker_id` either, because `None` is not a bucket.
        """
        return cls(rj=rj, scope=rj.worker_id or ORCHESTRATOR)

    @property
    def run_id(self) -> str:
        return self.rj.run_id

    def note(
        self, key: str, text: str, *, tainted: bool = False, private: bool = False
    ) -> Note:
        """Write one note into this scope. Replaces a note already under this key.

        Refuses rather than repairs. An empty key, an empty note or one over
        `NOTE_MAX_CHARS` raises `WorkingMemoryError`: a clipped note reads exactly like a
        complete one, and this bucket is what a later pass classifies for promotion.
        """
        key = key.strip()
        if not key:
            raise WorkingMemoryError("a working-memory note needs a key to be filed under")
        if not text.strip():
            raise WorkingMemoryError(f"working-memory note {key!r} is empty")
        if len(text) > NOTE_MAX_CHARS:
            raise WorkingMemoryError(
                f"working-memory note {key!r} is {len(text)} characters, over the "
                f"{NOTE_MAX_CHARS} limit; it is refused rather than truncated, because a "
                "clipped note reads like a whole one. Note the conclusion, not the material."
            )
        self.rj.emit(
            NOTED,
            {
                "scope": self.scope,
                "key": key,
                "text": text,
                "chars": len(text),
                "entry_version": ENTRY_VERSION,
                "tainted": bool(tainted),
                "private": bool(private),
            },
        )
        return Note(key=key, text=text, tainted=bool(tainted), private=bool(private))

    def notes(self) -> tuple[Note, ...]:
        """Everything this scope holds, in the order it was first noted.

        The writer is flushed first. Notes are buffered like the rest of a turn's chatter,
        so a read that skipped the flush would miss the note written two lines earlier -
        which is the one reading that must never be wrong.
        """
        self.rj.writer.flush()
        entries = notes_at(self.rj.writer.store, self.rj.run_id, self.scope)
        return tuple(Note.from_entry(e) for e in entries)

    def get(self, key: str) -> Note | None:
        self.rj.writer.flush()
        entry = note_at(self.rj.writer.store, self.rj.run_id, self.scope, key)
        return None if entry is None else Note.from_entry(entry)

    def discard(self, reason: str) -> int:
        """End this scope. Returns how many notes were discarded.

        Appends a tombstone; it deletes nothing. The notes stay in the journal, so a fold
        taken at an earlier position still shows what the task held - which is what keeps a
        completed run auditable - while every read from here on returns nothing.

        A scope that holds nothing gets no tombstone, and that is not a silent skip: there
        is nothing to entomb, the fold of an empty scope is already empty, and this is called
        at the end of *every* turn and *every* worker. Writing one anyway would put two
        events saying "nothing happened" into the live journal for each of them, which is the
        kind of volume that makes a feed unreadable. `working_memory_discarded` therefore
        always records a real discard, and its `notes` count is always positive.
        """
        if reason not in DISCARD_REASONS:
            raise WorkingMemoryError(
                f"unknown discard reason {reason!r}; expected one of {', '.join(DISCARD_REASONS)}"
            )
        live = len(self.notes())
        if not live:
            return 0
        self.rj.emit(DISCARDED, {"scope": self.scope, "reason": reason, "notes": live})
        return live


def discard_for_turn(rj: RunJournal, reason: str) -> int:
    """End the working scope of the agent whose turn just ended.

    A one-liner with a name, so that the two callers - `agent/loop.py` at the end of a run's
    turn and `agent/subagents.py` after a worker finishes - are findable from here and read
    as the same decision rather than as two places that happen to do a similar thing.
    """
    return WorkingMemory.for_turn(rj).discard(reason)


__all__ = [
    "ORCHESTRATOR",
    "Note",
    "WorkingMemory",
    "WorkingMemoryError",
    "discard_for_turn",
]
