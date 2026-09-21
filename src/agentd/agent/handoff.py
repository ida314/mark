"""The handoff object: what survives when a conversation runs out of room to carry itself.

Session 5a taught the runtime to notice. This is what happens after the crossing.

A checkpoint is a lossless mechanical snapshot for surviving a process failure. A handoff is
a **lossy semantic compression for surviving a context limit**, and the two are deliberately
different things. Everything below follows from that one sentence: a handoff is allowed to
lose the conversation, and is therefore not allowed to lose the state, so the object is
structured fields rather than a summary paragraph, and an object that is missing the fields
the successor has to act on is a *failed* handoff rather than a partial one.

## What is read, and what is refused

The generator reads a live message list - the one the turn was about to send - so it sees
full message bodies rather than the journal's 200-character previews. That is the whole
reason generation happens inside the turn: session 4b refused to hand previews to a model as
message bodies, and there is still no `model_messages()`. A handoff generated from the
journal alone would have to invent the bodies, which is why the pass file gives that case a
model call of its own and gives it to 5c.

Three kinds of text in that list are refused, all for one reason - **text the runtime wrote
about a call, folded into a summary, becomes a claim nobody made and no tool returned**:

1. `observations.ClosingMessage` and anything else carrying `synthetic=True`. Dylan's
   requirement at the Pass 4/5 boundary, and it is enforced by the flag rather than by the
   `RUNTIME_PREFIX` string, which is a second and independent signal.
2. Every `system` message. `FINAL_NUDGE`, `STUCK_NUDGE` and the assembled instruction block
   are all the runtime talking to the model, none of them is flagged, and a summary that
   read them would report "the user said to stop calling tools".
3. Anything still carrying `RUNTIME_PREFIX` after the first two passes. That cannot happen
   today; if it ever does it is a caller that lost the flag, so it is excluded *and counted*
   into the stored object rather than dropped quietly.

## What the model is not asked for

`important_memory_refs` and `active_subagents` are supplied by the runtime, not extracted.
The runtime knows both exactly - the retrieval pack returns its refs, and open workers are a
fold over the journal - and a model-invented `[F:01a0]` in a field the successor will look
things up from is the same laundering channel in a smaller font.

## Failure is a state, not a fallback

`complete_json` gets one repair attempt inside the provider and then raises. Nothing here
puts a cross-field rule into the pydantic schema for that reason: a model that returns nine
plausible fields and an empty `next_actions` must be *told what is wrong and asked again*,
which is a decision this module makes after decoding. If it still fails, the handoff fails:
`handoff_finished(status="failed")` is journaled, `handoff_object` stays NULL, and the turn
is untouched. The pass file's second *Must not* forbids the obvious alternative - copying the
old context into the successor when generation looks weak - so there is no fallback path
here at all, and there is deliberately nowhere for one to be added.
"""

from __future__ import annotations

import json
import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, Field

from ..config import Config, get_config
from ..ids import estimate_tokens_for_chars, utcnow, uuid7
from ..llm.base import LLMError, LLMProvider
from ..llm.roles import get_provider, params_for
from . import budget
from . import context as ctxmod
from .observations import RUNTIME_PREFIX

# The stored object's shape, so anything that reads one later can tell which rules it was
# built under. Session 5a's open question 2 is the reason this exists at all: five
# `agent_finished` rows stopped validating when that session added required fields, and the
# lesson taken from it was that a record with no version is a record nobody can migrate.
SCHEMA_VERSION = 1

# Why a handoff was generated. `context_threshold` is the runtime's own decision;
# `forced` is a caller that asked for one regardless, which is the only way to get one
# today on a conversation that has not crossed.
REASON_THRESHOLD = "context_threshold"
REASON_FORCED = "forced"
REASONS: tuple[str, ...] = (REASON_THRESHOLD, REASON_FORCED)

