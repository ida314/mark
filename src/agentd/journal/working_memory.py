"""Working memory, as the journal holds it: the fold, and the scope that isolates it.

Session 7b. `agent/working_memory.py` is the other half - it decides what a note means and
is the only thing a tool touches. This module is the journal-layer half and holds no opinion
about what a note is for, because `journal/` does not import `agent/` and this is the
boundary where that stops being true if anybody is casual about it. The split is the one
session 6c drew between `agent/result_cache.py` and `journal/worker_results.py`, copied
deliberately: that pair is the only run-scoped, discarded-with-the-run state this runtime
had, and it is the shape working memory needs.

## The journal is the working memory, and a checkpoint is a position in it

`state = fold(reduce, journal, initial)`. Working memory is `working_memory_noted` events
folded per scope, and a checkpoint captures it by *position* rather than by copy: everything
here takes a `covers_seq`, so `notes_at(store, run, scope, checkpoint.covers_seq)` is what
the run held at that boundary. That is the relationship `messages_ref` already has with the
messages - a pointer, not a third copy - and it is why no column was added to the
`checkpoint` table for this. It also means working memory exists on this machine: the live
journal holds 1140 events, 52 runs and **zero checkpoint rows**, because `[checkpoints]
enabled` is false in the shipped config, so a bucket that lived only in a checkpoint would be
a bucket nothing has ever written.

## Scope is an explicit string, never an absent worker id

Isolation keys on `(run_id, scope)` and both are in the WHERE clause of `notes_at` - a line
a test can read, the way 6c put the run id in a query rather than inside a hash.

`scope` is *not* `worker_id`, and the difference is the whole point. A worker's id is a
perfectly good scope, but the orchestrator has none, and `worker_id IS NULL` as a scope
would be this codebase's other recurring bug: an absent value becoming a shared bucket that
unrelated writers collide in. So the orchestrator's scope is the literal string
`ORCHESTRATOR` and an empty scope is refused at the door.

It is not the **session id** either, and that is load-bearing rather than incidental:
`run_subagent` builds `Session(id=parent_session_id, ...)`, so a worker and its caller share
one session id (a known Pass 6 privacy defect, deliberately not fixed here). Isolation built
on the session id would be no isolation at all - worker A, worker B and the orchestrator
would read and overwrite one another's scratch state, and every test of it would still pass,
because there would be exactly one bucket and it would look full and healthy.

## Discard is a tombstone, never a delete

The journal is append-only; nothing here deletes a row. A scope is emptied by appending
`working_memory_discarded`, which the fold reads as "everything before this, in this scope,
is gone". So a fold taken at an earlier position still sees what the run held then - the
history of a completed run remains readable and auditable - while the live view is empty.
"""

from __future__ import annotations

import json
from typing import Any

from .store import JournalStore

NOTED = "working_memory_noted"
DISCARDED = "working_memory_discarded"

# The scope a turn that is not a worker writes under. A literal, because the alternative -
# leaving it unset for the orchestrator - makes "no worker" and "unknown worker" the same
# bucket.
ORCHESTRATOR = "orchestrator"

# Why a scope was emptied. `worker_finished` is a worker's task scope ending, `run_completed`
# is the run's, and `manual` is a caller throwing its own scratch away mid-task. An enum
# rather than free text so that "a completed run leaves nothing behind" is countable.
DISCARD_REASONS: tuple[str, ...] = ("worker_finished", "run_completed", "manual")

# The shape of a stored note. Inside the entry rather than inside the key, for 6c's reason:
# an entry written under other rules is skipped by the fold - the note is simply not there,
# which is the state the runtime had before working memory existed - rather than read as if
# the rules had not moved.
ENTRY_VERSION = 1

# The longest note this bucket will carry. A note is journaled in full, because a preview
# cannot be handed back as scratch state, so there has to be a ceiling. It is enforced by
# the writer in `agent/working_memory.py` as a refusal, not a truncation: a silently clipped
# note is a half-fact that still reads as a whole one.
NOTE_MAX_CHARS = 4000


