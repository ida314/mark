"""The run-scoped result cache: what a delegation is keyed by, and what is kept.

Session 6c. `delegation.py` is the request, `results.py` is the response, and this is the
answer to "we have already done exactly this, in this run". Two uses, from the pass file: a
resume reuses completed work rather than re-running it, and a redundant in-run delegation is
a cache hit rather than a second bill.

## The key

    result_key = hash(durable_role, task_spec, relevant_context_refs)

All three of those are inside `TaskSpec.canonical()`, so `result_key(spec)` **is**
`spec.digest` - the same bytes `worker_created.task_digest` already carries - and not a
second, independent derivation that would be free to disagree with it about what two
delegations have in common. `relevant_context_refs` is `TaskSpec.relevant_context`:
normalized, deduplicated, sorted, and prose today rather than urls and ids (6a's open
question 1), which is what makes a *missed* hit the likely failure here and a false hit
unlikely. That asymmetry is the one this pass wants - a miss costs a worker, a false hit
answers a question nobody asked.

## Run-scoped, and visibly so

Cross-run reuse is the pass's *Must not*: staleness semantics for repository and web state
are not worked out, and a key that survives a run would quietly assert they are. The scope
is enforced in the query (`journal/worker_results.py`) rather than by hashing the run id
into the key, so that "this never crosses a run" is a line a test can read, and so that two
runs doing the same work remain *observable* as the same work.

## Only `completed`, and only what a result is

An `uncertain` result is exactly the case where re-running may be the right answer, and
serving one from a cache decides that question silently and for ever. `blocked` says the
work did not happen, and nothing was earned. So only `completed` is written.

Session 9a adds the third refusal and one carried field. A result the runtime **invalidated**
is never written: caching it would serve the same disproved claim back for the rest of the
run, free and with the verification stripped off, and a fabrication is not cheaper the second
time - only faster. A result merely flagged `uncertain` *is* written, with its flags, because
the alternative is re-running a worker on every hit to re-derive a doubt that is already
known; what must not happen is the doubt being lost in the copy, which is why `validation`
travels in the entry exactly as `tainted` does.

Two things a result carries are deliberately not cached:

* **the transcript.** 6b left this open - "if 6c starts persisting results, whether the
  transcript is persisted with them has to be answered before it is answered by accident".
  It is not: the worker's prose is already in the archive under `subagent:<role>`, a copy in
  the journal would be a second one, and every copy is another place a transcript can reach
  a prompt from. A reused result carries `reused_from` instead, so an empty transcript reads
  as "this was not re-run" rather than as a worker that said nothing.
* **the candidate memories.** The worker that earned the result already proposed them, and
  the review gate already has them. Re-inserting them on every cache hit would be this
  codebase's other recurring bug - the duplicate - and would let one delegation, repeated,
  manufacture the appearance of independent corroboration.
"""

from __future__ import annotations

from ..journal.runtime import RunJournal
from ..journal.store import JournalStore
from ..journal.worker_results import CACHED, ENTRY_VERSION, REUSED, cached_result
from .delegation import TaskSpec
from .results import Flag, WorkerResult
from .verification import HARD_FLAGS


class ResultCacheError(RuntimeError):
    """A cache entry that could not be written. Raised, never degraded into a silent miss."""


def result_key(spec: TaskSpec) -> str:
    """The key one delegation's result is cached under.

    Deliberately an alias rather than a hash of a hash: the canonical task spec already
    contains the durable role, the task and the context refs, and `worker_created` already
    records this exact string as `task_digest`, so the worker that earned a cached result is
    one join away.
    """
    return spec.digest


def entry_for(spec: TaskSpec, result: WorkerResult) -> dict[str, object]:
    """The journal payload for one cached result. `worker_id` travels as a named argument."""
    return {
        "result_key": result_key(spec),
        "name": spec.durable_role,
        "status": result.status,
        "entry_version": ENTRY_VERSION,
        "answer": result.answer,
        "evidence": list(result.evidence),
        "actions_taken": list(result.actions_taken),
        "followups": list(result.followups),
        "notes": list(result.notes),
        "validation": result.validation,
        "flags": [f.code for f in result.flags],
        "details": [f.detail for f in result.flags],
        "tainted": bool(result.tainted),
    }