# The eleven fields the pass file names, in its order.
FIELDS: tuple[str, ...] = (
    "task",
    "user_intent",
    "current_state",
    "decisions_made",
    "constraints",
    "completed_actions",
    "active_subagents",
    "relevant_evidence",
    "unresolved_questions",
    "next_actions",
    "important_memory_refs",
)

# Prose fields the successor cannot start without. A blank `current_state` is an orchestrator
# that knows the task and not where the work got to, which is the failure a handoff exists
# to prevent.
REQUIRED_TEXT: tuple[str, ...] = ("task", "user_intent", "current_state")

# The pass file, verbatim: "a handoff missing `next_actions` or `unresolved_questions` is a
# failed handoff, not a partial one." An empty list is missing. It is *not* accepted as "there
# were none": this codebase's recurring failure is a real value degrading into a plausible
# empty one, and "no next action" from a generator that was asked for next actions is
# indistinguishable from a generator that skipped the field. A run with genuinely nothing
# open says so in a sentence, which is a claim the successor can read and act on.
REQUIRED_LISTS: tuple[str, ...] = ("next_actions", "unresolved_questions")

LIST_FIELDS: tuple[str, ...] = (
    "decisions_made",
    "constraints",
    "completed_actions",
    "active_subagents",
    "relevant_evidence",
    "unresolved_questions",
    "next_actions",
    "important_memory_refs",
)

# Supplied by the runtime rather than asked of the model. See the module docstring.
DERIVED_FIELDS: tuple[str, ...] = ("active_subagents", "important_memory_refs")


class HandoffError(RuntimeError):
    """A handoff could not be produced. Never swallowed into an empty object."""


class HandoffInvalid(HandoffError):
    """The generator returned something that is not a usable handoff."""

    def __init__(self, problems: Sequence[str]) -> None:
        self.problems = tuple(problems)
        super().__init__("; ".join(self.problems))


# --- what the model is asked for ---------------------------------------------


class HandoffDraft(BaseModel):
    """The nine fields a model is asked to extract.

    Every field has a default, which is not laxness. `complete_json` sends this schema to a
    guided decoder and repairs once against pydantic's own error; a required field or a
    cross-field validator here would spend that repair on a rule this module can state far
    better afterwards, and then raise `LLMError` into the middle of somebody's turn. So the
    schema decodes, and `validate` decides.
    """

    task: str = ""
    user_intent: str = ""
    current_state: str = ""
    decisions_made: list[str] = Field(default_factory=list)
    constraints: list[str] = Field(default_factory=list)
    completed_actions: list[str] = Field(default_factory=list)
    relevant_evidence: list[str] = Field(default_factory=list)
    unresolved_questions: list[str] = Field(default_factory=list)
    next_actions: list[str] = Field(default_factory=list)


INSTRUCTION = """You are writing a handoff for a fresh orchestrator that will continue this
work with none of the conversation below in its context. It gets this object and nothing
else, so anything you leave out is lost.

Rules:
- Write structured state, not a transcript and not a narrative. Short, flat sentences.
- Only what the transcript supports. Do not infer a goal the user did not state, and do not
  describe a tool call that is not there.
- next_actions: what the successor should do next, in order, specific enough to act on. If
  the work is finished, say that in one entry and say what "finished" means here.
- unresolved_questions: what is still open, including anything you could not determine from
  the transcript. If nothing is open, say so in one entry and say why.
- completed_actions: only actions the transcript shows happening, including whether each
  one succeeded.
- relevant_evidence: the specific facts, file paths, ids, URLs or results the successor will
  need, quoted closely enough to be usable.
- constraints: limits that still bind - what the user asked you not to do, what failed and
  must not be retried blindly, what is waiting on them.

Return JSON only."""

PREVIOUS_HEADING = """This work already handed off once. Below is the handoff object in
force. Carry forward everything in it that is still true - the successor will not see it
separately - and update whatever the transcript since then has changed."""

TRANSCRIPT_HEADING = "Transcript to compress, oldest first:"


# --- the object --------------------------------------------------------------


