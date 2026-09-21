"""Checkpoints: where a run had got to, snapshotted at five boundaries.

A checkpoint is a **lossless mechanical snapshot for surviving a process failure**. It is
not a handoff, which is a lossy semantic compression for surviving a context limit (Pass 5),
and conflating the two is how a resume quietly loses work it claimed to have kept.

The governing invariant decides the shape of everything here:

    state = fold(reduce, journal, initial)

The journal is the source of truth. A checkpoint is an acceleration structure over it: it
**may lag the journal and may never disagree with it**, because the same state is always
recoverable by re-folding. Three consequences, each enforced rather than asserted:

1. **`covers_seq` is read before the announcement is journaled**, so the event that
   announces a checkpoint always sits *after* the position that checkpoint accounts for.
   `event_seq > covers_seq` is a CHECK in the schema (`store.SCHEMA_V3`), not a convention.
2. **Nothing conversational is copied.** `messages_ref` is a pointer - a run id and a
   position - and the messages themselves stay in the journal, where re-folding finds them.
   The journal only holds 200-character previews anyway (the archive in Postgres has the
   content), so a checkpoint that copied them would be a third, lossy copy.
3. **The journal is written first and the row second**, exactly as `ledger.py` does it. A
   crash between the two leaves a `checkpoint_written` event with no row - recoverable, and
   visibly so - rather than a row nothing announced.

## The five boundaries

    turn_end         `agent/loop.py`, as the turn record unwinds, after `agent_finished`
    worker_finished  `agent/subagents.py`, after the worker's result is journaled
    pre_effect       `tools/executor.py`, before an `unsafe_write` is even announced
    handoff          nobody, yet - Pass 5 owns the code path that produces a handoff
    manual           `agent journal checkpoint <run>`, a human asking for a marker

## Workers are the unit of atomicity

The architecture's rule is "do not checkpoint inside a worker in the first version", and
this module derives that rather than being told it. A snapshot is refused whenever the run
has a `worker_created` with no matching `worker_finished` at or before `covers_seq`. That
single rule covers all three live boundaries at once: a worker's own `turn_end` is refused
because the worker is still open, a worker's `pre_effect` likewise, and a nested worker
keeps its parent's `worker_finished` boundary quiet. Nothing has to thread "am I inside a
worker" through the tool surface to get it right.

So `open_workers[]` is both the slot the architecture asks for and the detector that
enforces the *Must not*. Today it is always empty in a written checkpoint - delegation is
awaited inside a step, so no worker is open at a boundary - and the day parallel workers
exist (Pass 6) it is the field that already knows.

## What is deliberately inert

`handoff_object` (Pass 5), `worker_results[]` (Pass 6), `pending_promotions[]` and
`memory_watermark` (Pass 7) are defined, stored and read back, and this pass writes nothing
into them. They are slots to fill, not a schema to migrate. The two lists are empty lists
and the two objects are `None`, and those are different statements on purpose: an empty
list is "there were none", a null is "this pass did not record one".
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from ..config import Config, get_config
from ..ids import utcnow, uuid7
from .events import CHECKPOINT_TRIGGERS
from .ledger import INTENDED, STARTED
from .runtime import RunJournal
from .store import JournalError, JournalStore
from .writer import JournalWriter

TRIGGERS: tuple[str, ...] = CHECKPOINT_TRIGGERS

# The event types a rehydration has to read to rebuild the model's message list. Recorded
# here because `messages_ref.message_events` counts only the first: session 2b decided that
# a tool result is described by `tool_finished` / `tool_failed` and is deliberately not a
# `message_appended`, so a count of message events is not a count of messages.
MESSAGE_EVENTS: tuple[str, ...] = ("message_appended",)
TOOL_RESULT_EVENTS: tuple[str, ...] = ("tool_finished", "tool_failed")


class CheckpointError(JournalError):
    """A checkpoint could not be written. Raised, never degraded into a missing snapshot."""


class MidWorkerCheckpoint(CheckpointError):
    """A boundary fired while a worker was still in flight. Workers are atomic."""


class NoOrchestrator(CheckpointError):
    """The run has no `agent_started` to name an orchestrator, so there is nothing to snapshot.

    Raised rather than filled in with a placeholder. A run in this state is real - session
    3b's `detached:<action_id>` runs hold two effect events and nothing else - and a
    checkpoint of one would name an orchestrator that never existed.
    """


@dataclass(frozen=True)
class WorkerRef:
    """One worker the run has not seen finish.

    `task_spec` is the 200-character preview from `worker_created`, which is what the
    journal holds; the full task is the `subagent_message` row in the archive. Named for
    the architecture's field rather than for the preview so the slot is recognisable, and
    said plainly here so nobody re-delegates from a truncated string by accident.
    """

    worker_id: str
    role: str
    task_spec: str
    status: str

    def as_dict(self) -> dict[str, str]:
        return {
            "worker_id": self.worker_id, "role": self.role,
            "task_spec": self.task_spec, "status": self.status,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> WorkerRef:
        return cls(
            worker_id=data["worker_id"], role=data["role"],
            task_spec=data["task_spec"], status=data["status"],
        )


@dataclass(frozen=True)
class MessagesRef:
    """The pointer that replaces a copy of the conversation.

    `run_id` is carried separately from the checkpoint's own run because a forked run
    (session 4d) takes its messages from its parent up to the fork point, and a pointer
    that could only ever mean "this run" would have to be re-invented there.
    """

    run_id: str
    through_seq: int
    message_events: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id, "through_seq": self.through_seq,
            "message_events": self.message_events,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> MessagesRef:
        return cls(
            run_id=data["run_id"], through_seq=data["through_seq"],
            message_events=data["message_events"],
        )


@dataclass(frozen=True)
class EffectsCursor:
    """How much of this run's effect record the snapshot accounts for.

    `last_effect_seq` is a journal position: the seq of the last `effect_*` event at or
    before `covers_seq`, or 0 for a run that has made no effecting call. `effects` and
    `open_keys` come from the ledger table, which has no position of its own - a second
    attempt at one logical call updates the row it already had - so they are as of the
    moment the snapshot was taken. With the writer flushed first and one turn running,
    those are the same instant; if they ever diverge, the journal position is the one that
    is reproducible by re-folding.

    `open_keys` is the evidence 4b reconciles against: an `unsafe_write` still at
    `intended` or `started` here is one that may have happened.
    """

    last_effect_seq: int
    effects: int
    open_keys: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "last_effect_seq": self.last_effect_seq, "effects": self.effects,
            "open_keys": list(self.open_keys),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> EffectsCursor:
        return cls(
            last_effect_seq=data["last_effect_seq"], effects=data["effects"],
            open_keys=tuple(data["open_keys"]),
        )


@dataclass(frozen=True)
class Checkpoint:
    """One snapshot, as written and as read back."""

    checkpoint_id: str
    run_id: str
    covers_seq: int
    event_seq: int
    created_at: str
    trigger: str
    orchestrator_id: str
    messages_ref: MessagesRef
    open_workers: tuple[WorkerRef, ...]
    effects_cursor: EffectsCursor
    # --- the slots later passes fill. Nothing here writes them; see the module docstring.
    handoff_object: dict[str, Any] | None = None   # Pass 5
    worker_results: tuple[dict[str, Any], ...] = ()   # Pass 6
    pending_promotions: tuple[dict[str, Any], ...] = ()   # Pass 7
    memory_watermark: dict[str, Any] | None = None   # Pass 7

    def as_dict(self) -> dict[str, Any]:
        return {
            "checkpoint_id": self.checkpoint_id,
            "run_id": self.run_id,
            "covers_seq": self.covers_seq,
            "event_seq": self.event_seq,
            "created_at": self.created_at,
            "trigger": self.trigger,
            "orchestrator_id": self.orchestrator_id,
            "messages_ref": self.messages_ref.as_dict(),
            "open_workers": [w.as_dict() for w in self.open_workers],
            "effects_cursor": self.effects_cursor.as_dict(),
            "handoff_object": self.handoff_object,
            "worker_results": list(self.worker_results),
            "pending_promotions": list(self.pending_promotions),
            "memory_watermark": self.memory_watermark,
        }

    @classmethod
    def from_row(cls, row: Any) -> Checkpoint:
        return cls(
            checkpoint_id=row["checkpoint_id"],
            run_id=row["run_id"],
            covers_seq=row["covers_seq"],
            event_seq=row["event_seq"],
            created_at=row["created_at"],
            trigger=row["trigger"],
            orchestrator_id=row["orchestrator_id"],
            messages_ref=MessagesRef.from_dict(json.loads(row["messages_ref"])),
            open_workers=tuple(
                WorkerRef.from_dict(w) for w in json.loads(row["open_workers"])
            ),
            effects_cursor=EffectsCursor.from_dict(json.loads(row["effects_cursor"])),
            handoff_object=_loads_or_none(row["handoff_object"]),
            worker_results=tuple(json.loads(row["worker_results"])),
            pending_promotions=tuple(json.loads(row["pending_promotions"])),
            memory_watermark=_loads_or_none(row["memory_watermark"]),
        )


def _loads_or_none(text: str | None) -> dict[str, Any] | None:
    """NULL stays None. There is no `{}` fallback: an empty object would read as a recorded
    measurement, and "Pass 7 has not landed" is not a measurement."""
    return None if text is None else json.loads(text)


def _dumps(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


@dataclass
class _Snapshot:
    """What the journal and the ledger say, read once, before anything is written."""

    covers_seq: int
    orchestrator_id: str
    messages_ref: MessagesRef
    open_workers: tuple[WorkerRef, ...]
    effects_cursor: EffectsCursor
    build_ms: int = 0


class Checkpointer:
    """Writes and reads checkpoints for the journal file a writer owns.

    Constructed around a `JournalWriter` - or a callable returning one - for the same
    reason `EffectLedger` is: the run's events, its effect rows and its snapshots must land
    in one file, and an object built before anything decided which file that is would
    otherwise pin itself to the wrong one.
    """

    def __init__(
        self,
        writer: JournalWriter | JournalStore | Callable[[], JournalWriter],
        *,
        cfg: Config | None = None,
    ) -> None:
        self._writer = writer
        self._cfg = cfg

    @classmethod
    def reading(cls, store: JournalStore, *, cfg: Config | None = None) -> Checkpointer:
        """A checkpointer over a store, for the read side only.

        Session 4b's `resume.plan()` is read-only and has to stay that way - it is what a
        `--dry-run` runs and what the CLI prints before anybody has agreed to anything - so
        it must not open an append path as a side effect of looking. Asking for the writer
        on one of these raises rather than quietly opening one.
        """
        return cls(store, cfg=cfg)

    @property
    def writer(self) -> JournalWriter:
        writer = self._writer
        if isinstance(writer, JournalStore):
            raise CheckpointError(
                "this checkpointer was opened over a store for reading and cannot write"
            )
        return writer if isinstance(writer, JournalWriter) else writer()

    @property
    def store(self) -> JournalStore:
        source = self._writer
        return source if isinstance(source, JournalStore) else self.writer.store

    # --- write ---------------------------------------------------------------

    def write(self, run_id: str, *, trigger: str) -> Checkpoint:
        """Snapshot `run_id` at this boundary, announce it, and store it.

        Raises rather than returning None on anything unexpected: `MidWorkerCheckpoint`
        when a worker is in flight, `NoOrchestrator` when the run has no `agent_started`,
        `CheckpointError` for an unknown trigger or an announcement that did not reach
        disk. `checkpoint_at` is the boundary-facing wrapper that turns the first of those
        into "no checkpoint here", because refusing a mid-worker snapshot is the rule and
        not a failure.
        """
        if trigger not in TRIGGERS:
            raise CheckpointError(
                f"unknown checkpoint trigger {trigger!r}; expected one of {', '.join(TRIGGERS)}"
            )
        started = time.perf_counter()
        writer = self.writer
        # Flushed first so that the snapshot and `covers_seq` describe the same instant.
        # Without it the buffered tail - `worker_created`, the tool events - would be
        # journaled *ahead* of the checkpoint event (the writer carries the buffer into the
        # same transaction) while sitting outside `covers_seq`, and a resume would replay
        # events the snapshot had already accounted for.
        writer.flush()
        snap = self._snapshot(writer.store, run_id)
        if snap.open_workers:
            raise MidWorkerCheckpoint(
                f"run {run_id}: {len(snap.open_workers)} worker(s) still in flight "
                f"({', '.join(w.worker_id for w in snap.open_workers)}); "
                "workers are the unit of atomicity and are re-delegated, not resumed"
            )
        checkpoint_id = str(uuid7())
        created_at = utcnow().isoformat()
        body = {
            "checkpoint_id": checkpoint_id,
            "run_id": run_id,
            "covers_seq": snap.covers_seq,
            "created_at": created_at,
            "trigger": trigger,
            "orchestrator_id": snap.orchestrator_id,
            "messages_ref": snap.messages_ref.as_dict(),
            "open_workers": [],
            "effects_cursor": snap.effects_cursor.as_dict(),
            "handoff_object": None,
            "worker_results": [],
            "pending_promotions": [],
            "memory_watermark": None,
        }
        # The stored size of this snapshot, which is the storage half of "checkpoint write
        # overhead". `event_seq` is not in it yet - it is one small integer, assigned by the
        # append below - so this undercounts the row by that much and by SQLite's own
        # per-row overhead.
        size = len(_dumps(body).encode())
        event = RunJournal(writer, run_id).emit(
            "checkpoint_written",
            {
                "checkpoint_id": checkpoint_id,
                "trigger": trigger,
                "covers_seq": snap.covers_seq,
                # Pass 7's. Null rather than {} - see the field's comment in `events.py`.
                "memory_watermark": None,
                # Message *events*, not messages: a tool result is a `tool_finished`, not a
                # `message_appended` (session 2b).
                "messages": snap.messages_ref.message_events,
                "bytes": size,
                # Building the snapshot only: the two commits that follow this event are
                # what a boundary actually costs, and they cannot be measured from inside
                # the event that starts them. `scripts/bench_checkpoints.py` times the whole
                # call.
                "duration_ms": snap.build_ms,
                "location": str(writer.store.path),
            },
            sync=True,
        )
        if event is None:  # pragma: no cover - `checkpoint_written` is in SYNC_TYPES
            raise CheckpointError(
                "checkpoint_written was buffered; a snapshot whose announcement is not on "
                "disk is a snapshot nothing points at"
            )
        self._insert(body, event_seq=event.seq)
        checkpoint = Checkpoint(
            checkpoint_id=checkpoint_id,
            run_id=run_id,
            covers_seq=snap.covers_seq,
            event_seq=event.seq,
            created_at=created_at,
            trigger=trigger,
            orchestrator_id=snap.orchestrator_id,
            messages_ref=snap.messages_ref,
            open_workers=(),
            effects_cursor=snap.effects_cursor,
        )
        _record_overhead(trigger, time.perf_counter() - started, size)
        return checkpoint

    def _insert(self, body: dict[str, Any], *, event_seq: int) -> None:
        with self.store.transaction() as conn:
            conn.execute(
                """
                INSERT INTO checkpoint (
                    checkpoint_id, run_id, covers_seq, event_seq, created_at, trigger,
                    orchestrator_id, messages_ref, open_workers, effects_cursor,
                    handoff_object, worker_results, pending_promotions, memory_watermark
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    body["checkpoint_id"], body["run_id"], body["covers_seq"], event_seq,
                    body["created_at"], body["trigger"], body["orchestrator_id"],
                    _dumps(body["messages_ref"]), _dumps(body["open_workers"]),
                    _dumps(body["effects_cursor"]),
                    None if body["handoff_object"] is None else _dumps(body["handoff_object"]),
                    _dumps(body["worker_results"]), _dumps(body["pending_promotions"]),
                    None if body["memory_watermark"] is None else _dumps(body["memory_watermark"]),
                ),
            )

    # --- the snapshot --------------------------------------------------------

    def _snapshot(self, store: JournalStore, run_id: str) -> _Snapshot:
        started = time.perf_counter()
        covers_seq = store.last_seq(run_id)
        if covers_seq == 0:
            raise NoOrchestrator(f"run {run_id} has no events to snapshot")
        snap = _Snapshot(
            covers_seq=covers_seq,
            orchestrator_id=_orchestrator_id(store, run_id, covers_seq),
            messages_ref=MessagesRef(
                run_id=run_id,
                through_seq=covers_seq,
                message_events=_count_types(store, run_id, covers_seq, MESSAGE_EVENTS),
            ),
            open_workers=open_workers_at(store, run_id, covers_seq),
            effects_cursor=_effects_cursor(store, run_id, covers_seq),
        )
        snap.build_ms = int((time.perf_counter() - started) * 1000)
        return snap

    # --- read ----------------------------------------------------------------

    def get(self, checkpoint_id: str) -> Checkpoint | None:
        rows = self.store.query(
            "SELECT * FROM checkpoint WHERE checkpoint_id = ?", (checkpoint_id,)
        )
        return Checkpoint.from_row(rows[0]) if rows else None

    def latest(self, run_id: str) -> Checkpoint | None:
        """The furthest-along snapshot of a run. Where session 4b's resume starts."""
        rows = self.store.query(
            "SELECT * FROM checkpoint WHERE run_id = ? ORDER BY event_seq DESC LIMIT 1",
            (run_id,),
        )
        return Checkpoint.from_row(rows[0]) if rows else None

    def at(self, run_id: str, through_seq: int) -> Checkpoint | None:
        """The furthest-along snapshot that does not overrun a position.

        Session 4d forks a run at a seq the user chose, and a snapshot whose `covers_seq` is
        past that seq accounts for events the fork is deliberately leaving behind. Filtered
        on `covers_seq` rather than on `event_seq` for exactly that reason: the announcement
        always sits after the position it covers, so filtering on the wrong one of the two
        would pick up a checkpoint that knows more than the fork point does.
        """
        rows = self.store.query(
            "SELECT * FROM checkpoint WHERE run_id = ? AND covers_seq <= ? "
            "ORDER BY covers_seq DESC, event_seq DESC LIMIT 1",
            (run_id, through_seq),
        )
        return Checkpoint.from_row(rows[0]) if rows else None

    def entries(self, run_id: str | None = None) -> list[Checkpoint]:
        """Every checkpoint, oldest first, optionally narrowed to one run."""
        if run_id is None:
            rows = self.store.query("SELECT * FROM checkpoint ORDER BY created_at, rowid")
        else:
            rows = self.store.query(
                "SELECT * FROM checkpoint WHERE run_id = ? ORDER BY event_seq", (run_id,)
            )
        return [Checkpoint.from_row(r) for r in rows]