def result_from_entry(entry: dict) -> WorkerResult:
    """One stored entry as the result a caller is handed.

    Every field is read by key and none has a fallback: a cache entry missing a field is a
    bug in whoever wrote it, and the value it would be filled in with - an empty answer, an
    untainted flag - is a plausible one, which is how this codebase's characteristic failure
    gets in.
    """
    return WorkerResult(
        status=entry["status"],
        answer=entry["answer"],
        evidence=tuple(entry["evidence"]),
        actions_taken=tuple(entry["actions_taken"]),
        followups=tuple(entry["followups"]),
        notes=tuple(entry["notes"]),
        tainted=bool(entry["tainted"]),
        # Not stored, and both are statements. `report_valid` is True because only a
        # readable `completed` report is ever written (see `remember`), and the entry's
        # `status` is enforced as `completed` by the journal's own enum. The candidates were
        # proposed once, by the worker that earned this, and are not proposed again.
        report_valid=True,
        candidate_memories=(),
        # Session 9a. Read back rather than defaulted, so a hit on an `uncertain` result is
        # as uncertain the second time. `hard` is recomputed from the code instead of being
        # stored, because which codes are hard is a property of this build's checks and not
        # of the run that wrote the entry - a stored `hard` would be a stale ruling.
        validation=entry["validation"],
        flags=tuple(
            Flag(code=code, detail=detail, hard=code in HARD_FLAGS)
            for code, detail in zip(entry["flags"], entry["details"], strict=False)
        ),
        reused_from=entry["worker_id"],
    )


def remember(
    rj: RunJournal, spec: TaskSpec, result: WorkerResult, *, worker_id: str
) -> bool:
    """Cache a worker's result for the rest of this run. Returns whether anything was kept.

    False is the ordinary answer for a `blocked` or `uncertain` worker, and it is not a
    failure: those are the two statuses where re-running is a decision somebody may still
    want to make.

    Session 9b adds the third refusal, and it is the one that matters most: a result the
    runtime could not corroborate is not kept. Caching it would serve the same unchecked
    claim back for the rest of the run, free and with the verification stripped off - a
    fabrication is not cheaper the second time, it is only faster.
    """
    if not worker_id:
        raise ResultCacheError(
            "a cached result must name the worker that earned it; an entry with no worker "
            "cannot be traced back to the run that produced it"
        )
    if result.status != "completed" or not result.report_valid:
        return False
    if result.validation == "invalidated":
        return False
    # Synchronous (`writer.SYNC_TYPES`): a cache entry still sitting in a buffer is exactly
    # the entry a crash loses, and the crash is what the cache is for.
    rj.emit(CACHED, entry_for(spec, result), worker_id=worker_id, sync=True)
    return True


def lookup(store: JournalStore, run_id: str, spec: TaskSpec) -> WorkerResult | None:
    """What this run has already earned for this delegation, or None. Writes nothing."""
    entry = cached_result(store, run_id, result_key(spec))
    return None if entry is None else result_from_entry(entry)


def serve(
    rj: RunJournal, spec: TaskSpec, *, parent_step_id: str | None = None
) -> WorkerResult | None:
    """Answer a delegation out of this run's cache, recording that it was answered.

    None means no worker has done this work in this run, and the caller should start one.

    The writer is flushed first. Every cache entry is written synchronously, so today the
    store already holds them all; flushing anyway means a lookup cannot start missing hits
    because somebody later decided `worker_result_cached` could be buffered after all.
    """
    rj.writer.flush()
    result = lookup(rj.writer.store, rj.run_id, spec)
    if result is None:
        return None
    # The only record that this delegation happened at all: a cache hit writes no
    # `worker_created` / `worker_finished` pair, because no worker ran.
    rj.emit(
        REUSED,
        {
            "result_key": result_key(spec),
            "name": spec.durable_role,
            "source_worker_id": result.reused_from,
            "answer_chars": len(result.answer),
            "parent_step_id": parent_step_id,
        },
    )
    return result


__all__ = [
    "ResultCacheError",
    "entry_for",
    "lookup",
    "remember",
    "result_from_entry",
    "result_key",
    "serve",
]