@dataclass(frozen=True)
class Handoff:
    """One handoff: the eleven fields, and where they came from.

    The provenance half is not decoration. Every number in it is an estimate (5a), the
    watermark is what makes the successor's history window start in the right place, and
    `source` says how many messages were read and how many were refused - so "this handoff
    was written without seeing the tool results" is answerable from the object itself.
    """

    task: str
    user_intent: str
    current_state: str
    decisions_made: tuple[str, ...]
    constraints: tuple[str, ...]
    completed_actions: tuple[str, ...]
    active_subagents: tuple[str, ...]
    relevant_evidence: tuple[str, ...]
    unresolved_questions: tuple[str, ...]
    next_actions: tuple[str, ...]
    important_memory_refs: tuple[str, ...]

    handoff_id: str
    run_id: str
    session_id: str | None
    reason: str
    created_at: str
    # The archive id everything at or below which this handoff replaces. None means the
    # conversation was shorter than the carry window, so nothing is replaced - a fact about
    # this conversation, not a missing value.
    watermark: int | None
    source: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        """Exactly what goes into `checkpoint.handoff_object`."""
        body: dict[str, Any] = {"handoff_schema": SCHEMA_VERSION}
        for name in FIELDS:
            value = getattr(self, name)
            body[name] = list(value) if isinstance(value, tuple) else value
        body.update(
            {
                "handoff_id": self.handoff_id,
                "run_id": self.run_id,
                "session_id": self.session_id,
                "reason": self.reason,
                "created_at": self.created_at,
                "watermark": self.watermark,
                "source": dict(self.source),
            }
        )
        return body

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Handoff:
        """Read one back. Raises on a version this module was not written for, rather than
        reading unknown fields as absent ones."""
        version = data.get("handoff_schema")
        if version != SCHEMA_VERSION:
            raise HandoffError(
                f"handoff_schema {version!r} is not {SCHEMA_VERSION}; this object was "
                "written under different rules and its fields cannot be assumed"
            )
        return cls(
            **{
                name: tuple(data.get(name) or ()) if name in LIST_FIELDS else data.get(name, "")
                for name in FIELDS
            },
            handoff_id=data["handoff_id"],
            run_id=data["run_id"],
            session_id=data.get("session_id"),
            reason=data["reason"],
            created_at=data["created_at"],
            watermark=data.get("watermark"),
            source=dict(data.get("source") or {}),
        )


# --- validation --------------------------------------------------------------


def _clean(values: Sequence[str]) -> tuple[str, ...]:
    return tuple(v.strip() for v in values if isinstance(v, str) and v.strip())


def problems(draft: HandoffDraft) -> tuple[str, ...]:
    """Everything wrong with this draft, named so the model can be told and asked again.

    Returned rather than raised, because the first thing done with them is to show them to
    the generator - a repair pass that could not say what was wrong would be a re-roll.
    """
    found: list[str] = []
    for name in REQUIRED_TEXT:
        if not str(getattr(draft, name, "") or "").strip():
            found.append(f"{name} is empty; it must say, in a sentence, what this is")
    for name in REQUIRED_LISTS:
        if not _clean(getattr(draft, name, []) or []):
            found.append(
                f"{name} is empty; a handoff with no {name} is a failed handoff, not a "
                "partial one. If there are genuinely none, say so as one entry and say why"
            )
    return tuple(found)


