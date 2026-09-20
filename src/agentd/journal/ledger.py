"""The effect ledger: one row per effecting call, and what state that call reached.

The question this table answers, and the only one, is the question a crash leaves behind:
*did that already happen?* Nothing reads it yet - reconciliation and resume are Pass 4 -
so this session's whole job is to make sure the row exists, and exists **before** the side
effect it describes.

## The protocol

```
intended    row written and journaled before dispatch
started     the handler is about to be awaited
committed   the call returned and its result is recorded   (result_ref)
failed      the call returned a failure, or raised
orphaned    written by nobody here: Pass 4 sets it on resume for a row left at `started`
```

A process killed between `started` and a terminal state leaves the row at `started`, which
is exactly the evidence Pass 4 needs: an `unsafe_write` in that state may have happened and
is never retried automatically.

## Where the row sits relative to the journal

The journal is the source of truth; this table is derived from it and may lag it, never
lead it. So every transition **journals first and writes the row second**. A crash between
the two leaves an `effect_intended` with no row - recoverable, because re-folding the
journal rebuilds it - rather than a row claiming an effect the journal never announced.

The one field a re-fold cannot rebuild is the `intended` / `started` distinction, because
the Pass 2 vocabulary has two effect event types and not three (see the outcome record). A
Pass 4 reconciliation that finds the ledger missing must therefore treat every intended
effect as uncertain, which is the conservative direction.

## What it does not do

It does not prevent anything. Two attempts at the same logical call share a key and share a
row, with `attempt` incremented - the second attempt still runs. Suppressing it is
reconciliation, which needs to know what a crash interrupted, and that is Pass 4.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from ..config import Config
from ..ids import utcnow, uuid7
from ..tools import effects
from ..tools.idempotency import args_hash, digest, idempotency_key
from .runtime import RunJournal
from .store import JournalError, JournalStore
from .writer import JournalWriter

INTENDED = "intended"
STARTED = "started"
COMMITTED = "committed"
FAILED = "failed"
# Never written here. Pass 4 sets it on a row a crash left at `started`; it is in the
# vocabulary now so that pass fills a slot rather than migrating a CHECK constraint.
ORPHANED = "orphaned"

EFFECT_STATES: tuple[str, ...] = (INTENDED, STARTED, COMMITTED, FAILED, ORPHANED)
TERMINAL: tuple[str, ...] = (COMMITTED, FAILED)

# Which transitions this session's protocol allows. An out-of-order transition is a bug in
# the caller, not a state worth recording, so it raises: a ledger that accepts
# `committed -> started` is a ledger whose states mean nothing to the pass that reads them.
ALLOWED: dict[str, tuple[str, ...]] = {
    INTENDED: (STARTED, FAILED),
    STARTED: (COMMITTED, FAILED),
}


class EffectStateError(JournalError):
    """A ledger transition that the protocol does not allow."""


@dataclass(frozen=True)
class EffectRow:
    """One ledger row, as read back."""

    idempotency_key: str
    run_id: str
    step_id: str
    tool: str
    effect_class: str
    args_hash: str
    state: str
    result_ref: str | None
    effect_id: str
    attempt: int
    created_at: str
    updated_at: str
    started_at: str | None

    @classmethod
    def from_row(cls, row: Any) -> EffectRow:
        return cls(
            idempotency_key=row["idempotency_key"], run_id=row["run_id"],
            step_id=row["step_id"], tool=row["tool"], effect_class=row["effect_class"],
            args_hash=row["args_hash"], state=row["state"], result_ref=row["result_ref"],
            effect_id=row["effect_id"], attempt=row["attempt"],
            created_at=row["created_at"], updated_at=row["updated_at"],
            started_at=row["started_at"],
        )

    @property
    def open(self) -> bool:
        """Still in flight: the call was announced and has not resolved."""
        return self.state in (INTENDED, STARTED)


class Effect:
    """A handle on one in-flight effect. Returned by `EffectLedger.intend`.

    Holds no lock and no transaction: each transition is its own journal append followed by
    its own row update, so a handle can be carried across an `await` that takes a minute.
    """

    def __init__(
        self,
        ledger: EffectLedger,
        *,
        key: str,
        effect_id: str,
        run_id: str,
        step_id: str,
        tool: str,
        effect_class: str,
        args_hash: str,
        attempt: int,
    ) -> None:
        self.ledger = ledger
        self.key = key
        self.effect_id = effect_id
        self.run_id = run_id
        self.step_id = step_id
        self.tool = tool
        self.effect_class = effect_class
        self.args_hash = args_hash
        self.attempt = attempt
        self.state = INTENDED
        self._dispatched_at: float | None = None

    # --- transitions ---------------------------------------------------------

    def dispatched(self) -> None:
        """The handler is about to be awaited. After this, a crash means "may have run"."""
        self._check(STARTED)
        self._dispatched_at = time.perf_counter()
        self.ledger._store_started(self.key)
        self.state = STARTED

    def committed(self, *, result_ref: str, result: str | None = None) -> None:
        self._finish(COMMITTED, result_ref=result_ref, result=result, error=None)

    def failed(self, error: str, *, result_ref: str) -> None:
        self._finish(FAILED, result_ref=result_ref, result=None, error=error)

    # --- internals -----------------------------------------------------------

    def _finish(
        self, state: str, *, result_ref: str, result: str | None, error: str | None
    ) -> None:
        self._check(state)
        self.ledger._journal_committed(
            run_id=self.run_id,
            step_id=self.step_id,
            effect_id=self.effect_id,
            key=self.key,
            status=state,
            duration_ms=self.duration_ms,
            result_digest=digest(result) if result is not None else None,
            error=error,
            attempt=self.attempt,
        )
        self.ledger._store_finished(self.key, state, result_ref)
        self.state = state

    @property
    def duration_ms(self) -> int:
        """How long the dispatched call has been running. 0 before dispatch."""
        if self._dispatched_at is None:
            return 0
        return int((time.perf_counter() - self._dispatched_at) * 1000)

    def _check(self, target: str) -> None:
        if target not in ALLOWED.get(self.state, ()):
            raise EffectStateError(
                f"effect {self.key[:12]} ({self.tool}) cannot go {self.state} -> {target}"
            )


class EffectLedger:
    """Reads and writes the `effect` table, and journals every transition it records.

    Constructed around a `JournalWriter` rather than a path: the ledger and the journal
    share one file, one connection and one lock, so the row can never be committed into a
    different transaction scope than the event it follows.
    """

    def __init__(self, writer: JournalWriter | Callable[[], JournalWriter]) -> None:
        self._writer = writer

    @property
    def writer(self) -> JournalWriter:
        """The writer this ledger appends through, resolved on every use.

        A zero-argument callable is accepted, and is what the runtime passes. An
        `AgentLoop` builds its executor in `__init__`, long before the turn it will run and
        before anything has decided which journal file this process writes to, so binding
        the writer at construction would open a file at import time - and would pin an
        effect's events to a *different* journal than the run that caused them if the loop
        was handed a writer of its own. Resolved rather than cached for the same reason:
        `get_writer` is already cached per path, and caching it here again is how a
        repointed data directory ends up with a run's events in two files.
        """
        writer = self._writer
        return writer if isinstance(writer, JournalWriter) else writer()

    @property
    def store(self) -> JournalStore:
        return self.writer.store

    # --- the protocol --------------------------------------------------------

    def intend(
        self,
        *,
        run_id: str,
        step_id: str,
        tool: str,
        effect_class: str,
        args: Mapping[str, Any],
        declared: tuple[str, ...] = (),
    ) -> Effect:
        """Record the intent to make this call, before anything is dispatched.

        Raises `CanonicalizationError` (from `tools.idempotency`) if no stable key can be
        derived from these arguments. The caller must refuse the call rather than run an
        effect under a key that will never match itself.
        """
        key = idempotency_key(
            run_id=run_id, step_id=step_id, tool_name=tool, args=args, declared=declared
        )
        digest_of_args = args_hash(args, declared=declared)
        # A second attempt at the same logical call lands on the same row with `attempt`
        # incremented - including one whose first attempt already committed. It is recorded
        # rather than refused: deciding that a finished effect must not run again is
        # reconciliation, which is Pass 4 and is this pass's *Must not*.
        previous = self.get(key)
        attempt = (previous.attempt + 1) if previous is not None else 1
        effect_id = str(uuid7())
        self._journal_intended(
            run_id=run_id,
            step_id=step_id,
            effect_id=effect_id,
            key=key,
            tool=tool,
            effect_class=effect_class,
            args_digest=digest_of_args,
            attempt=attempt,
        )
        now = utcnow().isoformat()
        with self.store.transaction() as conn:
            conn.execute(
                """
                INSERT INTO effect (
                    idempotency_key, run_id, step_id, tool, effect_class, args_hash,
                    state, result_ref, effect_id, attempt, created_at, updated_at,
                    started_at
                ) VALUES (?,?,?,?,?,?,?,NULL,?,?,?,?,NULL)
                ON CONFLICT (idempotency_key) DO UPDATE SET
                    state = excluded.state,
                    effect_id = excluded.effect_id,
                    attempt = excluded.attempt,
                    updated_at = excluded.updated_at,
                    result_ref = NULL,
                    started_at = NULL
                """,
                (
                    key, run_id, step_id, tool, effect_class, digest_of_args,
                    INTENDED, effect_id, attempt, now, now,
                ),
            )
        return Effect(
            self, key=key, effect_id=effect_id, run_id=run_id, step_id=step_id,
            tool=tool, effect_class=effect_class, args_hash=digest_of_args,
            attempt=attempt,
        )

    # --- reads ---------------------------------------------------------------

    def get(self, key: str) -> EffectRow | None:
        rows = self.store.query("SELECT * FROM effect WHERE idempotency_key = ?", (key,))
        return EffectRow.from_row(rows[0]) if rows else None

    def entries(self, run_id: str | None = None) -> list[EffectRow]:
        """Every effect, oldest first, optionally narrowed to one run."""
        if run_id is None:
            rows = self.store.query("SELECT * FROM effect ORDER BY created_at, rowid")
        else:
            rows = self.store.query(
                "SELECT * FROM effect WHERE run_id = ? ORDER BY created_at, rowid",
                (run_id,),
            )
        return [EffectRow.from_row(r) for r in rows]

    # --- row writes ----------------------------------------------------------

    def _store_started(self, key: str) -> None:
        now = utcnow().isoformat()
        with self.store.transaction() as conn:
            changed = conn.execute(
                "UPDATE effect SET state = ?, started_at = ?, updated_at = ? "
                "WHERE idempotency_key = ? AND state = ?",
                (STARTED, now, now, key, INTENDED),
            ).rowcount
        if not changed:
            raise EffectStateError(f"effect {key[:12]} is not in {INTENDED}")

    def _store_finished(self, key: str, state: str, result_ref: str) -> None:
        now = utcnow().isoformat()
        with self.store.transaction() as conn:
            changed = conn.execute(
                "UPDATE effect SET state = ?, result_ref = ?, updated_at = ? "
                "WHERE idempotency_key = ? AND state IN (?, ?)",
                (state, result_ref, now, key, INTENDED, STARTED),
            ).rowcount
        if not changed:
            raise EffectStateError(f"effect {key[:12]} is not open")

    # --- journal -------------------------------------------------------------

    def _journal(self, run_id: str) -> RunJournal:
        return RunJournal(self.writer, run_id)

    def _journal_intended(
        self,
        *,
        run_id: str,
        step_id: str,
        effect_id: str,
        key: str,
        tool: str,
        effect_class: str,
        args_digest: str,
        attempt: int,
    ) -> None:
        self._journal(run_id).emit(
            "effect_intended",
            {
                "effect_id": effect_id,
                "tool_name": tool,
                "effect_class": effect_class,
                "idempotency_key": key,
                "args_digest": args_digest,
                "attempt": attempt,
            },
            step_id=step_id,
        )

    def _journal_committed(
        self,
        *,
        run_id: str,
        step_id: str,
        effect_id: str,
        key: str,
        status: str,
        duration_ms: int,
        result_digest: str | None,
        error: str | None,
        attempt: int,
    ) -> None:
        self._journal(run_id).emit(
            "effect_committed",
            {
                "effect_id": effect_id,
                "idempotency_key": key,
                "status": status,
                "duration_ms": duration_ms,
                "result_digest": result_digest,
                "error": error,
                "attempt": attempt,
            },
            step_id=step_id,
        )


def get_ledger(cfg: Config | None = None) -> EffectLedger:
    """The ledger over this process's journal writer.

    Not cached itself: an `EffectLedger` is a method table over the writer, and the writer
    is the thing that is cached per file (`runtime.get_writer`).
    """
    from .runtime import get_writer

    return EffectLedger(lambda: get_writer(cfg))


def ledgered(effect_class: str) -> bool:
    """Whether a call of this class gets a ledger row at all.

    `read` does not: it changes nothing outside, so there is no question for a crash to
    leave behind, and journaling two synchronous events around every `time_now` would make
    the ledger mostly noise - which is how a record stops being read.
    """
    return effect_class in effects.EFFECTING


__all__ = [
    "ALLOWED",
    "COMMITTED",
    "FAILED",
    "INTENDED",
    "ORPHANED",
    "STARTED",
    "EFFECT_STATES",
    "TERMINAL",
    "Effect",
    "EffectLedger",
    "EffectRow",
    "EffectStateError",
    "get_ledger",
    "ledgered",
]
