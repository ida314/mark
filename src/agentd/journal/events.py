"""The event vocabulary: every type the journal may carry, and the shape of its payload.

Not to be confused with `agent/events.py`, which is the in-process UI stream the CLI and
Telegram render. That one is ephemeral and is what session 2c replaces with a journal-backed
feed. This one is the durable record: `state = fold(reduce, journal, initial)` folds over
exactly these seventeen types and nothing else.

**Nine of the seventeen are emitted today** (session 2b wired them through the runtime).
The other eight are defined here and written by nobody yet - `tool_progress` needs a
progress channel the tool surface does not have, `handoff_*` is Pass 5, `checkpoint_written`
is Pass 4, `effect_*` is Pass 3, `run_resumed` is Pass 4 and `run_forked` is Pass 5. They are
specified now so those passes fill a slot instead of migrating a schema, and each has a test
that writes one, so none of them is a shape nobody ever tried to construct.

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
        "parent_step_id": opt(str, nullable=True),
    },
    "worker_finished": {
        "worker_id": req(str),
        "name": req(str),
        "status": req(str, enum=("ok", "partial", "failed", "budget_exhausted")),
        "summary_chars": req(int),
        "tainted": req(bool),
        "artifacts": req(int),
        "citations": req(int),
        "candidates": req(int),
        "tokens": req(int),
        "duration_ms": req(int),
        "summary_preview": opt(str),
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
        "memory_watermark": req(dict),
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
        "duration_ms": req(int),
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
    "run_forked": {
        "parent_run_id": req(str),
        "fork_point_seq": req(int),
        "reason": req(str),
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

# The nine the runtime writes today. Kept as data so a test can assert the gap between the
# vocabulary and what is actually reachable, instead of that gap living in prose.
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