def build(
    draft: HandoffDraft,
    *,
    run_id: str,
    session_id: str | None,
    reason: str,
    watermark: int | None,
    source: dict[str, Any],
    active_subagents: Sequence[str] = (),
    important_memory_refs: Sequence[str] = (),
    handoff_id: str | None = None,
) -> Handoff:
    """Validate a draft and turn it into the stored object. Raises `HandoffInvalid`."""
    if reason not in REASONS:
        raise HandoffError(f"unknown handoff reason {reason!r}; expected one of {REASONS}")
    found = problems(draft)
    if found:
        raise HandoffInvalid(found)
    return Handoff(
        task=draft.task.strip(),
        user_intent=draft.user_intent.strip(),
        current_state=draft.current_state.strip(),
        decisions_made=_clean(draft.decisions_made),
        constraints=_clean(draft.constraints),
        completed_actions=_clean(draft.completed_actions),
        active_subagents=_clean(list(active_subagents)),
        relevant_evidence=_clean(draft.relevant_evidence),
        unresolved_questions=_clean(draft.unresolved_questions),
        next_actions=_clean(draft.next_actions),
        important_memory_refs=_clean(list(important_memory_refs)),
        handoff_id=handoff_id or str(uuid7()),
        run_id=run_id,
        session_id=session_id,
        reason=reason,
        created_at=utcnow().isoformat(),
        watermark=watermark,
        source=dict(source),
    )


# --- what the generator is allowed to read -----------------------------------


@dataclass(frozen=True)
class Source:
    """The message list after everything the runtime wrote has been taken out of it."""

    messages: tuple[dict[str, Any], ...]
    read: int
    synthetic_excluded: int
    system_excluded: int
    unflagged_runtime_text: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "messages_read": self.read,
            "synthetic_excluded": self.synthetic_excluded,
            "system_excluded": self.system_excluded,
            # Zero on every path that exists today. Non-zero means a caller built a message
            # list with runtime text in it and did not flag it, which is worth seeing in the
            # stored object rather than in a log nobody reads.
            "unflagged_runtime_text": self.unflagged_runtime_text,
        }


def source(
    messages: Sequence[dict[str, Any]], *, synthetic_ids: Sequence[str] = ()
) -> Source:
    """What may be summarised, and a count of everything that may not.

    `synthetic_ids` are the `tool_call_id`s of messages the runtime wrote in place of a
    tool result - `observations.closing_messages()` produces them and every one carries
    `synthetic=True`. They are identified by that flag at the call site and passed here as
    ids, because the message dicts themselves go to the provider and must stay exactly what
    the API accepts.
    """
    refused = frozenset(synthetic_ids)
    kept: list[dict[str, Any]] = []
    synthetic = system = runtime_text = 0
    for message in messages:
        role = message.get("role")
        if role == "system":
            system += 1
            continue
        if bool(message.get("synthetic")) or (
            message.get("tool_call_id") is not None and message["tool_call_id"] in refused
        ):
            synthetic += 1
            continue
        if RUNTIME_PREFIX in (message.get("content") or ""):
            runtime_text += 1
            continue
        kept.append(message)
    return Source(
        messages=tuple(kept),
        read=len(kept),
        synthetic_excluded=synthetic,
        system_excluded=system,
        unflagged_runtime_text=runtime_text,
    )


def transcript(src: Source, *, excerpt_chars: int, total_chars: int) -> str:
    """The source messages as labelled lines, newest kept first when the budget bites.

    Every line says who produced it. A tool result is labelled with the tool that produced
    it, resolved from the assistant message that called it, so a result is never attributable
    to the user or to the model.
    """
    names: dict[str, str] = {}
    for message in src.messages:
        for call in message.get("tool_calls") or []:
            names[call.get("id", "")] = (call.get("function") or {}).get("name", "tool")
    lines: list[str] = []
    for message in src.messages:
        role = message.get("role")
        body = (message.get("content") or "").strip()
        if role == "tool":
            who = f"tool {names.get(message.get('tool_call_id', ''), 'result')}"
        elif role == "assistant":
            called = ", ".join(
                (c.get("function") or {}).get("name", "?") for c in message.get("tool_calls") or []
            )
            who = f"assistant (called {called})" if called else "assistant"
        else:
            who = str(role)
        if not body and role == "assistant":
            body = "(no text; tool calls only)"
        if len(body) > excerpt_chars:
            # Whose truncation this is, said in the marker. The first live run of baseline
            # task B23 reported "the document you pasted was truncated in multiple places",
            # which attributed the handoff generator's own excerpting to the user's material.
            body = (
                f"{body[:excerpt_chars]}\n[... {len(body) - excerpt_chars} further "
                "characters of this message were not shown to the handoff generator]"
            )
        lines.append(f"{who}: {body}")
    out: list[str] = []
    used = 0
    for line in reversed(lines):
        if used + len(line) > total_chars:
            out.append(
                f"[... {len(lines) - len(out)} earlier messages were not shown to the "
                "handoff generator]"
            )
            break
        used += len(line)
        out.append(line)
    return "\n\n".join(reversed(out))