# --- deriving a snapshot from the journal ------------------------------------


def _orchestrator_id(store: JournalStore, run_id: str, covers_seq: int) -> str:
    """Who owns this run: the turn that opened it.

    A run is opened by exactly one top-level `agent_started` - a worker's turn is tagged
    with `worker_id` and belongs to the run that created it - so the first untagged one
    names the orchestrator. For most channels this equals `run_id`, and for the REPL and
    Telegram it does not, because those mint a run id before the turn exists (session 2c);
    they are different identities that happen to coincide.
    """
    rows = store.query(
        """
        SELECT payload FROM journal
         WHERE run_id = ? AND seq <= ? AND type = 'agent_started'
           AND json_extract(payload, '$.worker_id') IS NULL
         ORDER BY seq LIMIT 1
        """,
        (run_id, covers_seq),
    )
    if not rows:
        raise NoOrchestrator(
            f"run {run_id} has no top-level agent_started at or before seq {covers_seq}, "
            "so there is no orchestrator to name"
        )
    return str(json.loads(rows[0]["payload"])["turn_id"])


def _count_types(
    store: JournalStore, run_id: str, covers_seq: int, types: Sequence[str]
) -> int:
    marks = ",".join("?" * len(types))
    rows = store.query(
        f"SELECT COUNT(*) AS n FROM journal WHERE run_id = ? AND seq <= ? AND type IN ({marks})",
        (run_id, covers_seq, *types),
    )
    return int(rows[0]["n"])


