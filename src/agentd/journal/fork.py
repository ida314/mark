"""Fork: rewinding a conversation without rewinding the world.

A fork is the user saying "no, go back to before that". It produces a **new run** whose
first event is `run_forked`, naming the run it came from and the position it came from:

    fork(run_id, seq) -> new run with parent_run_id, forked_from_seq
                         state rehydrated from the journal through `seq`
                         original run untouched; its journal is not rewritten

Three properties, and each is the reason the next one is possible.

## The parent is not touched, at all

No event is written into it, no ledger row moves, no checkpoint is taken of it, and nothing
about it is deleted. `state = fold(reduce, journal, initial)` means a run's history is the
run: rewriting it - even by appending "this was forked from" - would make the parent's own
fold depend on something that happened to somebody else. The lineage lives in the *child*,
which is the only run that needs to know.

## Nothing is undone, and that is the feature

This pass's *Must not* is "automatic side-effect reversal, including file rollback". So
forking past three `fs_write` calls leaves three written files, past a sent notification
leaves it sent, and the runtime says so in words the user can act on rather than restoring
a conversation that quietly disagrees with the disk. `Disclosure` is that statement as data;
`agent/observations.disclosure()` is the sentence, kept next to the other recovery wording
so the CLI and a future resumed turn cannot drift into saying two different things.

The disclosure reports what the journal *holds*, not what a plausible summary would be: the
counts are counts of announced effects and the paths come from the `tool_requested` the
loop recorded. A tool call the journal has no arguments for is named as such - session 4c
found the shape where an argument line silently drops the one identifying argument, and a
disclosure with an invented location would be the same failure with more confidence.

An effect that is still open at the end of the parent run, or that an earlier resume closed
as `uncertain`, is disclosed **separately** and is never counted among the things the run
did. "I did this" and "this may have happened" are different sentences; merging them in
either direction is the bug the `uncertain` vocabulary exists to prevent.

## A forked run does not inherit the parent's effect ledger

Deliberate, and the *Must not* again ("no cross-run result caching"). The idempotency key is
`hash(run_id, step_id, tool_name, canonical_args)`, so a call the new run makes has a key the
parent's row cannot answer for: the call will actually happen again. That is the honest
behaviour - a fork rewinds the conversation, and the second send is a second send - and it is
also exactly why the disclosure has to be accurate before the user decides to re-run
anything.

## Workers are atomic here too

A fork point inside an open worker is refused (`MidWorkerFork`), using session 4a's
derivation rather than a second one. Rehydrating a run with a worker in flight would hand
the new run an open delegation nothing can ever finish, and "workers are re-delegated, not
resumed" is the same rule that refuses a mid-worker checkpoint.
"""

from __future__ import annotations

import posixpath
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from ..ids import uuid7
from . import resume as R
from .checkpoints import Checkpoint, open_workers_at
from .render import LEADING_ARGS, brief_args
from .runtime import RunJournal
from .store import JournalError, JournalStore
from .writer import JournalWriter

# The argument that names a place on disk. One name, not a guessed list: the location line
# in a disclosure is only allowed to come from an argument whose meaning this module is sure
# of, and a `url` that looks path-shaped would otherwise be folded into a directory that
# does not exist.
PATH_ARG = "path"

COMMITTED = "committed"
FAILED = "failed"


class ForkError(JournalError):
    """A run could not be forked. Raised, never degraded into a fork that did less."""


class NoSuchForkPoint(ForkError):
    """The position asked for is not one this run ever reached."""


class MidWorkerFork(ForkError):
    """The fork point sits inside a delegation. Workers are re-delegated, not resumed."""


class RunExists(ForkError):
    """The new run already holds events, so `run_forked` would not be its first one."""


@dataclass(frozen=True)
class DisclosedCall:
    """One effecting call the parent run made after the fork point.

    Built from `resume.AnnouncedCall`, which is the fold both recovery paths share.
    """

    effect_id: str
    tool: str
    effect_class: str
    step_id: str
    intended_seq: int
    settled_seq: int | None
    status: str | None
    arguments: dict[str, Any] | None

    @classmethod
    def of(cls, call: R.AnnouncedCall) -> DisclosedCall:
        return cls(
            effect_id=call.effect_id, tool=call.tool, effect_class=call.effect_class,
            step_id=call.step_id, intended_seq=call.intended_seq,
            settled_seq=call.settled_seq, status=call.status, arguments=call.arguments,
        )

    @property
    def subject(self) -> str | None:
        """The call on one line, or `None` when the journal holds no arguments for it.

        `LEADING_ARGS` for the reason session 4c found the hard way: the line is truncated,
        and the argument that says *which* file or URL this was has to survive that.
        """
        if self.arguments is None:
            return None
        return f"{self.tool}({brief_args(self.arguments, lead=LEADING_ARGS)})"

    @property
    def name(self) -> str:
        """What to call it in a sentence. Says so when the arguments were never recorded,
        rather than printing an empty pair of brackets that reads as "no arguments"."""
        return self.subject or f"{self.tool} (arguments not recorded)"

    @property
    def location(self) -> str | None:
        """The place on disk this call was about, when it recorded an absolute path."""
        if not self.arguments:
            return None
        value = self.arguments.get(PATH_ARG)
        return value if isinstance(value, str) and value.startswith("/") else None