def _rows(store: JournalStore, sql: str, args: tuple[Any, ...]) -> list[tuple[str, dict[str, Any]]]:
    return [(row["type"], json.loads(row["payload"])) for row in store.query(sql, args)]


def _fold(rows: list[tuple[str, dict[str, Any]]]) -> dict[str, dict[str, Any]]:
    """One scope's events in seq order, as the notes that scope currently holds.

    Last write wins per key - a note re-stated under the same key is an update to scratch
    state, not a second note - and insertion order is kept, so what a scope reads back is
    the order it learned things in.
    """
    entries: dict[str, dict[str, Any]] = {}
    for event_type, payload in rows:
        if event_type == DISCARDED:
            entries.clear()
            continue
        if payload.get("entry_version") != ENTRY_VERSION:
            continue
        key = payload["key"]
        entries.pop(key, None)
        entries[key] = payload
    return entries


def notes_at(
    store: JournalStore, run_id: str, scope: str, covers_seq: int | None = None
) -> tuple[dict[str, Any], ...]:
    """What one scope of one run holds, at a journal position.

    `covers_seq` is the checkpoint's question - "what was in working memory by here" - and
    None is "wherever the run got to". Both go through this one fold, so what a checkpoint
    accounts for and what an agent reads can never be two derivations that disagree.

    The isolation is the two equalities in this WHERE clause. Widening either of them -
    dropping the scope, or reading across runs - is what a worker reading another worker's
    scratch state would look like in the source, and `tests/test_working_memory.py` fails if
    either goes.
    """
    if not scope:
        raise ValueError("working memory has no scope-less bucket; pass a scope")
    sql = (
        "SELECT type, payload FROM journal "
        " WHERE run_id = ? AND type IN (?, ?) "
        "   AND json_extract(payload, '$.scope') = ?"
    )
    args: tuple[Any, ...] = (run_id, NOTED, DISCARDED, scope)
    if covers_seq is not None:
        sql += " AND seq <= ?"
        args = (*args, covers_seq)
    return tuple(_fold(_rows(store, sql + " ORDER BY seq", args)).values())


def note_at(
    store: JournalStore, run_id: str, scope: str, key: str, covers_seq: int | None = None
) -> dict[str, Any] | None:
    """One note, or None. Derived from the same fold rather than from a query of its own."""
    for entry in notes_at(store, run_id, scope, covers_seq):
        if entry["key"] == key:
            return entry
    return None


def scopes_with_notes(
    store: JournalStore, run_id: str, covers_seq: int | None = None
) -> dict[str, int]:
    """Every scope of a run that still holds notes, and how many. The audit view.

    Deliberately not scoped, and deliberately not reachable from a tool: this is what
    answers "did that run leave anything behind", and it is the one function here that would
    be a leak if an agent could call it. Nothing in `agent/` renders its output into a
    prompt; `tests/test_working_memory.py` and a later promotion pass are its callers.
    """
    sql = "SELECT type, payload FROM journal WHERE run_id = ? AND type IN (?, ?)"
    args: tuple[Any, ...] = (run_id, NOTED, DISCARDED)
    if covers_seq is not None:
        sql += " AND seq <= ?"
        args = (*args, covers_seq)
    by_scope: dict[str, list[tuple[str, dict[str, Any]]]] = {}
    for event_type, payload in _rows(store, sql + " ORDER BY seq", args):
        by_scope.setdefault(payload["scope"], []).append((event_type, payload))
    held = {scope: len(_fold(rows)) for scope, rows in by_scope.items()}
    return {scope: n for scope, n in held.items() if n}


__all__ = [
    "DISCARDED",
    "DISCARD_REASONS",
    "ENTRY_VERSION",
    "NOTED",
    "NOTE_MAX_CHARS",
    "ORCHESTRATOR",
    "note_at",
    "notes_at",
    "scopes_with_notes",
]