def carry_window(
    sizes: Sequence[tuple[int, int]], *, keep: int, budget_tokens: int
) -> tuple[int | None, int]:
    """Where the watermark goes, and how many messages stay above it.

    `sizes` is `(archive id, characters)` newest first. The window is bounded by *tokens*
    first and by count second, and that order is the point: a handoff whose successor
    inherits two 14,000-character pastes has replaced the conversation with a summary and
    freed almost nothing, which is the failure mode where a handoff costs a model call and
    buys no room. A message too large to fit is not carried - it is already in the object,
    which is what the object is for.

    Returns `(None, kept)` when nothing is dropped: there is no line to draw above a
    conversation that fits, and that is an answer rather than a missing value.
    """
    used = kept = 0
    for _id, chars in sizes[: max(keep, 0)]:
        cost = estimate_tokens_for_chars(chars)
        if used + cost > budget_tokens:
            break
        used += cost
        kept += 1
    if kept >= len(sizes):
        return None, kept
    return sizes[kept][0], kept


def _previous_block(previous: Handoff) -> str:
    return f"{PREVIOUS_HEADING}\n\n{render(previous)}"


# --- generating --------------------------------------------------------------


async def generate(
    messages: Sequence[dict[str, Any]],
    *,
    run_id: str,
    session_id: str | None = None,
    reason: str = REASON_THRESHOLD,
    cfg: Config | None = None,
    provider: LLMProvider | None = None,
    synthetic_ids: Sequence[str] = (),
    previous: Handoff | None = None,
    watermark: int | None = None,
    reading: budget.ContextReading | None = None,
    dropped: tuple[int, int] = (0, 0),
    active_subagents: Sequence[str] = (),
    important_memory_refs: Sequence[str] = (),
) -> tuple[Handoff, Source, int]:
    """One handoff from one live message list. Returns it with its source and the ms it took.

    Raises `HandoffInvalid` when the second attempt is still not a handoff, and `HandoffError`
    when the model call itself failed. Both are states the caller journals; neither has a
    fallback that quietly produces an object anyway.
    """
    cfg = cfg or get_config()
    started = time.perf_counter()
    src = source(messages, synthetic_ids=synthetic_ids)
    body = transcript(
        src,
        excerpt_chars=cfg.handoff.excerpt_chars,
        total_chars=cfg.handoff.source_chars,
    )
    prompt: list[dict[str, Any]] = [{"role": "system", "content": INSTRUCTION}]
    if previous is not None:
        prompt.append({"role": "user", "content": _previous_block(previous)})
    prompt.append({"role": "user", "content": f"{TRANSCRIPT_HEADING}\n\n{body}"})

    llm = provider or get_provider(cfg)
    params = params_for(cfg.handoff.generator_role, cfg)
    attempt = list(prompt)
    last: tuple[str, ...] = ()
    for _ in range(2):
        try:
            draft = await llm.complete_json(attempt, HandoffDraft, params=params)
        except LLMError as exc:
            raise HandoffError(f"handoff generation failed: {exc}") from exc
        last = problems(draft)
        if not last:
            handoff = build(
                draft,
                run_id=run_id,
                session_id=session_id,
                reason=reason,
                watermark=watermark,
                source={
                    **src.as_dict(),
                    "carried_tokens": None if reading is None else reading.used_tokens,
                    "ceiling_tokens": None if reading is None else reading.ceiling_tokens,
                    "basis": None if reading is None else reading.basis,
                    "model": params.model,
                    "supersedes": None if previous is None else previous.handoff_id,
                    # What the successor no longer has, in its own units. `render` turns
                    # this into a sentence, because a successor that is not told how much
                    # was taken away answers as though nothing was - which is what the
                    # first run of baseline task B23 did.
                    "dropped_messages": dropped[0],
                    "dropped_chars": dropped[1],
                },
                active_subagents=active_subagents,
                important_memory_refs=important_memory_refs,
            )
            return handoff, src, int((time.perf_counter() - started) * 1000)
        attempt = [
            *prompt,
            {"role": "assistant", "content": json.dumps(draft.model_dump())},
            {
                "role": "user",
                "content": (
                    "That is not a usable handoff:\n- "
                    + "\n- ".join(last)
                    + "\nReturn the whole object again, corrected."
                ),
            },
        ]
    raise HandoffInvalid(last)


