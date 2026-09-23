"""The run-scoped result cache, as the journal holds it.

Session 6c. `agent/result_cache.py` is the other half: it decides what a result key is and
turns an entry back into a `WorkerResult`. This module is the journal-layer half - the fold
- and it holds no opinion about what a worker result means, because `journal/` does not
import `agent/` and this is the boundary where that stops being true if anybody is casual
about it.

## The journal is the cache, and the checkpoint is a copy of it

`state = fold(reduce, journal, initial)`. A cached result is `worker_result_cached` events
folded by `result_key`, and `checkpoints.worker_results[]` is that fold written down at a
boundary - an acceleration structure that may lag the journal and may never disagree with
it. The alternative, storing results only in checkpoints, would have made this pass inert on
the shipped config: `[checkpoints] enabled` is false, so there is no checkpoint on this
machine to have kept anything.

## Run-scoped, and the scope is in the query

`result_key` is content-addressed: the same delegation has the same key in every run, and in
every process. What makes reuse run-scoped is the `run_id` in the two queries below, which
is a line a test can read (`test_a_result_cached_in_another_run_is_never_reused`). Hashing
the run id into the key instead would make cross-run reuse impossible *and* unobservable -
the pass's *Must not* buried inside a digest, where nothing can check that it still holds and
nothing can count how often two runs did the same work.

## First entry wins

Two entries under one key mean the same work was done twice - today only possible if a write
and a lookup crossed, and routinely possible the day workers run in parallel. The fold keeps
the *first*, so that a fold taken at a later position cannot hand back a different answer
than a fold taken at an earlier one: every reuse already served was served the first entry,
and a cache that changes its mind about a key is a cache whose hits are not reproducible.
"""

from __future__ import annotations

import json
from typing import Any

from .store import JournalStore

CACHED = "worker_result_cached"
REUSED = "worker_result_reused"

# The shape of a cached entry's fields. Inside the entry rather than inside the key: the key
# is the identity of the *work*, and it must not move because the record of the work changed
# shape. An entry written under other rules is skipped by the fold below - a miss, which
# costs a worker, rather than a read of fields that may not mean what they say.
ENTRY_VERSION = 1


def cached_results_at(
    store: JournalStore, run_id: str, covers_seq: int | None = None
) -> tuple[dict[str, Any], ...]:
    """This run's cached results at a journal position, in the order they were earned.

    `covers_seq` is the checkpoint's question - "what had been cached by here" - and None is
    "wherever the run got to". Both go through this one fold, so a checkpoint's
    `worker_results[]` is never a second derivation of what the journal already says.
    """
    sql = "SELECT payload FROM journal WHERE run_id = ? AND type = ?"
    args: tuple[Any, ...] = (run_id, CACHED)
    if covers_seq is not None:
        sql += " AND seq <= ?"
        args = (run_id, CACHED, covers_seq)
    entries: dict[str, dict[str, Any]] = {}
    for row in store.query(sql + " ORDER BY seq", args):
        entry = json.loads(row["payload"])
        if entry.get("entry_version") != ENTRY_VERSION:
            # Not an error and not a null: the work is simply done again, which is the
            # behaviour this runtime had before the cache existed.
            continue
        entries.setdefault(entry["result_key"], entry)
    return tuple(entries.values())


def cached_result(store: JournalStore, run_id: str, result_key: str) -> dict[str, Any] | None:
    """The entry one delegation would be served, or None for a miss.

    Derived from the same fold rather than from a query of its own, so "what a checkpoint
    recorded" and "what a delegation is served" cannot disagree about a key.
    """
    for entry in cached_results_at(store, run_id):
        if entry["result_key"] == result_key:
            return entry
    return None


__all__ = ["CACHED", "ENTRY_VERSION", "REUSED", "cached_result", "cached_results_at"]
