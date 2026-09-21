"""Resume: picking a run up after the process that was running it died.

Three things happen here, in this order, and the order is the whole design:

1. **Fold the journal.** `state = fold(reduce, journal, initial)`. The checkpoint is read
   because it says where the run had got to cheaply, but nothing here *depends* on one -
   a run recorded before `[checkpoints] enabled` was ever switched on resumes exactly the
   same way, from seq 0. That is what "mechanical full rehydration" means, and it is why
   this module works on runs that predate session 4a.
2. **Reconcile the effects the crash interrupted**, by effect class:

       read              nothing to reconcile: a read never got a ledger row (`ledgered`),
                         because nothing outside changed and there is no question to answer
       idempotent_write  may be re-executed; re-execution converges
       unsafe_write      never auto-retried. Surfaced with its recorded arguments.

3. **Announce it**, with `run_resumed` as the next event in the run, and close each
   orphaned effect with `effect_committed(status="uncertain")` so a later fold sees a
   terminal state rather than an intent dangling for ever.

## Orphans are found in the journal, not in the ledger

The ledger is derived from the journal and may lag it: `intend()` journals `effect_intended`
and *then* writes the row, so a crash in between leaves an announced effect with no row at
all. Reading the `effect` table for open rows would miss exactly that case - a call that was
announced, may have run, and left no row to find. So the fold over `effect_intended` /
`effect_committed` decides *which* effects are open, and the ledger row is consulted only
for the one bit the journal cannot reproduce (session 3b): whether the handler had been
entered.

    row at `intended`   the handler was never awaited - `dispatched()` commits before it
    row at `started`    the handler was entered and never reported back
    no row              unknown; the announcement reached disk and the row did not

That distinction is carried as `Orphan.evidence` and it never changes the disposition. An
`unsafe_write` is never auto-retried whatever the evidence says; the evidence is there so
that whoever has to tell the user can say "recorded but never started" instead of the
uniformly frightening "this may have happened".

## The arguments come from `tool_requested`

The ledger holds an `args_hash`, not the arguments, and `result_ref` - the pointer to the
`actions` row that does hold them - is NULL until the effect reaches a terminal state,
which is precisely what an orphan never did. So the only durable copy of an orphaned call's
arguments is the `tool_requested` event in this journal. Reconciliation reads it, because a
prompt that cannot say *which* URL it is asking about trains the user to confirm blind, and
that is the outcome `web_fetch`'s `unsafe_write` ruling exists to prevent (pass-03 outcome,
the two rulings settled at the Pass 3/4 boundary). Several orphans of one tool are grouped
into one `UncertainGroup` for the same reason: one prompt naming four URLs, not four
prompts naming none.

## What this module deliberately does not do

It does not re-execute anything. "`idempotent_write` may be re-executed" is a permission
recorded against the orphan, not an action taken here: the only thing that can re-run a
tool call is a turn, and running one is the continuation path that Pass 5 (handoff) and
4c (the `uncertain` observation the orchestrator acts on) own. It also does not reverse
anything - automatic side-effect reversal is this pass's *Must not*, and the disclosure is
the feature.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from ..tools import effects
from .checkpoints import Checkpoint, Checkpointer
from .ledger import INTENDED, STARTED, EffectLedger
from .render import LEADING_ARGS, brief_args
from .runtime import RunJournal
from .store import JournalError, JournalStore
from .writer import JournalWriter

# --- dispositions ------------------------------------------------------------

RETRY = "retry"
UNCERTAIN = "uncertain"

# The pass file's reconciliation table, as data. Total over `effects.EFFECT_CLASSES`, so a
# fourth class cannot quietly inherit the behaviour of whichever branch was written first;
# `disposition()` raises for anything outside the vocabulary rather than bucketing it.
#
# `read` is in here because the vocabulary has three classes and a map missing one is a
# `KeyError` waiting for the day somebody journals one. It is unreachable today and asserted
# to be: a `read` gets no ledger row and no `effect_*` events at all, so it cannot become an
# orphan. That is also why `memory_search` - the runtime's most-called tool, ruled `read` at
# the Pass 3/4 boundary - can never be surfaced to the user as uncertain.
DISPOSITIONS: dict[str, str] = {
    effects.READ: RETRY,
    effects.IDEMPOTENT_WRITE: RETRY,
    effects.UNSAFE_WRITE: UNCERTAIN,
}

# What the ledger row says about whether the call was ever entered. Not a disposition.
NEVER_DISPATCHED = "never_dispatched"
MAY_HAVE_RUN = "may_have_run"
UNKNOWN = "unknown"

EVIDENCE: dict[str | None, str] = {
    INTENDED: NEVER_DISPATCHED,
    STARTED: MAY_HAVE_RUN,
    None: UNKNOWN,
}

# The run states a plan can report. `no_orchestrator` is session 3b's `detached:<action_id>`
# run - a queued approval replayed long after its turn ended - which has effects worth
# reconciling and no turn to continue.
INTERRUPTED = "interrupted"
COMPLETE = "complete"
NO_ORCHESTRATOR = "no_orchestrator"


class ResumeError(JournalError):
    """A run could not be resumed. Raised, never degraded into an empty plan."""


class NoSuchRun(ResumeError):
    """There are no events under this run id, so there is nothing to fold."""


def disposition(effect_class: str) -> str:
    """What may be done about an interrupted call of this class."""
    try:
        return DISPOSITIONS[effect_class]
    except KeyError:
        raise ResumeError(
            f"no reconciliation rule for effect_class {effect_class!r}; "
            f"expected one of {', '.join(DISPOSITIONS)}"
        ) from None


# --- what the crash left behind ----------------------------------------------


@dataclass(frozen=True)
class Orphan:
    """One effecting call that was announced and never reported back.

    `arguments` is `None` when the journal holds no `tool_requested` for this call - a
    detached call or an MCP caller, neither of which goes through the loop that emits it.
    `None` and not `{}`: an empty dict reads as "called with no arguments", which is a
    different and answerable statement.
    """

    effect_id: str
    idempotency_key: str
    tool: str
    effect_class: str
    step_id: str
    attempt: int
    intended_seq: int
    ledger_state: str | None
    arguments: dict[str, Any] | None
    arguments_seq: int | None
    # The model's id for the call this effect was announced by, when the journal holds the
    # request. `None` for a caller that never went through the loop (detached, MCP), which
    # is also a caller whose call sits in nobody's message list. Only unique within a step,
    # so `(step_id, call_id)` is the join and `call_id` alone is not.
    call_id: str | None = None

    @property
    def disposition(self) -> str:
        return disposition(self.effect_class)

    @property
    def evidence(self) -> str:
        return EVIDENCE[self.ledger_state]

    @property
    def subject(self) -> str | None:
        """The call on one line, for whoever has to name it to the user.

        `None` when the arguments were not recorded, so that a caller has to decide what to
        say about a call it cannot describe instead of printing an empty pair of brackets.
        """
        if self.arguments is None:
            return None
        return f"{self.tool}({brief_args(self.arguments, lead=LEADING_ARGS)})"


@dataclass(frozen=True)
class UncertainGroup:
    """Every orphan of one tool, together.

    The grouping is a requirement rather than a convenience (pass-03 outcome, the rulings
    settled at the Pass 3/4 boundary): a resume with four interrupted fetches asks once and
    names all four, because four separate prompts are four chances to confirm blind.
    """

    tool: str
    effect_class: str
    orphans: tuple[Orphan, ...]

    @property
    def subjects(self) -> tuple[str | None, ...]:
        return tuple(o.subject for o in self.orphans)


@dataclass(frozen=True)
class AnnouncedCall:
    """One effecting call the journal announced, and how it ended - including "it did not".

    Added by session 4c, which needed the *settled* effects as well as the open ones. A tool
    call whose `tool_finished` died in the buffer leaves the same dangling call in the
    rebuilt message list as an orphan does, and the three cases are three different things
    to say: `committed` means it happened and only the result text was lost, `failed` means
    it resolved without producing its effect, and `uncertain` means an earlier resume
    already gave up on it. Without this, all three read as "never announced", which is the
    one sentence that is wrong about every one of them.

    `status` is `None` while the effect is open - that call is also in `orphans`.
    """

    effect_id: str
    tool: str
    effect_class: str
    step_id: str
    call_id: str | None
    status: str | None


@dataclass(frozen=True)
class Reconciliation:
    """What the crash left open in one run, and what may be done about each of them."""

    run_id: str
    orphans: tuple[Orphan, ...]
    announced: tuple[AnnouncedCall, ...] = ()

    @property
    def retryable(self) -> tuple[Orphan, ...]:
        return tuple(o for o in self.orphans if o.disposition == RETRY)

    @property
    def uncertain(self) -> tuple[Orphan, ...]:
        return tuple(o for o in self.orphans if o.disposition == UNCERTAIN)

    @property
    def groups(self) -> tuple[UncertainGroup, ...]:
        """The uncertain orphans, one group per tool, in the order they were intended."""
        order: list[str] = []
        by_tool: dict[str, list[Orphan]] = {}
        for orphan in self.uncertain:
            if orphan.tool not in by_tool:
                by_tool[orphan.tool] = []
                order.append(orphan.tool)
            by_tool[orphan.tool].append(orphan)
        return tuple(
            UncertainGroup(
                tool=tool,
                effect_class=by_tool[tool][0].effect_class,
                orphans=tuple(by_tool[tool]),
            )
            for tool in order
        )


# --- what the run was saying -------------------------------------------------


@dataclass(frozen=True)
class ToolCallRef:
    """One tool call the model made, rebuilt exactly.

    The arguments are the only part of a turn's message list the journal holds in full -
    `tool_requested.args` is the parsed call, not a preview - which is what makes an
    orphaned `web_fetch`'s URL recoverable at all.
    """

    call_id: str
    name: str
    arguments: dict[str, Any]
    seq: int
    visible: bool
    known: bool


@dataclass(frozen=True)
class RehydratedMessage:
    """One entry in the run's message list, as the journal recorded it.

    `preview` is a preview: up to 200 characters with its whitespace collapsed
    (`events.preview`). It is **not** the body and must never be handed to a model as one -
    the bodies live in the Postgres archive, which `Rehydration.archive` points at. `chars`
    is the real length, so the size of what is missing is always visible.
    """

    seq: int
    role: str
    actor: str
    chars: int
    preview: str
    source: str
    step_id: str | None = None
    trust: str | None = None
    tool_call_id: str | None = None
    calls: tuple[ToolCallRef, ...] = ()

    @property
    def truncated(self) -> bool:
        """Whether the journal is holding less than was said."""
        return self.chars > len(self.preview)


@dataclass(frozen=True)
class ArchiveRef:
    """Where the full bodies of this run's messages are: `raw_events`, by turn.

    A pointer for the same reason `messages_ref` is one. Nullable as a whole, because a
    detached run has no turn and therefore no archive rows - which is a fact about that run,
    not a missing value.
    """

    session_id: str
    turn_id: str


@dataclass(frozen=True)
class Rehydration:
    """A run's message list, folded out of the journal.

    Orchestrator-only: a worker's messages are in this same run, tagged with `worker_id`,
    and they stay out. "Worker transcripts never enter orchestrator context outside debug
    mode" is a rule about what a resumed turn may be told, and a rehydration that quietly
    re-injected them would break it in the one place nobody would look. `worker_events`
    counts what was left out so the omission is visible rather than silent.
    """

    run_id: str
    from_seq: int
    through_seq: int
    replayed_events: int
    messages: tuple[RehydratedMessage, ...]
    worker_events: int
    turn_id: str | None
    session_id: str | None
    archive: ArchiveRef | None
    steps: int
    status: str | None

    @property
    def truncated(self) -> tuple[RehydratedMessage, ...]:
        """The messages whose bodies the journal does not hold in full.

        Every resumed turn of any length has some. It is a property rather than a warning
        because the caller that continues the run has to decide what to do about it, and
        the decision cannot be made here.
        """
        return tuple(m for m in self.messages if m.truncated)


@dataclass(frozen=True)
class ResumePlan:
    """Everything a resume would do, worked out without writing anything."""

    run_id: str
    state: str
    checkpoint: Checkpoint | None
    rehydration: Rehydration
    reconciliation: Reconciliation

    @property
    def from_seq(self) -> int:
        return self.rehydration.through_seq

    @property
    def needs_resume(self) -> bool:
        """Whether there is anything to record.

        A run killed at `turn_end` is complete and holds nothing open: resuming it correctly
        means doing nothing at all, and writing `run_resumed` into a finished run would put
        an event after `agent_finished` that says a resume happened and nothing followed.
        """
        return self.state != COMPLETE or bool(self.reconciliation.orphans)


@dataclass(frozen=True)
class Resumed:
    """What a resume actually wrote."""

    plan: ResumePlan
    applied: bool
    event_seq: int | None = None
    orphaned_keys: tuple[str, ...] = ()
    rowless_effects: tuple[str, ...] = ()


# --- reading -----------------------------------------------------------------


def plan(run_id: str, *, store: JournalStore) -> ResumePlan:
    """Work out how this run would be resumed. Writes nothing, reads everything.

    Reads the journal *off disk*, which is the right thing for the case this exists for -
    the process being asked about is dead and its buffer died with it - and a trap for a
    caller that has just written events of its own through a buffering writer. `resume()`
    flushes before it plans for exactly that reason; a caller planning from inside a live
    process should do the same.
    """
    events = store.read(run_id)
    if not events:
        raise NoSuchRun(f"run {run_id} has no events; there is nothing to resume")
    rehydration = _rehydrate(run_id, events)
    reconciliation = _reconcile(run_id, events, store)
    if rehydration.turn_id is None:
        state = NO_ORCHESTRATOR
    elif rehydration.status is not None:
        state = COMPLETE
    else:
        state = INTERRUPTED
    return ResumePlan(
        run_id=run_id,
        state=state,
        checkpoint=Checkpointer.reading(store).latest(run_id),
        rehydration=rehydration,
        reconciliation=reconciliation,
    )


def _rehydrate(run_id: str, events: Sequence[Any]) -> Rehydration:
    messages: list[RehydratedMessage] = []
    calls_by_step: dict[str, list[ToolCallRef]] = {}
    worker_events = 0
    turn_id: str | None = None
    session_id: str | None = None
    status: str | None = None
    steps = 0
    for event in events:
        payload = event.payload
        if payload.get("worker_id") is not None:
            # A worker's own events, in the caller's run. Counted, never folded into the
            # orchestrator's message list.
            worker_events += 1
            continue
        step_id = payload.get("step_id")
        if event.type == "agent_started":
            turn_id = payload["turn_id"]
            session_id = payload["session_id"]
        elif event.type == "agent_finished":
            status = payload["status"]
            steps = payload["steps"]
        elif event.type == "message_appended":
            messages.append(
                RehydratedMessage(
                    seq=event.seq, role=payload["role"], actor=payload["actor"],
                    chars=payload["chars"], preview=payload["preview"],
                    source=event.type, step_id=step_id, trust=payload.get("trust"),
                )
            )
        elif event.type == "tool_requested":
            calls_by_step.setdefault(step_id or "", []).append(
                ToolCallRef(
                    call_id=payload["call_id"], name=payload["name"],
                    arguments=payload["args"], seq=event.seq,
                    visible=payload["visible"], known=payload["known"],
                )
            )
        elif event.type in ("tool_finished", "tool_failed"):
            body = payload.get("summary") if event.type == "tool_finished" else payload["error"]
            messages.append(
                RehydratedMessage(
                    seq=event.seq, role="tool", actor=f"tool:{payload['name']}",
                    chars=payload.get("result_chars", len(body or "")),
                    preview=body or "", source=event.type, step_id=step_id,
                    trust=payload.get("trust"), tool_call_id=payload["call_id"],
                )
            )
    # The model's message list carries an assistant turn and its tool calls as one message.
    # A step has exactly one assistant `message_appended`, emitted after the model's call and
    # before the `tool_requested` events it caused, so the step is the join and no guess is
    # involved.
    attached = tuple(
        (
            m
            if m.role != "assistant" or not calls_by_step.get(m.step_id or "")
            else _with_calls(m, tuple(calls_by_step[m.step_id or ""]))
        )
        for m in messages
    )
    last = events[-1]
    return Rehydration(
        run_id=run_id,
        from_seq=0,
        through_seq=last.seq,
        replayed_events=len(events),
        messages=attached,
        worker_events=worker_events,
        turn_id=turn_id,
        session_id=session_id,
        archive=(
            ArchiveRef(session_id=session_id, turn_id=turn_id)
            if session_id is not None and turn_id is not None
            else None
        ),
        steps=steps,
        status=status,
    )


def _with_calls(message: RehydratedMessage, calls: tuple[ToolCallRef, ...]) -> RehydratedMessage:
    return RehydratedMessage(
        seq=message.seq, role=message.role, actor=message.actor, chars=message.chars,
        preview=message.preview, source=message.source, step_id=message.step_id,
        trust=message.trust, tool_call_id=message.tool_call_id, calls=calls,
    )


def _reconcile(run_id: str, events: Sequence[Any], store: JournalStore) -> Reconciliation:
    """Fold the effect events, then ask the ledger the one thing the journal cannot say."""
    intents: dict[str, Any] = {}
    closed: dict[str, str] = {}
    for event in events:
        if event.type == "effect_intended":
            intents[event.payload["effect_id"]] = event
        elif event.type == "effect_committed":
            # Any terminal status closes it, `uncertain` included: an effect a previous
            # resume already gave up on is not orphaned a second time. The status is kept
            # rather than discarded, because "closed" and "closed how" are different
            # questions and 4c has to answer the second one.
            closed[event.payload["effect_id"]] = event.payload["status"]
    open_effects = {eid: e for eid, e in intents.items() if eid not in closed}
    ledger = EffectLedger.reading(store)
    announced = tuple(
        AnnouncedCall(
            effect_id=eid,
            tool=event.payload["tool_name"],
            effect_class=event.payload["effect_class"],
            step_id=event.payload["step_id"],
            call_id=_call_id_of(events, event),
            status=closed.get(eid),
        )
        for eid, event in intents.items()
    )
    orphans = []
    for event in open_effects.values():
        payload = event.payload
        row = ledger.get(payload["idempotency_key"])
        args_event = _requesting_call(events, payload["tool_name"], payload["step_id"], event.seq)
        orphans.append(
            Orphan(
                effect_id=payload["effect_id"],
                idempotency_key=payload["idempotency_key"],
                tool=payload["tool_name"],
                effect_class=payload["effect_class"],
                step_id=payload["step_id"],
                attempt=payload.get("attempt") or 1,
                intended_seq=event.seq,
                ledger_state=row.state if row is not None else None,
                arguments=args_event.payload["args"] if args_event is not None else None,
                arguments_seq=args_event.seq if args_event is not None else None,
                call_id=args_event.payload["call_id"] if args_event is not None else None,
            )
        )
    return Reconciliation(run_id=run_id, orphans=tuple(orphans), announced=announced)


def _call_id_of(events: Sequence[Any], intent: Any) -> str | None:
    """The model's id for the call an `effect_intended` was announced by, if the loop
    emitted one. Same match as `_requesting_call`, because it is the same question."""
    payload = intent.payload
    request = _requesting_call(events, payload["tool_name"], payload["step_id"], intent.seq)
    return request.payload["call_id"] if request is not None else None


def _requesting_call(events: Sequence[Any], tool: str, step_id: str, before_seq: int) -> Any | None:
    """The `tool_requested` that this `effect_intended` came from.

    The nearest one before it with the same tool *and* the same step. The loop runs a
    batch's calls one after another - request, start, intend - so "nearest before" is exact
    rather than a heuristic, and matching the step as well keeps two identical calls in
    different steps from borrowing each other's arguments. `call_id` would look like the
    obvious key and is not one: it comes from the model and is only unique within a step.
    """
    for event in reversed([e for e in events if e.seq < before_seq]):
        if (
            event.type == "tool_requested"
            and event.payload["name"] == tool
            and event.payload.get("step_id") == step_id
        ):
            return event
    return None


# --- writing -----------------------------------------------------------------


def resume(
    run_id: str,
    *,
    writer: JournalWriter,
    reason: str,
    force: bool = False,
) -> Resumed:
    """Reconcile and announce. The one call that writes.

    Emits `run_resumed` as the next event in the run, then closes every orphaned effect
    with `effect_committed(status="uncertain")` and moves its ledger row to `orphaned`.
    Nothing is re-executed and nothing is reversed.

    A complete run with nothing open is left alone (`applied=False`) unless `force`, because
    a resume that had nothing to do should not leave a record saying it did something.
    """
    # Same reason `Checkpointer.write` flushes: the tail of a killed process's buffer is
    # gone, but this process may be holding events of its own, and a plan built around them
    # while they sit in memory would fold a different run than the one on disk.
    writer.flush()
    current = plan(run_id, store=writer.store)
    if not current.needs_resume and not force:
        return Resumed(plan=current, applied=False)

    orphans = current.reconciliation.orphans
    rj = RunJournal(writer, run_id)
    event = rj.emit(
        "run_resumed",
        {
            "from_seq": current.from_seq,
            # Null is a statement: this run was rebuilt by folding the journal from the
            # start, because no checkpoint stands behind it. Every run recorded before
            # `[checkpoints] enabled` was switched on is in that state.
            "checkpoint_id": current.checkpoint.checkpoint_id if current.checkpoint else None,
            "reason": reason,
            "replayed_events": current.rehydration.replayed_events,
            "uncertain_effects": [o.effect_id for o in orphans],
        },
        sync=True,
    )
    if event is None:  # pragma: no cover - sync=True appends
        raise ResumeError("run_resumed was buffered; a resume nothing announced is not a resume")

    ledger = EffectLedger(writer)
    orphaned: list[str] = []
    rowless: list[str] = []
    for orphan in orphans:
        had_row = ledger.orphan(
            run_id=run_id,
            step_id=orphan.step_id,
            effect_id=orphan.effect_id,
            key=orphan.idempotency_key,
            attempt=orphan.attempt,
            note=_note(orphan),
        )
        (orphaned if had_row else rowless).append(orphan.idempotency_key)
    return Resumed(
        plan=current,
        applied=True,
        event_seq=event.seq,
        orphaned_keys=tuple(orphaned),
        rowless_effects=tuple(rowless),
    )


NOTES: dict[str, str] = {
    NEVER_DISPATCHED: "orphaned on resume: recorded, and the call was never started",
    MAY_HAVE_RUN: "orphaned on resume: the call was started and never reported back",
    UNKNOWN: "orphaned on resume: no ledger row, so whether the call was started is unknown",
}


def _note(orphan: Orphan) -> str:
    """Why this effect is being closed as uncertain, kept in the journal.

    The three cases are written down rather than collapsed into one sentence because they
    are the difference between "it may have sent the email" and "it never got as far as
    trying", and that difference is the whole value of the record to the person who has to
    decide what to do next.
    """
    return NOTES[orphan.evidence]


def summary(current: ResumePlan) -> str:
    """One line about a run, for a log or a `--dry-run`. Not a prompt: 4c owns the wording
    a user is asked to act on, and phrasing it here would be two sources for one sentence."""
    counts = (
        f"{len(current.reconciliation.uncertain)} uncertain, "
        f"{len(current.reconciliation.retryable)} retryable"
    )
    return (
        f"run {current.run_id}: {current.state}, through seq {current.from_seq}, "
        f"{current.rehydration.replayed_events} events, "
        f"{len(current.rehydration.messages)} messages, {counts}"
    )


__all__ = [
    "COMPLETE",
    "DISPOSITIONS",
    "EVIDENCE",
    "INTERRUPTED",
    "MAY_HAVE_RUN",
    "NEVER_DISPATCHED",
    "NO_ORCHESTRATOR",
    "NOTES",
    "RETRY",
    "UNCERTAIN",
    "UNKNOWN",
    "AnnouncedCall",
    "ArchiveRef",
    "NoSuchRun",
    "Orphan",
    "Reconciliation",
    "RehydratedMessage",
    "Rehydration",
    "ResumeError",
    "ResumePlan",
    "Resumed",
    "ToolCallRef",
    "UncertainGroup",
    "disposition",
    "plan",
    "resume",
    "summary",
]