def open_workers_at(store: JournalStore, run_id: str, covers_seq: int) -> tuple[WorkerRef, ...]:
    """Workers created and not yet finished at this position, in creation order.

    Public because "a worker is in flight here" is a fact about a journal position and not
    about checkpointing: session 4d's fork asks the same question of the point a user wants
    to rewind to, and a second implementation of it would be free to disagree with this one.
    """
    rows = store.query(
        """
        SELECT type, payload FROM journal
         WHERE run_id = ? AND seq <= ? AND type IN ('worker_created', 'worker_finished')
         ORDER BY seq
        """,
        (run_id, covers_seq),
    )
    open_workers: dict[str, WorkerRef] = {}
    for row in rows:
        payload = json.loads(row["payload"])
        worker_id = payload["worker_id"]
        if row["type"] == "worker_created":
            open_workers[worker_id] = WorkerRef(
                worker_id=worker_id,
                role=payload["role"],
                task_spec=payload["task_preview"],
                status="running",
            )
        else:
            open_workers.pop(worker_id, None)
    return tuple(open_workers.values())


def _effects_cursor(store: JournalStore, run_id: str, covers_seq: int) -> EffectsCursor:
    rows = store.query(
        """
        SELECT COALESCE(MAX(seq), 0) AS s FROM journal
         WHERE run_id = ? AND seq <= ? AND type IN ('effect_intended', 'effect_committed')
        """,
        (run_id, covers_seq),
    )
    last_effect_seq = int(rows[0]["s"])
    ledger_rows = store.query(
        "SELECT idempotency_key, state FROM effect WHERE run_id = ? ORDER BY created_at, rowid",
        (run_id,),
    )
    return EffectsCursor(
        last_effect_seq=last_effect_seq,
        effects=len(ledger_rows),
        open_keys=tuple(
            r["idempotency_key"] for r in ledger_rows if r["state"] in (INTENDED, STARTED)
        ),
    )


