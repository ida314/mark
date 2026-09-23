"""Promotion, as the journal holds it: what has been classified, what has landed, where.

Session 7c. `memory/promotion.py` is the other half - it decides what a note means, calls
the classifier and does the durable write. This module is the journal-layer half and holds
no opinion about either, because `journal/` does not import `memory/` or `agent/`. Same
split as `journal/worker_results.py` / `agent/result_cache.py` (6c) and
`journal/working_memory.py` / `agent/working_memory.py` (7b), for the same reason.

## Why promotion lives in the journal at all

`state = fold(reduce, journal, initial)`. A promotion is two facts recorded in order:

    promotion_classified   this note is worth keeping, here is the whole of it
    promotion_committed    and it is now in the durable store, at this position

Everything a resume needs is the gap between them. A promotion with the first and not the
second was interrupted between the decision and the write, and finishing it is the *only*
thing that keeps a crash from losing it. That state has to survive the process, so it
cannot be in memory; it has to survive a machine with checkpoints switched off, so it
cannot be only in a checkpoint (`[checkpoints] enabled` is false in the shipped config and
the live journal holds zero checkpoint rows across 52 runs); and it may not disagree with
the journal, so it must be *derived* from it rather than accumulated beside it.

So `Checkpoint.pending_promotions` and `Checkpoint.memory_watermark` - the two slots Pass 4
cut and left inert - are filled from `pending_at` and `watermark_at` below, exactly as 6c
filled `worker_results[]` from `cached_results_at`. The checkpoint may lag the fold. It can
never contradict it, because it is the same function.

## The classified record carries the note, not a pointer to it

Unusual here, and deliberate. A scope is discarded at the boundary that promotes it, so by
the time a resume looks, the working bucket that held the note has been tombstoned. A
pointer would point at an empty bucket. `worker_result_cached` carries its body for the
same reason: the thing a resume has to be able to *do something with* cannot be a preview.

The checkpoint copy, by contrast, carries the digest and not the text. A checkpoint is an
acceleration structure, and the text is one re-fold away.

## First record wins

Two `promotion_committed` rows under one key would mean the write ran twice. The fold keeps
the first, so a fold taken later cannot report a different landing place than one taken
earlier. Whether the *store* was written twice is a different question, and it is answered
by the dedup key in the store itself (`candidate_promotion_key_idx`,
`raw_events_connector_dedup_idx`) rather than by anything here - the journal cannot prevent
a write, only record one.
"""

from __future__ import annotations

import json
from typing import Any

from .store import JournalStore

CLASSIFIED = "promotion_classified"
COMMITTED = "promotion_committed"
BATCH = "promotion_batch"

EPISODIC = "episodic"
SEMANTIC = "semantic"
TARGETS: tuple[str, ...] = (EPISODIC, SEMANTIC)

# The shape of a classified entry. Inside the entry rather than inside the key, for 6c's
# reason: an entry written under other rules is skipped by the fold - the promotion is
# simply not pending, which is the state the runtime had before promotion existed - rather
# than read as if the rules had not moved.
ENTRY_VERSION = 1

# What the checkpoint copy of a pending promotion carries. The text is deliberately absent:
# the journal has it, and a snapshot that copied it would be a second copy of a body that is
# already durable one re-fold away.
PENDING_FIELDS: tuple[str, ...] = (
    "promotion_key",
    "scope",
    "key",
    "target",
    "chars",
    "text_sha256",
    "session_id",
    "entry_version",
)


def _rows(store: JournalStore, run_id: str, types: tuple[str, ...], covers_seq: int | None):
    placeholders = ",".join("?" for _ in types)
    sql = f"SELECT seq, type, payload FROM journal WHERE run_id = ? AND type IN ({placeholders})"
    args: tuple[Any, ...] = (run_id, *types)
    if covers_seq is not None:
        sql += " AND seq <= ?"
        args = (*args, covers_seq)
    return [
        (row["seq"], row["type"], json.loads(row["payload"]))
        for row in store.query(sql + " ORDER BY seq", args)
    ]


