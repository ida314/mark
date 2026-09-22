"""Cold resume: what a run is picked up from after the process holding it died.

Session 4b left this half-built and said so. It can fold a run out of the journal, name the
effects the crash interrupted and announce the resume; what it cannot do is start a turn,
because a turn needs *message bodies* and the journal holds 200-character previews. This is
the other half, and it is the pass file's resume policy made executable:

```
resume(run_id)
    load latest checkpoint
    reconcile orphaned effects
    if fresh and messages fit within budget:
        rehydrate full message list          # mechanical, lossless
    else:
        rehydrate from handoff_object + recent turns + memory refs
```

## Two paths, and which one is the default

**Lossless wins wherever it fits.** That is the pass file's third *Must not* - the lossy
path taken where the lossless one fits - and it is the reason the first question asked here
is not "is there a handoff" but "does the conversation still fit". A run killed two minutes
ago, whose whole conversation is four messages, is resumed by replaying four messages. It
costs nothing, loses nothing, and a handoff generated for it would be a model call spent
compressing something that was never large.

**Compressed is for the two cases where lossless is not available**: the conversation no
longer fits inside `[handoff] resume_budget_tokens`, or the run is older than
`[handoff] warm_window_s` and is being picked up about something else. In both, what is
carried forward is a handoff object - the stored one when there is one, and one generated
from the journal and the archive when there is not.

## Generating a handoff from a dead run

`WARM`/`COLD` decide the path; this is what makes the cold one possible at all. Session 4b
refused to hand the journal's previews to a model as message bodies, and that refusal is
kept: bodies come from the Postgres archive, and any message the archive does not hold is
shown to the generator **as a preview, labelled as one**, and counted in
`source["preview_only"]`. A handoff written mostly from previews is a worse handoff, and
the object says how much of it was.

The archive holds more of a dead turn than it used to. `loop.py` now writes an
`assistant_step` row as each step's prose arrives, rather than only the joined answer at the
end of a turn that completed - Dylan's ruling at the Pass 4/5 boundary, taken with its
stated cost: **it fixes nothing for runs already journaled.** A run recorded before that
change has no mid-turn prose in the archive and never will, so its generated handoff is
built from the user's messages, the tool results, and previews of what the model said. That
is a real degradation and it is reported rather than hidden.

## What this module does not do

It does not write. `resume.resume()` is the one call that writes, and it still is: this
module reads a plan, decides a path and assembles a message list. Nothing here re-executes
a tool, and nothing here decides that an interrupted `unsafe_write` may be run again -
`executor.py`'s rerun guard is what refuses that, and `observations.closing_messages` is
what tells the model why.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID

from ..config import Config, get_config
from ..db import repo_archive
from ..ids import estimate_tokens_for_chars, utcnow
from ..journal import resume as resume_mod
from ..journal.store import JournalStore
from ..llm.base import LLMProvider
from ..obs.telemetry import messages_tokens
from . import handoff as handoff_mod
from . import observations as obs
from .handoff import Handoff

# Which path a resume took, and why. Two values and not a bool, because "this run was
# replayed" and "this run was compressed" are different enough that a caller rendering a
# line about it should not have to remember which way round `True` meant.
LOSSLESS = "lossless"
COMPRESSED = "compressed"

# Why the lossless path was not available. One of these is always set when the path is
# `compressed`, so "we compressed it" never has to be read as "we felt like it".
TOO_LARGE = "messages_exceed_budget"
TOO_OLD = "outside_warm_window"
# The third, and the one that is not about size or age: this conversation has already
# handed off. Found by a test rather than designed, and worth the words.
#
# A conversation crosses the threshold at `ceiling - threshold` = 16,000 estimated tokens
# and is compressed there. `resume_budget_tokens` is the same ceiling, 24,000. So the
# conversation that has *just* handed off still "fits", and a resume that asked only about
# size would replay it in full, discard a handoff somebody already paid a model call for,
# and cross the threshold again at the end of the very first resumed turn - paying for a
# second one. Nothing is lost either way; what is wasted is the compression, twice.
#
# This is not the pass file's third *Must not* in disguise. That forbids the lossy path
# where the lossless one fits, and the lossless one does not fit here: it is available for
# exactly one turn. And with the manifest and the lookup, what the handoff dropped is
# named and fetchable rather than gone.
ALREADY_COMPRESSED = "already_handed_off"
# And a fourth, for the run that has no conversation to take either path with: session 3b's
# `detached:<action_id>`, a queued approval replayed long after its turn ended. It has
# effects worth reconciling and no session at all. Naming it is not pedantry - the
# alternative was reporting it as `messages_exceed_budget`, which is a statement about a
# budget nothing was measured against, and this codebase's characteristic bug is a real
# value degrading into a plausible wrong one that still folds.
NO_CONVERSATION = "no_conversation"

# Where a compressed resume's handoff came from.
FROM_CHECKPOINT = "checkpoint"
FROM_JOURNAL = "generated_from_journal"
NO_HANDOFF = "none"


class RehydrationError(RuntimeError):
    """A run could not be prepared for continuation. Raised, never degraded into an empty
    message list that looks like a conversation nobody had."""


@dataclass(frozen=True)
class Restart:
    """Everything needed to take one run's next turn, and an account of how it was decided.

    `handoff` is None on the lossless path and that is not a missing value: a conversation
    replayed in full has nothing to summarise. `closing` is the synthetic tool messages for
    calls the crash interrupted - already `synthetic=True`, already carrying
    `RUNTIME_PREFIX`, so nothing downstream can mistake one for something a tool returned.
    """

    run_id: str
    session_id: UUID | None
    path: str
    reason: str | None
    handoff: Handoff | None
    handoff_source: str
    history: tuple[dict[str, Any], ...]
    closing: tuple[obs.ClosingMessage, ...]
    notice: str
    age_s: float | None
    carried_tokens: int
    budget_tokens: int
    plan: resume_mod.ResumePlan
    generated_ms: int | None = None
    source: dict[str, Any] = field(default_factory=dict)

    @property
    def lossless(self) -> bool:
        return self.path == LOSSLESS


def budget_tokens(cfg: Config | None = None) -> int:
    """The budget a replayed conversation must fit inside.

    `agent.history_tokens` unless overridden, because that is the budget the very next turn
    would be spent against anyway: a list that does not fit there is one `history_messages`
    would silently trim on the first turn, which is exactly the quiet loss a handoff exists
    to replace with something deliberate.
    """
    cfg = cfg or get_config()
    configured = cfg.handoff.resume_budget_tokens
    return cfg.agent.history_tokens if configured is None else configured


def _parse_ts(text: str | None) -> datetime | None:
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC_FALLBACK)


UTC_FALLBACK = utcnow().tzinfo


def age_seconds(store: JournalStore, run_id: str) -> float | None:
    """How long since anything happened in this run, or None if that cannot be told.

    None rather than 0 or `inf`: a run whose events carry an unparseable timestamp has an
    unknown age, and both of the obvious fillers are a decision wearing a measurement's
    clothes - one says "brand new", the other "ancient", and the path taken differs.
    """
    events = store.read(run_id)
    if not events:
        return None
    stamped = _parse_ts(events[-1].ts)
    if stamped is None:
        return None
    return max((utcnow() - stamped).total_seconds(), 0.0)


async def conversation_tokens(session_id: UUID) -> int:
    """What replaying this session's whole conversation would cost, as an estimate.

    Sizes, not bodies. Deciding whether a 42,000-character paste fits does not require
    loading it, and `recent_message_sizes` is the query that already knows that.
    """
    sizes = await repo_archive.recent_message_sizes(session_id)
    return sum(estimate_tokens_for_chars(chars) for _id, chars in sizes)


async def restart(
    run_id: str,
    *,
    store: JournalStore,
    cfg: Config | None = None,
    provider: LLMProvider | None = None,
    now_s: float | None = None,
) -> Restart:
    """Decide how this run continues, and assemble what its next turn starts from.

    Reads. Writes nothing - not the journal, not the archive, not the ledger. The caller
    that actually continues the run calls `resume.resume()` first, which is what announces
    the resume and closes the orphaned effects; doing that here would mean planning a
    continuation and reconciling as a side effect of asking a question.
    """
    cfg = cfg or get_config()
    plan = resume_mod.plan(run_id, store=store)
    session_id = (
        UUID(plan.rehydration.session_id) if plan.rehydration.session_id else None
    )
    closing = obs.closing_messages(plan)
    notice = obs.notice(plan)
    age = now_s if now_s is not None else age_seconds(store, run_id)
    limit = budget_tokens(cfg)

    carried = 0 if session_id is None else await conversation_tokens(session_id)
    fits = carried <= limit
    fresh = age is not None and age <= cfg.handoff.warm_window_s
    stored = plan.checkpoint.handoff_object if plan.checkpoint else None

    if session_id is not None and fits and fresh and not stored:
        # The mechanical path. `history_messages` with no watermark: there is no handoff in
        # force, so nothing has been replaced and everything is replayed.
        history = await _lossless_history(session_id, limit=limit, cfg=cfg)
        return Restart(
            run_id=run_id, session_id=session_id, path=LOSSLESS, reason=None,
            handoff=None, handoff_source=NO_HANDOFF, history=tuple(history),
            closing=closing, notice=notice, age_s=age,
            carried_tokens=messages_tokens(history), budget_tokens=limit, plan=plan,
            source={"messages_replayed": len(history), "conversation_tokens": carried},
        )

    if session_id is None:
        reason = NO_CONVERSATION
    elif stored:
        reason = ALREADY_COMPRESSED
    elif not fits:
        reason = TOO_LARGE
    else:
        reason = TOO_OLD

    handoff, source_of, generated_ms, provenance = await _compressed_handoff(
        run_id, plan=plan, session_id=session_id, cfg=cfg, provider=provider
    )
    history = (
        ()
        if session_id is None or handoff is None
        else await _carried_history(session_id, handoff=handoff, cfg=cfg)
    )
    return Restart(
        run_id=run_id, session_id=session_id, path=COMPRESSED, reason=reason,
        handoff=handoff, handoff_source=source_of, history=tuple(history),
        closing=closing, notice=notice, age_s=age,
        carried_tokens=messages_tokens(list(history)), budget_tokens=limit, plan=plan,
        generated_ms=generated_ms,
        source={"conversation_tokens": carried, **provenance},
    )


async def _lossless_history(
    session_id: UUID, *, limit: int, cfg: Config
) -> list[dict[str, Any]]:
    from . import context as ctxmod

    return await ctxmod.history_messages(session_id, budget_tokens=limit)


async def _carried_history(
    session_id: UUID, *, handoff: Handoff, cfg: Config
) -> list[dict[str, Any]]:
    from . import context as ctxmod

    return await ctxmod.history_messages(
        session_id,
        budget_tokens=cfg.handoff.carry_tokens,
        after_id=handoff.watermark,
    )


async def _compressed_handoff(
    run_id: str,
    *,
    plan: resume_mod.ResumePlan,
    session_id: UUID | None,
    cfg: Config,
    provider: LLMProvider | None,
) -> tuple[Handoff | None, str, int | None, dict[str, Any]]:
    """The stored handoff, or one generated from the journal, or neither."""
    stored = plan.checkpoint.handoff_object if plan.checkpoint else None
    if stored:
        try:
            return Handoff.from_dict(stored), FROM_CHECKPOINT, None, {"stored": True}
        except handoff_mod.HandoffError as exc:
            # A stored object this build cannot read is not a reason to continue without
            # one: it is a reason to generate a fresh one and say what happened to the old.
            unreadable = str(exc)
        else:  # pragma: no cover - the try returns
            unreadable = ""
    else:
        unreadable = ""

    if session_id is None:
        return None, NO_HANDOFF, None, {
            "stored": False,
            "why_none": "the run has no session, so there is no conversation to compress",
        }

    generated, ms, provenance = await generate_from_journal(
        run_id, plan=plan, session_id=session_id, cfg=cfg, provider=provider
    )
    if unreadable:
        provenance["stored_object_unreadable"] = unreadable
    if generated is None:
        return None, NO_HANDOFF, ms, provenance
    return generated, FROM_JOURNAL, ms, provenance


# --- a handoff for a run that has none ---------------------------------------
#
# The pass file: "If a cold resume needs a handoff object and none exists, generate one
# from the journal with a cheap model call before starting the new orchestrator." The
# generator is `handoff.generate`, unchanged; what is new is where its transcript comes
# from, and the one rule that governs it.
#
# **Bodies from the archive, previews only as previews.** Session 4b refused to hand the
# journal's 200-character previews to a model as message bodies, because text nobody said,
# presented as text somebody said, is a laundering channel. That refusal is kept rather
# than traded away for a better handoff: the archive is read for bodies, and a message the
# journal recorded that the archive does not hold is shown *labelled as a preview* and
# counted into `source["preview_only"]`. A handoff written mostly from previews is worse,
# and the object is where that is said.


def _collapse(text: str | None) -> str:
    return " ".join((text or "").split())


ROLE_FOR_KIND: dict[str, str] = {
    "user_message": "user",
    "assistant_message": "assistant",
    # Session 5c's archive write. One row per step as the prose arrives, so a turn that
    # died mid-flight leaves the model's own words behind it rather than only its tool
    # calls. Never replayed into a prompt - `recent_messages` does not select it - so it
    # adds nothing to any turn's history and exists for exactly this reader.
    "assistant_step": "assistant",
    "tool_result": "tool",
}


async def _archive_messages(
    session_id: UUID, *, limit: int
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """This session's archived rows as a message list, oldest first.

    Everything the archive holds, not only what a later prompt would replay: the point of
    this list is to reconstruct what the dead run *did*, and most of what a turn learns is
    in its tool results.
    """
    rows = await repo_archive.events_for_session(session_id, limit=limit)
    messages: list[dict[str, Any]] = []
    counts = {"user": 0, "assistant": 0, "tool": 0}
    for row in rows:
        role = ROLE_FOR_KIND.get(str(row.get("kind")))
        if role is None:
            continue
        message: dict[str, Any] = {"role": role, "content": row.get("content") or ""}
        if role == "tool":
            actor = str(row.get("actor") or "")
            message["tool_name"] = actor[len("tool:"):] if actor.startswith("tool:") else actor
        messages.append(message)
        counts[role] += 1
    return messages, counts


def _preview_only(plan: resume_mod.ResumePlan, archived: list[dict[str, Any]]) -> list[dict]:
    """What the journal says was said and the archive does not hold.

    Matched by prefix on the collapsed text, which is exactly the transformation
    `events.preview` applies, so a preview that is genuinely the head of an archived row is
    recognised as one rather than added a second time. What survives the match is real
    loss - mid-turn prose from a run recorded before `assistant_step` existed, which is the
    stated cost of Dylan's ruling: **it fixes nothing for runs already journaled.**
    """
    bodies = [_collapse(m.get("content")) for m in archived if m.get("role") == "assistant"]
    missing: list[dict[str, Any]] = []
    for message in plan.rehydration.messages:
        if message.role != "assistant" or not message.preview:
            continue
        head = message.preview.rstrip("…")
        if any(body.startswith(head) for body in bodies if head):
            continue
        missing.append(
            {
                "role": "assistant",
                "content": message.preview,
                "preview_only": True,
                "chars": message.chars,
            }
        )
    return missing


async def generate_from_journal(
    run_id: str,
    *,
    plan: resume_mod.ResumePlan,
    session_id: UUID,
    cfg: Config | None = None,
    provider: LLMProvider | None = None,
) -> tuple[Handoff | None, int | None, dict[str, Any]]:
    """One handoff for a run that stored none. Returns it, the ms it took, and provenance.

    None rather than a raise when generation fails, with the reason in the provenance: the
    caller is a resume, and a resume that cannot summarise a dead run should say so and
    carry on with what it does have, not refuse to continue the run at all. Nothing here
    invents an object - the failure modes of `handoff.generate` are unchanged and there is
    still no fallback that produces a thin one.
    """
    cfg = cfg or get_config()
    archived, counts = await _archive_messages(session_id, limit=400)
    missing = _preview_only(plan, archived)
    provenance: dict[str, Any] = {
        "archived_messages": len(archived),
        "archived_by_role": counts,
        "preview_only": len(missing),
        "journal_messages": len(plan.rehydration.messages),
        "run_state": plan.state,
    }
    if not archived and not missing:
        provenance["why_none"] = (
            "the archive holds nothing for this session and the journal holds no message "
            "previews, so there is no transcript to compress"
        )
        return None, None, provenance

    # Everything is dropped: a cold resume carries no conversation forward by itself, so
    # the manifest is the whole of what this session holds.
    upto = await repo_archive.max_event_id(session_id)
    items = handoff_mod.manifest(
        await repo_archive.manifest_rows(
            session_id,
            upto_id=upto,
            watermark=upto,
            excerpt_chars=cfg.handoff.manifest_excerpt_chars,
            limit=cfg.handoff.manifest_items,
        )
    )
    dropped_chars = sum(len(m.get("content") or "") for m in archived)
    try:
        handoff, _src, ms = await handoff_mod.generate(
            [*archived, *missing],
            run_id=run_id,
            session_id=str(session_id),
            reason=handoff_mod.REASON_RESUME,
            cfg=cfg,
            provider=provider,
            # The watermark is where the successor's history window starts, and for a cold
            # resume that is the end of everything: the conversation is being replaced by
            # the object, not extended past a point in it.
            watermark=upto,
            dropped=(len(archived), dropped_chars),
            dropped_manifest=items,
        )
    except handoff_mod.HandoffError as exc:
        provenance["generation_failed"] = str(exc)
        return None, None, provenance
    handoff.source.update(provenance)
    return handoff, ms, provenance


__all__ = [
    "ALREADY_COMPRESSED",
    "COMPRESSED",
    "NO_CONVERSATION",
    "FROM_CHECKPOINT",
    "FROM_JOURNAL",
    "LOSSLESS",
    "NO_HANDOFF",
    "TOO_LARGE",
    "TOO_OLD",
    "RehydrationError",
    "Restart",
    "age_seconds",
    "budget_tokens",
    "conversation_tokens",
    "generate_from_journal",
    "restart",
]