# --- the boundary door -------------------------------------------------------


def enabled(cfg: Config | None = None) -> bool:
    """The feature flag, defined once. Every boundary asks through `checkpoint_at`."""
    return (cfg or get_config()).checkpoints.enabled


def get_checkpointer(cfg: Config | None = None) -> Checkpointer:
    """A checkpointer over this process's journal writer, resolved on use."""
    from .runtime import get_writer

    return Checkpointer(lambda: get_writer(cfg), cfg=cfg)


def checkpoint_at(
    trigger: str,
    *,
    run_id: str | None,
    writer: JournalWriter | None = None,
    cfg: Config | None = None,
) -> Checkpoint | None:
    """Write the checkpoint for a boundary, or explain by returning None.

    None means one of three things, and all three are rules rather than failures: the
    feature is switched off, the caller has no run to snapshot (a detached tool call, which
    session 3b keys under `detached:<action_id>` and which has no orchestrator), or a worker
    is in flight and workers are atomic. Anything else raises - a boundary that cannot
    checkpoint when it was asked to is not something to discover on the next crash.
    """
    if not run_id or not enabled(cfg):
        return None
    checkpointer = (
        Checkpointer(writer, cfg=cfg) if writer is not None else get_checkpointer(cfg)
    )
    try:
        return checkpointer.write(run_id, trigger=trigger)
    except MidWorkerCheckpoint:
        return None