def classified_at(
    store: JournalStore, run_id: str, covers_seq: int | None = None
) -> tuple[dict[str, Any], ...]:
    """Every promotion this run decided on, at a journal position, in decision order.

    `covers_seq` is the checkpoint's question - "what had been classified by here" - and
    None is "wherever the run got to". One fold for both, so what a checkpoint accounts for
    and what a resume acts on cannot be two derivations that disagree.
    """
    entries: dict[str, dict[str, Any]] = {}
    for seq, _type, payload in _rows(store, run_id, (CLASSIFIED,), covers_seq):
        if payload.get("entry_version") != ENTRY_VERSION:
            continue
        entries.setdefault(payload["promotion_key"], {**payload, "classified_seq": seq})
    return tuple(entries.values())


def committed_at(
    store: JournalStore, run_id: str, covers_seq: int | None = None
) -> dict[str, dict[str, Any]]:
    """Promotion key -> the record of where it landed. First record wins."""
    out: dict[str, dict[str, Any]] = {}
    for seq, _type, payload in _rows(store, run_id, (COMMITTED,), covers_seq):
        if payload.get("entry_version") != ENTRY_VERSION:
            continue
        out.setdefault(payload["promotion_key"], {**payload, "committed_seq": seq})
    return out


def pending_at(
    store: JournalStore, run_id: str, covers_seq: int | None = None
) -> tuple[dict[str, Any], ...]:
    """Promotions decided and not yet durable - `Checkpoint.pending_promotions`.

    This is the list a crash between classification and write leaves behind, and completing
    it is what makes that crash cost nothing. An empty tuple is the ordinary state: on a run
    that promoted nothing, and on a run whose boundary finished its writes.
    """
    done = committed_at(store, run_id, covers_seq)
    return tuple(
        entry for entry in classified_at(store, run_id, covers_seq)
        if entry["promotion_key"] not in done
    )


def pending_summary(entry: dict[str, Any]) -> dict[str, Any]:
    """One pending promotion as a checkpoint carries it: the fields, without the body."""
    return {name: entry[name] for name in PENDING_FIELDS}


def watermark_at(
    store: JournalStore, run_id: str, covers_seq: int | None = None
) -> dict[str, Any] | None:
    """Committed episodic and semantic write positions - `Checkpoint.memory_watermark`.

    None when this run has no promotion events at all at this position. That is the
    distinction 4a cut the field for and it still holds: an object full of zeros would read
    as a run whose memory positions were measured and found to be nothing, and a run that
    never promoted has no measurement to report.

    What a "position" is differs by store, and the difference is recorded rather than
    smoothed over:

    * episodic writes go to `raw_events`, which has a bigint identity column, so
      `last_sequence` is a real monotonic position;
    * semantic writes go to `candidate_memories`, which has no sequence of any kind. Its
      ids are uuid7 and therefore time-ordered, so `last_ref` is the high-water mark and
      `last_sequence` is null. A count of rows is not a position and is never used as one -
      `committed` is beside the position, never instead of it.
    """
    entries = classified_at(store, run_id, covers_seq)
    committed = committed_at(store, run_id, covers_seq)
    if not entries and not committed:
        return None
    mark: dict[str, Any] = {
        "entry_version": ENTRY_VERSION,
        "pending": len(pending_at(store, run_id, covers_seq)),
        "last_seq": max((c["committed_seq"] for c in committed.values()), default=0),
    }
    for target in TARGETS:
        rows = [c for c in committed.values() if c["target"] == target]
        mark[target] = {
            "committed": len(rows),
            "last_ref": rows[-1]["ref"] if rows else None,
            "last_sequence": rows[-1]["sequence"] if rows else None,
        }
    return mark


__all__ = [
    "BATCH",
    "CLASSIFIED",
    "COMMITTED",
    "ENTRY_VERSION",
    "EPISODIC",
    "PENDING_FIELDS",
    "SEMANTIC",
    "TARGETS",
    "classified_at",
    "committed_at",
    "pending_at",
    "pending_summary",
    "watermark_at",
]