@dataclass(frozen=True)
class CallGroup:
    """Every disclosed call of one tool, together, with what they have in common.

    Grouped for the same reason session 4b groups orphans: one line naming three files is
    something a person reads, and three lines naming one file each is something they skip.
    """

    tool: str
    calls: tuple[DisclosedCall, ...]

    @property
    def location(self) -> str | None:
        """The directory every call in this group wrote inside, if there is one.

        Derived from the recorded paths and nothing else. `None` when any call has no path,
        when there is only one call (its own line already names the file), or when the only
        thing they share is `/` - a disclosure that says "3 files in /" has told the reader
        nothing while sounding like it has.
        """
        paths = [c.location for c in self.calls]
        if len(paths) < 2 or any(p is None for p in paths):
            return None
        common = posixpath.commonpath([posixpath.dirname(p) for p in paths if p])
        return None if common in ("", "/") else common + "/"


@dataclass(frozen=True)
class Disclosure:
    """What the parent run did after the fork point, as facts rather than as a sentence.

    `committed` is the pass file's list: effects the journal says landed. `unresolved` is
    the honest addition - an effect still open, or one an earlier resume closed as
    `uncertain`, may also have changed the world, and leaving it out of a fork disclosure
    would be a confident "here is everything I did" that is missing the email. `failed`
    effects are counted and named, and the wording states the error without inferring
    anything from it: session 3b's ruling that a failed call produced no effect is a
    ledger-level convenience, and a failure response does not prove the effect did not land
    - a fetch can fail after the server acted, which is why `web_fetch` is `unsafe_write`.
    Ruled by Dylan at the Pass 4/5 boundary, one rule for every tool so there is no
    per-class branch here. Collected for Pass 10 in `docs/records/pass-03-outcome.md`.
    """

    parent_run_id: str
    forked_from_seq: int
    committed: tuple[DisclosedCall, ...]
    unresolved: tuple[DisclosedCall, ...]
    failed: tuple[DisclosedCall, ...]

    @property
    def nothing_recorded(self) -> bool:
        """Whether the journal holds no effecting call after the fork point at all.

        The basis for saying "nothing outside the conversation changed": `effect_intended`
        is synchronous and precedes the handler, so its absence is evidence rather than
        hope. It is only as good as the file it read - a journal copied without its `-wal`
        is missing its most recent events (session 4c, open question 3).
        """
        return not (self.committed or self.unresolved or self.failed)

    @property
    def groups(self) -> tuple[CallGroup, ...]:
        """The committed calls, one group per tool, in the order they were intended."""
        return by_tool(self.committed)

    @property
    def unresolved_groups(self) -> tuple[CallGroup, ...]:
        return by_tool(self.unresolved)


def by_tool(calls: Sequence[DisclosedCall]) -> tuple[CallGroup, ...]:
    """One group per tool, first intended first."""
    order: list[str] = []
    grouped: dict[str, list[DisclosedCall]] = {}
    for call in calls:
        if call.tool not in grouped:
            grouped[call.tool] = []
            order.append(call.tool)
        grouped[call.tool].append(call)
    return tuple(CallGroup(tool=t, calls=tuple(grouped[t])) for t in order)


@dataclass(frozen=True)
class ForkPlan:
    """Everything a fork would do, worked out without writing anything."""

    parent_run_id: str
    forked_from_seq: int
    parent_last_seq: int
    state: R.ResumePlan
    disclosure: Disclosure

    @property
    def rehydration(self) -> R.Rehydration:
        """The parent's message list as of the fork point. Session 4b's spine, unchanged:
        previews and exact tool-call arguments, never bodies the journal does not hold."""
        return self.state.rehydration

    @property
    def checkpoint(self) -> Checkpoint | None:
        """The furthest-along checkpoint that does not overrun the fork point, or `None`."""
        return self.state.checkpoint

    @property
    def open_at_fork(self) -> tuple[R.Orphan, ...]:
        """Effects that were still open at the fork point itself.

        A call in flight when the user rewound past it. The new run inherits the
        conversation and not the ledger, so these are the parent's to reconcile with
        `agent journal resume`, and they are surfaced here so a fork does not quietly hide
        one.
        """
        return self.state.reconciliation.orphans


@dataclass(frozen=True)
class Forked:
    """What a fork actually wrote: one event, in one new run."""

    plan: ForkPlan
    run_id: str
    event_seq: int

    @property
    def parent_run_id(self) -> str:
        return self.plan.parent_run_id

    @property
    def forked_from_seq(self) -> int:
        return self.plan.forked_from_seq


# --- reading -----------------------------------------------------------------