# --- overhead ----------------------------------------------------------------
#
# Pass 10 owns the reporting of checkpoint write overhead, and 2a/2b/2c each closed with
# "the fsync cost is unmeasured". This keeps the number for the current process so the
# bench script and a future `agent journal checkpoints` can report it without a second
# instrumentation path. It is in-memory, per-process, and nothing folds it.

OVERHEAD: dict[str, dict[str, float]] = {}


def _record_overhead(trigger: str, seconds: float, size: int) -> None:
    row = OVERHEAD.setdefault(trigger, {"n": 0, "total_ms": 0.0, "max_ms": 0.0, "bytes": 0})
    row["n"] += 1
    row["total_ms"] += seconds * 1000
    row["max_ms"] = max(row["max_ms"], seconds * 1000)
    row["bytes"] += size


__all__ = [
    "MESSAGE_EVENTS",
    "OVERHEAD",
    "TOOL_RESULT_EVENTS",
    "TRIGGERS",
    "Checkpoint",
    "CheckpointError",
    "Checkpointer",
    "EffectsCursor",
    "MessagesRef",
    "MidWorkerCheckpoint",
    "NoOrchestrator",
    "WorkerRef",
    "checkpoint_at",
    "enabled",
    "get_checkpointer",
    "open_workers_at",
]