# --- starting the successor --------------------------------------------------

# The one heading a successor orchestrator reads. It says three things and each is load
# bearing: this is a compression and not a transcript, it was written by the runtime rather
# than said by anyone, and the conversation it describes is not coming back.
RENDER_HEADING = """## Handoff from the earlier part of this conversation

The conversation before this point ran out of room to carry and was replaced by the summary
below. It was written by the runtime, not said by the user, and the original messages are
not available to you. Treat it as the record of the work so far; where it is silent, say so
rather than filling it in."""

# How much was taken away, in the units a reader can act on. Derived from the object's own
# provenance rather than written by the generator, and separate from the heading because a
# general warning and a specific quantity do different work: baseline task B23's first run
# obeyed the heading when asked what the user had *said* and ignored it when asked about the
# material they had pasted, answering from nothing with no hedge at all.
DROPPED_LINE = (
    "{messages} earlier messages ({chars:,} characters) were replaced by this summary and "
    "their text is gone. If a question needs what was in them, say that you no longer have "
    "it rather than answering from the summary."
)


def render(handoff: Handoff) -> str:
    """The handoff as the block a fresh orchestrator is started with."""
    parts = [RENDER_HEADING]
    dropped = int(handoff.source.get("dropped_messages") or 0)
    if dropped:
        parts += [
            "",
            DROPPED_LINE.format(
                messages=dropped, chars=int(handoff.source.get("dropped_chars") or 0)
            ),
        ]
    parts.append("")
    for name in FIELDS:
        value = getattr(handoff, name)
        label = name.replace("_", " ")
        if isinstance(value, tuple):
            if not value:
                continue
            parts.append(f"{label}:")
            parts.extend(f"  - {item}" for item in value)
        elif value:
            parts.append(f"{label}: {value}")
    return "\n".join(parts)


def fresh_messages(
    handoff: Handoff,
    *,
    cfg: Config,
    autonomy: str,
    context_block: str = "",
    recent: Sequence[dict[str, Any]] = (),
    user_text: str,
) -> list[dict[str, Any]]:
    """The message list a fresh orchestrator starts from.

    System instructions, the memory retrieved for this turn, the handoff, the conversation
    since the handoff's watermark, and the user. Nothing from before the watermark: the pass
    file's second *Must not* is that the old context is never copied in, and the way that is
    enforced is that this function is not given it.
    """
    return ctxmod.build_messages(
        cfg,
        autonomy=autonomy,
        context_block=context_block,
        history=list(recent),
        user_text=user_text,
        handoff_block=render(handoff),
    )


__all__ = [
    "DERIVED_FIELDS",
    "DROPPED_LINE",
    "FIELDS",
    "INSTRUCTION",
    "LIST_FIELDS",
    "REASONS",
    "REASON_FORCED",
    "REASON_THRESHOLD",
    "REQUIRED_LISTS",
    "REQUIRED_TEXT",
    "SCHEMA_VERSION",
    "Handoff",
    "HandoffDraft",
    "HandoffError",
    "HandoffInvalid",
    "Source",
    "build",
    "carry_window",
    "fresh_messages",
    "generate",
    "problems",
    "render",
    "source",
    "transcript",
]
