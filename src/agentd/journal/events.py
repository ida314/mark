"""The event vocabulary: every type the journal may carry, and the shape of its payload.

These twenty-one types are the only vocabulary there is. Session 2c deleted the in-process
`agent/events.py` stream that used to carry the same facts under different names, so a
frontend that wants to know what a tool is doing reads them from here through
`journal/feed.py`. `agent/stream.py` is what is left of that module and carries prose only.
`state = fold(reduce, journal, initial)` folds over exactly these types and nothing else.

Seventeen of them are pass 2's list. The `worker_result_*` pair is session 6c's and the
`working_memory_*` pair is session 7b's, and both are here for one reason: run-scoped state
that is discarded with its run has to be foldable out of the journal, because the journal is
the only durable record every run has - `[checkpoints] enabled` is off in the shipped config,
so state that lived only in a checkpoint would be state that never existed on this machine.

**Twenty of the twenty-one are emitted today** - nine wired by session 2b, the two
`effect_*` types by session 3b's effect ledger, `checkpoint_written` by session 4a's
checkpointer, `run_resumed` by session 4b's `journal/resume.py`, `run_forked` by session
4d's `journal/fork.py`, the `handoff_*` pair by session 5b's `agent/handoff.py`, the
`worker_result_*` pair by session 6c's `agent/result_cache.py` and the `working_memory_*`
pair by session 7b's `agent/working_memory.py`. The one
left is `tool_progress`, which needs a progress channel the tool surface does not have. Each
was specified before it had a producer so that the pass which needed it filled a slot instead
of migrating a schema, and each has a test that writes one, so none of them is a shape nobody
ever tried to construct.

Three rules, and the first two are this codebase's characteristic bug stated backwards:

1. **A field is either present with a value of its declared type, or it is declared
   nullable.** There is no "absent means null" and no default. A `None` in a field that was
   not declared nullable raises, because the failure this runtime keeps having is a real
   value degrading quietly into a plausible NULL that still folds and folds to the wrong
   answer.
2. **An unknown key raises**, rather than riding along unvalidated. A misspelled key is
   otherwise a field that is silently never read.
3. **An unknown event type raises.** A type that is not in `EVENT_TYPES` is a typo or a
   vocabulary change, and either way `reduce` has no case for it. `JournalWriter.append`
   enforces this one for every caller; payload shape is enforced by `RunJournal.emit`,
   which is the door the runtime uses.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .store import JournalError

PREVIEW_CHARS = 200


class EventSchemaError(JournalError):
    """A payload did not match the declared shape for its event type."""


class UnknownEventType(JournalError):
    """An event type that is not in the vocabulary. Nothing folds it, so nothing writes it."""


@dataclass(frozen=True)
class Field:
    """One payload key.

    `nullable` is deliberately separate from `required`: "this key must be present and may
    legitimately be null" (a resume with no checkpoint behind it) is a different statement
    from "this key may be omitted", and collapsing them is how an unset field becomes
    indistinguishable from a measured absence.
    """

    types: tuple[type, ...]
    required: bool = True
    nullable: bool = False
    enum: tuple[str, ...] | None = None


def req(*types: type, enum: tuple[str, ...] | None = None, nullable: bool = False) -> Field:
    return Field(types=types, required=True, nullable=nullable, enum=enum)


def opt(*types: type, enum: tuple[str, ...] | None = None, nullable: bool = False) -> Field:
    return Field(types=types, required=False, nullable=nullable, enum=enum)


# Every event may carry these two. They are named arguments on `JournalWriter.append` rather
# than free payload keys, so promoting either to a column later is a migration plus one
# module. An event type that *requires* one redeclares it below.
COMMON: dict[str, Field] = {
    "worker_id": opt(str),
    "step_id": opt(str),
}

# Fixed by docs/architecture/tool-call-architecture.md and CLAUDE.md, not invented here.
# These three are the only enums imposed on event types nothing writes yet: an enum this
# session made up for Pass 5 would be a constraint Pass 5 has to migrate away from, which is
# the opposite of the point.
EFFECT_CLASSES = ("read", "idempotent_write", "unsafe_write")
CHECKPOINT_TRIGGERS = ("turn_end", "worker_finished", "pre_effect", "handoff", "manual")
# "uncertain" is not a hedge: an unsafe_write that crashed between intent and commit is never
# retried automatically, and this is the status it surfaces as.
EFFECT_STATUSES = ("committed", "failed", "uncertain")

# A turn's terminal status. The first three are telemetry's vocabulary verbatim
# (`obs/telemetry.py`) so the two records can be joined without a mapping table;
# "cancelled" is the one addition, for a consumer that stopped iterating the turn.
RUN_STATUSES = ("completed", "abandoned", "failed", "cancelled")

# A worker's terminal status (session 6b). `agent/results.py` imports this tuple rather than
# repeating it, the way `agent/budget.py` does with CONTEXT_BASES, so the enum the journal
# enforces and the words the runtime can produce cannot drift apart. The three are Pass 4c's
# effect vocabulary applied to a piece of work, and the reading is the same: `blocked` means
# it did not happen, `uncertain` means nobody can say what happened.
WORKER_STATUSES = ("completed", "blocked", "uncertain")

# How a context reading was arrived at (session 5a). "estimate" is `ids.estimate_tokens`
# over the assembled prompt, which is what every reading says today; "provider" is reserved
# for the day the router in front of this model stops dropping the usage chunk. "unmeasured"
# is not a hedge either: a turn that never assembled a prompt has no size, and recording that
# as a 0 would read as a turn that used no context. `agent/budget.py` imports this tuple
# rather than repeating it, so the enum and the values written cannot drift apart.
CONTEXT_BASES = ("estimate", "provider", "unmeasured")

EVENTS: dict[str, dict[str, Field]] = {
    # --- the turn itself -----------------------------------------------------
    "agent_started": {
        "session_id": req(str),
        "turn_id": req(str),
        "role": req(str),
        "actor": req(str),
        "origin": req(str),
        "channel": req(str),
        "autonomy": req(str),
        "model": req(str),
        "max_steps": req(int),
        "input_chars": req(int),
        "input_preview": req(str),
        # Set on a worker's turn, naming the turn that delegated to it. There is no
        # parent *run*: a worker's events are in its caller's run, tagged with `worker_id`,
        # because a worker is part of the work and not a separate history.
        "parent_turn_id": req(str, nullable=True),
    },
    "agent_finished": {
        "turn_id": req(str),
        "status": req(str, enum=RUN_STATUSES),
        "steps": req(int),
        "duration_ms": req(int),
        "answer_chars": req(int),
        "usage": req(dict),
        # False means "not measured", not "zero" - the router in front of this model drops
        # the usage chunk on streamed calls (Pass 1, open question 1). Without this the
        # journal would record an unmeasured turn as a free one.
        "usage_reported": req(bool),
        # Session 5a. How full the orchestrator's context got, and against what. Required
        # rather than optional because a turn that did not say is indistinguishable from
        # one nobody sized, and that is the failure this runtime keeps having.
        #
        # `context_tokens` is the largest prompt this turn assembled, **estimated** - it is
        # never a provider token count, and `context_basis` is the field that says so, in
        # the same way `usage_reported` does for `usage`. The ceiling is the budget
        # `agent/budget.py` measures against (agent.history_tokens by default, not the
        # model window); `context_crossed` is whether any step of this turn came within
        # `context_threshold_tokens` of it.
        "context_tokens": req(int),
        "context_ceiling_tokens": req(int),
        "context_threshold_tokens": req(int),
        "context_crossed": req(bool),
        "context_basis": req(str, enum=CONTEXT_BASES),
        "answer_preview": opt(str),
        "error": opt(str, nullable=True),
    },
    # --- tools ---------------------------------------------------------------
    # Four events, not one with a status field, because the fold cares about different
    # things at each edge: what was asked for, when execution began, and which of the two
    # terminal shapes it took.
    "tool_requested": {
        "call_id": req(str),
        "name": req(str),
        "args": req(dict),
        # Whether this turn had been shown the tool, and whether it exists at all. Both are
        # read before the loop re-adds a known-but-unoffered tool, so a tool the model
        # reached for blind stays visible as such in the journal.
        "visible": req(bool),
        "known": req(bool),
    },
    "tool_started": {
        "call_id": req(str),
        "name": req(str),
    },
    # Nothing emits this yet: a tool handler has no channel to report progress on, and
    # adding one is a change to the tool surface, which Pass 2 must not make.
    "tool_progress": {
        "call_id": req(str),
        "name": req(str),
        "message": req(str),
        "pct": opt(int, float, nullable=True),
    },
    "tool_finished": {
        "call_id": req(str),
        "name": req(str),
        "duration_ms": req(int),
        "result_chars": req(int),
        "trust": req(str, enum=("trusted", "untrusted")),
        "summary": opt(str),
        "private": opt(bool),
        "has_undo": opt(bool),
    },
    "tool_failed": {
        "call_id": req(str),
        "name": req(str),
        "duration_ms": req(int),
        "error": req(str),
        # A denial is a failed call but not a failed tool, and a policy refusal says nothing
        # about whether the right tool was chosen. Kept as flags on the failure rather than
        # as separate event types, because the fold wants one terminal event per call.
        "denied": req(bool),
        "invalid_args": req(bool),
        "attempt": opt(int),
        "rule": opt(str, nullable=True),
        "queued_id": opt(str, nullable=True),
    },
    # --- workers -------------------------------------------------------------
    "worker_created": {
        "worker_id": req(str),
        "name": req(str),
        "role": req(str),
        "autonomy": req(str),
        "max_steps": req(int),
        "tools": req(list),
        "task_chars": req(int),
        "task_preview": req(str),
        # Session 6a. sha256 of the canonical task spec: the identity of what was
        # delegated, next to a preview that is 200 characters and must never be
        # re-delegated from. Required, and absent from the rows written before 6a -
        # `validate_payload` runs on the write path only, so those rows still fold.
        "task_digest": req(str),
        "parent_step_id": opt(str, nullable=True),
    },
    "worker_finished": {
        "worker_id": req(str),
        "name": req(str),
        # Session 6b replaced the old ("ok", "partial", "failed", "budget_exhausted") with
        # `agent/results.py`'s three, which are Pass 4c's words for an effect read as words
        # for a piece of work: `blocked` is "it did not happen", `uncertain` is "something
        # happened and nobody can say what it amounts to". Rows written before 6b carry the
        # old four; `validate_payload` is on the write path only, so they still fold.
        "status": req(str, enum=WORKER_STATUSES),
        "answer_chars": req(int),
        # Session 6b. Whether the worker returned the result schema at all, which is not
        # readable from `status`: an unreadable report is `uncertain`, and so is a worker
        # that reported honestly that it could not vouch for its own work.
        "report_valid": req(bool),
        "tainted": req(bool),
        # Counts of the result's three lists. `artifacts` and `citations` were the previous
        # schema's and are not fields of a result any more.
        "evidence": req(int),
        "actions_taken": req(int),
        "followups": req(int),
        "candidates": req(int),
        "tokens": req(int),
        "duration_ms": req(int),
        "answer_preview": opt(str),
    },
    # Session 6c. One completed worker's result, in full, under the key it is cached at.
    # The only event type that carries a body rather than a preview of one, and the reason
    # is that a preview cannot be served back as a result: this is the durable artifact a
    # resume reuses instead of re-running the worker that earned it. Bounded by the final
    # instruction's "at most 200 words".
    #
    # Written for `completed` results only, which is why `status` is an enum of one. An
    # `uncertain` result is precisely the case where re-running may be the right answer, and
    # serving one from a cache would decide that question silently and for ever.
    "worker_result_cached": {
        "result_key": req(str),
        # sha256 of the canonical task spec - the same bytes as `worker_created.task_digest`,
        # so the worker that earned a cached result is one join away.
        "name": req(str),
        "status": req(str, enum=("completed",)),
        # The shape of the fields below. A stored entry written under other rules is skipped
        # by the fold rather than read as if the rules had not moved (`worker_results.py`).
        "entry_version": req(int),
        "answer": req(str),
        "evidence": req(list),
        "actions_taken": req(list),
        "followups": req(list),
        "notes": req(list),
        # Carried because reuse must not launder it: a result earned while the turn held the
        # user's private data is still untrusted the second time it is served.
        "tainted": req(bool),
    },
    # Session 6c. A delegation that was answered out of this run's cache instead of by a
    # worker. There is no `worker_created`/`worker_finished` pair for it - nothing ran - so
    # this is the only record that the delegation was made at all, and the only thing a
    # `worker_results_reused` rate can be counted from.
    "worker_result_reused": {
        "result_key": req(str),
        "name": req(str),
        # The worker whose run produced what is being served. Always in this same run.
        "source_worker_id": req(str),
        "answer_chars": req(int),
        "parent_step_id": opt(str, nullable=True),
    },
    # Session 7b. One note in the working bucket - task-local, agent-local scratch state.
    # Like `worker_result_cached` and unlike everything else here it carries a body rather
    # than a preview of one, for the same reason: a preview cannot be handed back as the
    # thing itself, and the journal is where this bucket lives. `journal/working_memory.py`
    # folds the pair below into what a scope currently holds.
    "working_memory_noted": {
        # The isolation key, alongside `run_id`. Always a non-empty string: a worker's id
        # for a worker, the literal "orchestrator" otherwise. Not the *absence* of a worker
        # id, because an absent value used as a bucket is where unrelated writers collide,
        # and emphatically not the session id, which a worker shares with its caller.
        "scope": req(str),
        "key": req(str),
        "text": req(str),
        "chars": req(int),
        "entry_version": req(int),
        # The two provenance flags of the turn that wrote it, carried so that reading a note
        # back cannot launder it and so that a later promotion pass can tell a note the model
        # copied off a web page from one the user said. `tainted` is "untrusted text was in
        # context", `private` is "the user's own private data was" - two different doors
        # (`agent/loop.py`), and collapsing them here would lose the one that shuts egress.
        "tainted": req(bool),
        "private": req(bool),
    },
    # Session 7b. A scope emptied: a worker's task scope ending, a run completing, or a
    # caller throwing its own scratch away. A tombstone the fold honours, never a delete -
    # the notes stay in the journal, so what a run held at an earlier position is still
    # readable, and the live view of that scope is empty from here on.
    "working_memory_discarded": {
        "scope": req(str),
        "reason": req(str, enum=("worker_finished", "run_completed", "manual")),
        # How many notes were discarded. Always positive: a scope that held nothing gets no
        # tombstone at all (`agent/working_memory.py` says why), so this event is evidence
        # that something existed and was thrown away, and never a per-turn "nothing here".
        "notes": req(int),
    },
    # --- conversation --------------------------------------------------------
    # One per message entering the model's message list that no other event already
    # describes: the user's message, the assembled system block, each step's assistant
    # output, and the mid-turn system nudges. A tool result is deliberately *not* one of
    # these - `tool_finished` and `tool_failed` already carry its size, trust and summary,
    # and emitting both would put the same body in the journal twice under two names.
    "message_appended": {
        "role": req(str, enum=("user", "assistant", "tool", "system")),
        "actor": req(str),
        "chars": req(int),
        "preview": req(str),
        "trust": opt(str, enum=("trusted", "untrusted")),
        "private": opt(bool),
        "tool_call_id": opt(str, nullable=True),
        "tool_calls": opt(int),
    },
    # --- written by Pass 4 ---------------------------------------------------
    "checkpoint_written": {
        "checkpoint_id": req(str),
        "trigger": req(str, enum=CHECKPOINT_TRIGGERS),
        # The last journal seq the snapshot accounts for, which is always lower than this
        # event's own seq. Named for what it means rather than "seq", so nobody reads it as
        # the position of the checkpoint event itself: a checkpoint may lag the journal, and
        # this is the number that says by how much.
        "covers_seq": req(int),
        # Committed episodic/semantic write positions at checkpoint time. An object rather
        # than two ints because Pass 4 owns what a watermark is made of.
        #
        # Required *and* nullable, which session 4a changed from required-and-not-nullable:
        # the watermark is Pass 7's to fill, and until then every checkpoint has to be able
        # to say "no watermark was recorded" out loud. An empty object would have read as a
        # measurement - a run whose memory positions were both zero - which is this
        # codebase's characteristic bug with the lights on. Same reasoning as
        # `run_resumed.checkpoint_id`.
        "memory_watermark": req(dict, nullable=True),
        "messages": opt(int),
        "bytes": opt(int),
        "duration_ms": opt(int),
        "location": opt(str, nullable=True),
    },
    # --- written by Pass 3 ---------------------------------------------------
    # `effect_intended` is a promise that the record precedes the action, which is why it is
    # synchronous (writer.SYNC_PREFIXES). `step_id` is required on both, because the
    # idempotency key is hash(run_id, step_id, tool_name, canonical_args) and a key computed
    # from a missing step is a key that collides across steps.
    "effect_intended": {
        "effect_id": req(str),
        "step_id": req(str),
        "tool_name": req(str),
        "effect_class": req(str, enum=EFFECT_CLASSES),
        "idempotency_key": req(str),
        "args_digest": req(str),
        "attempt": opt(int),
    },
    "effect_committed": {
        "effect_id": req(str),
        "step_id": req(str),
        "idempotency_key": req(str),
        "status": req(str, enum=EFFECT_STATUSES),
        # Required *and* nullable, which session 4b changed from required-and-not-nullable.
        # An effect closed as `uncertain` on resume was interrupted, so nobody ever timed
        # it; writing 0 would record it as a call that returned instantly, which is a
        # measurement that never happened. Same reasoning as `checkpoint_written`'s
        # `memory_watermark`, and the only shape a resume may write it in.
        "duration_ms": req(int, nullable=True),
        "result_digest": opt(str, nullable=True),
        "error": opt(str, nullable=True),
        "attempt": opt(int),
    },
    # --- written by Pass 4 (resume) and Pass 5 (handoff) ---------------------
    # Emitted *into the run being resumed*, as the next event after the last one that
    # survived. `checkpoint_id` is required-and-nullable on purpose: resuming by folding the
    # whole journal from seq 0 is a legitimate resume, and it has to be stated rather than
    # inferred from a missing key.
    "run_resumed": {
        "from_seq": req(int),
        "checkpoint_id": req(str, nullable=True),
        "reason": req(str),
        "replayed_events": req(int),
        # Effect ids that were intended and never committed. An unsafe_write here is never
        # retried automatically; it is reported.
        "uncertain_effects": req(list),
    },
    # Emitted as the *first* event of the new run, naming the run it came from. The parent
    # run gets no event: a fork is not something that happens to the run being forked from,
    # and writing one there would mean a completed run's journal keeps growing.
    #
    # Session 4d renamed 2b's `fork_point_seq` to `forked_from_seq`, which is what the pass
    # file and the architecture call the same number everywhere else. Nothing had ever
    # emitted this type, so it is a one-line vocabulary edit and not a migration; the point
    # is that the run-level field and the payload key are one name rather than two names for
    # one integer, which is the trap session 4a wrote down under `covers_seq`.
    "run_forked": {
        "parent_run_id": req(str),
        "forked_from_seq": req(int),
        "reason": req(str),
        # Both are written as an explicit null by 4d rather than omitted: "no checkpoint
        # stood behind this fork" and "this fork did not come out of a handoff" are
        # statements, and an absent key would be indistinguishable from a caller that forgot.
        "checkpoint_id": opt(str, nullable=True),
        "handoff_id": opt(str, nullable=True),
    },
    # --- written by Pass 5 ---------------------------------------------------
    # A handoff is a lossy semantic compression for surviving a context limit, not a
    # checkpoint. `kept`/`dropped` are the size of the loss, and they are required because a
    # handoff that reports no loss is the one worth looking at.
    "handoff_started": {
        "handoff_id": req(str),
        "reason": req(str),
        "messages": req(int),
        "context_tokens": opt(int),
        "ceiling_tokens": opt(int),
    },
    "handoff_finished": {
        "handoff_id": req(str),
        "status": req(str, enum=("ok", "failed")),
        "summary_chars": req(int),
        "kept_messages": req(int),
        "dropped_messages": req(int),
        "duration_ms": req(int),
        "error": opt(str, nullable=True),
        "successor_run_id": opt(str, nullable=True),
    },
}

EVENT_TYPES: frozenset[str] = frozenset(EVENTS)

# The fourteen the runtime writes today. Kept as data so a test can assert the gap between
# the vocabulary and what is actually reachable, instead of that gap living in prose.
EMITTED_TYPES: frozenset[str] = frozenset(
    {
        "agent_started",
        "agent_finished",
        "tool_requested",
        "tool_started",
        "tool_finished",
        "tool_failed",
        "worker_created",
        "worker_finished",
        "message_appended",
        # Session 3b. Both are written by `journal/ledger.py`, from inside the executor, so
        # they are reachable from every caller and not only from a turn.
        "effect_intended",
        "effect_committed",
        # Session 4a. Written by `journal/checkpoints.py` at the five boundaries, and only
        # when `[checkpoints] enabled` is on - so this is the first member of this set that
        # a configuration can switch off. The type is reachable; whether a given run reaches
        # it is a setting.
        "checkpoint_written",
        # Session 4b. Written by `journal/resume.py`, into the run being resumed. Reachable
        # only when a person or a supervisor asks for a resume: nothing in the runtime
        # resumes a run on its own, so a run that was never asked about never sees one.
        "run_resumed",
        # Session 4d. Written by `journal/fork.py` as the first event of the *new* run, and
        # only when a person asks for a rewind. The run it names is not touched.
        "run_forked",
        # Session 5b. Written by `agent/handoff.py` through the turn loop, as a pair, around
        # one generation. Both are written even when generation fails - a handoff that was
        # attempted and did not work is a different state from one nobody tried, and with
        # `handoff_object` NULL in both cases these two events are the only thing that says
        # which happened.
        "handoff_started",
        "handoff_finished",
        # Session 6c. Written by `agent/result_cache.py` from inside `run_subagent`:
        # `worker_result_cached` after every completed worker, `worker_result_reused` in
        # place of the worker a cache hit means nobody has to run. Both are reachable
        # without any configuration - unlike `checkpoint_written`, the cache is not behind
        # a flag, because a result that was only kept when checkpoints were on would be a
        # result this machine never kept.
        "worker_result_cached",
        "worker_result_reused",
        # Session 7b. Written by `agent/working_memory.py`: `working_memory_noted` from the
        # `working_memory_note` tool, `working_memory_discarded` from the two places a task
        # scope ends - `agent/subagents.py` when a worker finishes and `agent/loop.py` when
        # the run's turn unwinds. Behind no flag, for the cache's reason: a bucket that only
        # existed when checkpoints were on would be a bucket this machine has never had.
        "working_memory_noted",
        "working_memory_discarded",
    }
)


def spec_for(event_type: str) -> dict[str, Field]:
    """The full field spec for a type, common fields included."""
    fields = EVENTS.get(event_type)
    if fields is None:
        raise UnknownEventType(f"unknown event type: {event_type!r}")
    return {**COMMON, **fields}


def check_type(event_type: str) -> None:
    if event_type not in EVENTS:
        raise UnknownEventType(f"unknown event type: {event_type!r}")


def _type_name(value: object) -> str:
    return type(value).__name__


def _matches(value: Any, types: tuple[type, ...]) -> bool:
    # bool is an int in Python, and a `True` sitting in `steps` would pass a naive isinstance
    # check and then fold as 1.
    if isinstance(value, bool):
        return bool in types
    return isinstance(value, types)


def validate_payload(event_type: str, payload: dict[str, Any]) -> None:
    """Raise unless `payload` is exactly what `event_type` declares.

    Exactly: no missing required key, no unknown key, no `None` outside a nullable field,
    no value of the wrong type, no string outside its enum.
    """
    spec = spec_for(event_type)
    problems: list[str] = []
    for key in sorted(set(payload) - set(spec)):
        problems.append(f"{key!r} is not a field of {event_type}")
    for name, field in spec.items():
        if name not in payload:
            if field.required:
                problems.append(f"{event_type} requires {name!r}")
            continue
        value = payload[name]
        if value is None:
            if not field.nullable:
                problems.append(
                    f"{event_type}.{name} is None, and {name!r} is not declared nullable"
                )
            continue
        if not _matches(value, field.types):
            wanted = " | ".join(t.__name__ for t in field.types)
            problems.append(
                f"{event_type}.{name} is {_type_name(value)}, expected {wanted}"
            )
            continue
        if field.enum is not None and value not in field.enum:
            problems.append(
                f"{event_type}.{name} is {value!r}, expected one of {field.enum}"
            )
    if problems:
        raise EventSchemaError("; ".join(problems))


def preview(text: str | None, limit: int = PREVIEW_CHARS) -> str:
    """One line of a body, for reading the journal as a story.

    The journal is a record of *what happened*, not a second copy of the conversation - the
    archive in Postgres holds the content. Collapsing whitespace matters for the same reason
    it does in `tools/base.flat`: 2c renders these into a feed one event per line, and a
    value carrying a newline would forge a line.
    """
    if not text:
        return ""
    one_line = " ".join(text.split())
    return one_line[:limit] + ("…" if len(one_line) > limit else "")


def step_id(step: int, *, worker_id: str | None = None) -> str:
    """The identifier for one step of one agent inside a run.

    Scoped by worker, because a worker's steps share the caller's run and a bare step number
    would collide with the caller's. Pass 3 hashes this into an idempotency key, so two
    different steps must never produce the same string.
    """
    return f"{worker_id}.s{step}" if worker_id else f"s{step}"


__all__ = [
    "CHECKPOINT_TRIGGERS",
    "COMMON",
    "EFFECT_CLASSES",
    "EFFECT_STATUSES",
    "EMITTED_TYPES",
    "EVENTS",
    "EVENT_TYPES",
    "PREVIEW_CHARS",
    "RUN_STATUSES",
    "EventSchemaError",
    "Field",
    "UnknownEventType",
    "check_type",
    "opt",
    "preview",
    "req",
    "spec_for",
    "step_id",
    "validate_payload",
]