def plan_fork(run_id: str, *, at_seq: int, store: JournalStore) -> ForkPlan:
    """Work out the fork at this position. Writes nothing, reads everything.

    Raises rather than adjusting: a fork point past the end of the run, or at a position
    that run never reached, is a question about a history that does not exist, and silently
    clamping it to the last seq would rewind to somewhere the user did not ask for.
    """
    events = store.read(run_id)
    if not events:
        raise R.NoSuchRun(f"run {run_id} has no events; there is nothing to fork")
    last_seq = events[-1].seq
    if at_seq < 1 or at_seq > last_seq:
        raise NoSuchForkPoint(
            f"run {run_id} has events at seq 1..{last_seq}; seq {at_seq} is not one of them"
        )
    open_workers = open_workers_at(store, run_id, at_seq)
    if open_workers:
        raise MidWorkerFork(
            f"run {run_id} had {len(open_workers)} worker(s) in flight at seq {at_seq} "
            f"({', '.join(w.worker_id for w in open_workers)}); a worker is re-delegated, "
            "not resumed, so a fork inside one would inherit a delegation nothing finishes"
        )
    return ForkPlan(
        parent_run_id=run_id,
        forked_from_seq=at_seq,
        parent_last_seq=last_seq,
        state=R.plan(run_id, store=store, through_seq=at_seq),
        disclosure=disclose(run_id, events, at_seq),
    )


def disclose(run_id: str, events: Sequence[Any], at_seq: int) -> Disclosure:
    """What this run did after `at_seq`, by effect status.

    "After" means the effect was announced after the fork point *or* settled after it. The
    second half matters: a call already in flight when the user rewound past it still landed
    afterwards, and a disclosure that filtered on the intent alone would leave out the one
    call the fork is most likely to be about.
    """
    after = [
        DisclosedCall.of(call)
        for call in R.announced_calls(events)
        if call.intended_seq > at_seq or (call.settled_seq or 0) > at_seq
    ]
    return Disclosure(
        parent_run_id=run_id,
        forked_from_seq=at_seq,
        committed=tuple(c for c in after if c.status == COMMITTED),
        unresolved=tuple(c for c in after if c.status is None or c.status == R.UNCERTAIN),
        failed=tuple(c for c in after if c.status == FAILED),
    )


# --- writing -----------------------------------------------------------------


def fork(
    run_id: str,
    *,
    at_seq: int,
    writer: JournalWriter,
    reason: str,
    new_run_id: str | None = None,
) -> Forked:
    """Open a new run from `run_id` at `at_seq`. The one call that writes.

    Writes exactly one event, `run_forked`, and writes it into the **new** run. Nothing is
    written into the parent, nothing is re-executed, and nothing is undone.

    Synchronous, like the other recovery events: a lineage that is still in a buffer is a
    new run nobody can trace back to where it came from, and it is the first thing anything
    built on this run will read.
    """
    # Same reason `resume()` flushes: `plan_fork` reads the file, and this process may be
    # holding the tail of the run being forked in memory.
    writer.flush()
    current = plan_fork(run_id, at_seq=at_seq, store=writer.store)
    child = new_run_id or str(uuid7())
    if child == run_id:
        raise ForkError("a fork's new run cannot be the run it forked from")
    if writer.store.last_seq(child) != 0:
        raise RunExists(
            f"run {child} already holds events; `run_forked` has to be the first event of "
            "the run it opens"
        )
    checkpoint = current.checkpoint
    event = RunJournal(writer, child).emit(
        "run_forked",
        {
            "parent_run_id": run_id,
            "forked_from_seq": at_seq,
            "reason": reason,
            # Null is a statement: this fork was rebuilt by folding the parent's journal,
            # because no checkpoint stands at or before the fork point.
            "checkpoint_id": checkpoint.checkpoint_id if checkpoint else None,
            # Pass 5's slot. A fork asked for by a person did not come out of a handoff, and
            # saying so is not the same as leaving the key out.
            "handoff_id": None,
        },
        sync=True,
    )
    if event is None:  # pragma: no cover - sync=True appends
        raise ForkError("run_forked was buffered; a fork nobody can trace is not a fork")
    return Forked(plan=current, run_id=child, event_seq=event.seq)


def summary(current: ForkPlan) -> str:
    """One line about a fork, for a log or a `--dry-run`. Not the disclosure: the words the
    user is asked to act on are in `agent/observations.py`, which is the one place any of
    this pass's user-facing sentences are written."""
    d = current.disclosure
    return (
        f"fork of run {current.parent_run_id} at seq {current.forked_from_seq} "
        f"of {current.parent_last_seq}: {len(current.rehydration.messages)} messages, "
        f"{len(d.committed)} committed effect(s) after that point, "
        f"{len(d.unresolved)} unresolved, {len(d.failed)} failed"
    )


__all__ = [
    "COMMITTED",
    "FAILED",
    "PATH_ARG",
    "CallGroup",
    "DisclosedCall",
    "Disclosure",
    "ForkError",
    "ForkPlan",
    "Forked",
    "MidWorkerFork",
    "NoSuchForkPoint",
    "RunExists",
    "by_tool",
    "disclose",
    "fork",
    "plan_fork",
    "summary",
]
