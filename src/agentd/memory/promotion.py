"""Promotion: the one place disposable execution state writes into durable memory.

Session 7c.

    working memory
        |
    classification
        |-- discard
        |-- episodic
        `-- semantic

Nothing else in this runtime moves a fact from a run into the user's memory, and that is
what makes this the file where a crash is expensive. The failure to design against is named
in the pass file: **duplicate semantic memory**. It does not throw, nothing reports it, and
it degrades retrieval quietly for months, because two rows saying the same thing are
indistinguishable from two independent observations - which is precisely the evidence the
review gate counts when it decides whether to believe something.

## The protocol, and why it is in this order

    1. classify          the boundary asks what each note is; nothing is written yet
    2. promotion_classified   journaled, synchronously, one per note kept, in full
    3. promotion_batch        journaled: every note the boundary saw, counted
    4. the durable write      an archive row or a candidate row, keyed on the promotion
    5. promotion_committed    journaled, synchronously: where it landed

Killed between 3 and 4, the journal holds a classified promotion with nothing after it.
`journal/promotions.pending_at` finds it, `complete_pending` writes it, and nothing is
lost. Killed between 4 and 5 - the write committed in postgres and the process died before
the journal heard - the same resume tries the same write again, and the *store* refuses it:
the promotion key is unique in both targets, so the second attempt returns the first
attempt's row and records `inserted=False`. Neither ordering can produce a second fact.

The ledger row is not what prevents the duplicate and must not be mistaken for it. It
records that the write was attempted and how it ended; the ledger explicitly does not
suppress a second attempt (`journal/ledger.py`: "It does not prevent anything"). The
suppression is the unique index, because that is the only guard that is still standing
after the process holding the ledger handle has died.

## The key, and the hazard in the default shape

    idempotency_key = hash(run_id, step_id, tool_name, canonical_args)

`step_id` is in that shape, and 4c already recorded the hazard: the *same logical call* made
again in a later step does not collide with the row already open. For promotion that hazard
is the whole bug - a resume runs in a different step than the boundary that classified the
note, so the default derivation would hand the retry a fresh key, a fresh ledger row, and a
fresh dedup key, and the duplicate this file exists to prevent would arrive through the
mechanism meant to prevent it.

So the step is the **boundary**, not the step that wrote the note: `promote:<scope>`. A
scope is promoted at exactly one boundary, so that string is the same in the process that
classified the note and in the process that finishes the job a week later. What goes into
the canonical args is the note's identity and its content digest, so re-stating the same
note under the same key is the same promotion and a different sentence is a different one.

## Batching

At a task or run boundary and nowhere else, which is the pass file's second *Must not*:

    task boundary   `agent/subagents.py`, after `worker_finished`, before the scope is
                    discarded. One batch per worker.
    run boundary    `agent/loop.py`, at the end of the turn, before the orchestrator's
                    scope is discarded. One batch per run.

Both sit immediately before the discard 7b put there, so the last thing that reads a scope
is the thing that decides what to keep from it.

## Nothing is promoted that the classifier did not classify

The pass file's first *Must not*. Three shapes of that, each of them a real way to get it
wrong:

* **A note the classifier was never asked about is not promoted.** Runtime-authored text
  and text carrying a secret are refused before the prompt is built, and counted into
  `promotion_batch` as `synthetic` and `secret` - Pass 5's handoff generator counts its
  exclusions into the stored object rather than filtering quietly, and this is the same
  requirement one pass later.
* **A note the classifier answered about unusably is not promoted.** No cross-field
  validator and no default: the response schema is decoded permissively and reconciled
  afterwards (`_reconcile`), because `complete_json` gets one repair attempt and then
  raises, and an aborted turn is a worse failure than an unclassified note. A missing
  decision, an unknown key, a target that is not a bucket - all `unclassified`, counted,
  and left in the journal.
* **A classifier failure promotes nothing at all.** `classifier="failed"` with the error in
  the event. Not a fallback to "semantic looks likely".

## The Pass 4/5 rule: synthetic text never becomes a fact

> Messages with `synthetic=True` are never summarized or promoted as fact.

7a found that today's promotion path satisfies this *structurally* - it reads `raw_events`,
which has no synthetic column - and warned that the protection is an accident of the input.
This path does not inherit that accident, because its input is a note an agent wrote. So the
rule is a check here rather than a property: `_runtime_authored` refuses any note whose text
begins with one of `observations.RUNTIME_MARKERS`, the same tuple `agent/handoff.py` refuses
on, and the refusal is counted into `promotion_batch.synthetic` so that "none were excluded"
and "nobody looked" are different observations.

## What is written, and what is not

Neither branch writes a fact. `memory/review.py` is the only writer of `facts` and stays so:
the semantic branch proposes a **candidate**, at a confidence below the gate's acceptance
threshold and above its floor, with `proposed_by="promotion"` - never `"user"`, which would
launder a model's own inference through both the lower confidence floor and the exemption
from the evidence requirement. The episodic branch writes an **archive row**, which is the
episodic record this system actually has (`episodes` has held 0 rows across 111
consolidations; 7a's open question 1), and it is the branch with a real monotonic position.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field

from ..agent import observations as obs
from ..agent.working_memory import Note, WorkingMemory
from ..config import Config, get_config
from ..db import repo_archive, repo_memory
from ..journal import promotions as fold
from ..journal.ledger import EffectLedger
from ..journal.runtime import RunJournal
from ..journal.store import JournalStore
from ..journal.writer import JournalWriter
from ..llm.roles import get_provider, params_for
from ..tools.effects import IDEMPOTENT_WRITE
from ..tools.idempotency import idempotency_key
from .review import contains_secret

DISCARD = "discard"
EPISODIC = fold.EPISODIC
SEMANTIC = fold.SEMANTIC
TARGETS: tuple[str, ...] = (DISCARD, EPISODIC, SEMANTIC)

# The effect name and class these writes are recorded under.
#
# Not a registered tool, and so not in `docs/records/effect-classification.md`, whose test
# asserts that table is exactly the registry. Promotion is a runtime action at a boundary:
# the model cannot call it, there is no schema to declare, and giving it a registry entry
# would put a durable memory write on the tool surface - which is the one door this pass is
# supposed to be closing, not opening. The class is declared here instead, once, and it is
# `idempotent_write` for a reason that is checkable rather than hopeful: both targets refuse
# a second write under the same promotion key, so re-execution converges by construction.
PROMOTION_TOOL = "memory_promote"
PROMOTION_EFFECT_CLASS = IDEMPOTENT_WRITE

# Who is recorded as proposing a promoted candidate. Deliberately not `"user"` and not
# `ctx.actor`: `review.process_candidate` gives `proposed_by="user"` a lower confidence
# floor *and* an exemption from the evidence requirement, so a model's note arriving under
# that name would be the model's own inference wearing the user's authority.
PROPOSED_BY = "promotion"

# The confidence a promoted note is proposed at. Between `review.MIN_CONFIDENCE` (0.50, or
# the gate drops it) and `review.ACCEPT_CONFIDENCE` (0.70, or the gate accepts it
# unattended), so a note the agent kept for itself becomes something a human or the judge
# looks at rather than something that is simply true now.
#
# A fixed number rather than one the classifier picks. A model scoring its own inference's
# reliability is the same laundering channel as `proposed_by="user"`, one field over.
CONFIDENCE = 0.6

# The archive `kind` an episodic promotion lands under. New kind, not `assistant_message`:
# `repo_archive.recent_messages` rebuilds prompt history from `user_message` and
# `assistant_message` only, so a promoted note is consolidation input (it is in
# `events_for_session`) without being replayed into anybody's conversation.
ARCHIVE_KIND = "working_note"

CLASSIFY_SYSTEM = (
    "You decide what an agent should keep from its own scratch notes once a task is over.\n"
    "For each note, answer with exactly one target:\n"
    "  semantic - a durable fact about the user, their preferences, their commitments or "
    "their world. Something that will still be true and still be worth knowing next week.\n"
    "  episodic - worth remembering that it happened, but not a standing fact: what was "
    "done, what was found, how something turned out.\n"
    "  discard  - scratch. Working state, intermediate reasoning, restatements of the task, "
    "anything true only inside the task that just ended.\n"
    "Most notes are discard. Choose semantic only for a claim you would be willing to have "
    "repeated back to the user as something the agent knows about them.\n"
    "Return one decision per note, keyed by the note's key, with a short reason."
)


class NoteDecision(BaseModel):
    """One decision, decoded permissively and reconciled afterwards.

    Every field defaults, and that is not this codebase's plausible-NULL bug: an empty
    `target` is not a bucket, so `_reconcile` counts the note as unclassified and promotes
    nothing. The alternative - required fields, or a cross-field validator - turns a model
    that answers badly into a `ValidationError`, which `complete_json` retries once and then
    raises as `LLMError`, which at a turn-end boundary is an aborted turn. A note nobody
    classified is a far cheaper failure than a turn nobody finished.
    """

    key: str = ""
    target: str = ""
    reason: str = ""


class Classification(BaseModel):
    decisions: list[NoteDecision] = Field(default_factory=list)


@dataclass(frozen=True)
class Decision:
    """What the boundary decided about one note, and who decided it."""

    key: str
    target: str
    reason: str
    decided_by: str


@dataclass
class Batch:
    """One classification pass over one scope: the decisions, and every note accounted for.

    The counts are not diagnostics. `notes` equals the sum of the six outcomes, which is how
    "nothing was quietly dropped" is checked instead of claimed.
    """

    scope: str
    boundary: str
    notes: int = 0
    classifier: str = "rules"
    error: str | None = None
    decisions: tuple[Decision, ...] = ()
    synthetic: int = 0
    secret: int = 0
    discarded: int = 0
    unclassified: int = 0
    downgraded: int = 0

    @property
    def episodic(self) -> int:
        return sum(1 for d in self.decisions if d.target == EPISODIC)

    @property
    def semantic(self) -> int:
        return sum(1 for d in self.decisions if d.target == SEMANTIC)

    @property
    def accounted(self) -> int:
        return (
            self.synthetic + self.secret + self.discarded + self.unclassified
            + self.episodic + self.semantic
        )

    def as_event(self) -> dict[str, Any]:
        return {
            "scope": self.scope,
            "boundary": self.boundary,
            "notes": self.notes,
            "episodic": self.episodic,
            "semantic": self.semantic,
            "discarded": self.discarded,
            "unclassified": self.unclassified,
            "synthetic": self.synthetic,
            "secret": self.secret,
            "downgraded": self.downgraded,
            "classifier": self.classifier,
            "error": self.error,
        }


@dataclass
class Promoted:
    """What a boundary actually did. Returned for the caller's logs and for tests."""

    batch: Batch | None = None
    classified: tuple[str, ...] = ()
    committed: tuple[str, ...] = ()
    reused: tuple[str, ...] = ()
    failed: dict[str, str] = field(default_factory=dict)


# --- keys --------------------------------------------------------------------


def promotion_step_id(scope: str) -> str:
    """The `step_id` a promotion is keyed under: the boundary, not the step.

    See the module docstring. A scope has exactly one promotion boundary, so this string is
    identical in the process that classified the note and in the one that finishes the write
    after a crash - which is the property the default key shape does not have.
    """
    if not scope:
        raise ValueError("a promotion needs a scope; there is no scope-less boundary")
    return f"promote:{scope}"


def text_digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def promotion_key(
    *, run_id: str, scope: str, key: str, target: str, text_sha256: str
) -> str:
    """`hash(run_id, promote:<scope>, memory_promote, {key, target, digest})`.

    The content digest is in the args rather than the text itself so the key does not carry
    the note around, and so that re-noting the same key with a *different* sentence is a
    different promotion rather than a silent overwrite of a claim that is already durable.
    """
    if target not in (EPISODIC, SEMANTIC):
        raise ValueError(f"{target!r} is not a promotion target; nothing is written for it")
    return idempotency_key(
        run_id=run_id,
        step_id=promotion_step_id(scope),
        tool_name=PROMOTION_TOOL,
        args={"scope": scope, "key": key, "target": target, "text_sha256": text_sha256},
    )


# --- classification ----------------------------------------------------------


def _runtime_authored(text: str) -> bool:
    """Whether the runtime wrote this, rather than an agent or a tool.

    The Pass 4/5 rule, as a check rather than as a property of the plumbing. The same tuple
    `agent/handoff.py` refuses on, read from `agent/observations.py` so a third marker added
    there cannot be forgotten here.
    """
    stripped = text.lstrip()
    return stripped.startswith(obs.RUNTIME_MARKERS)


def _prompt(notes: tuple[Note, ...]) -> str:
    lines = [f"[{n.key}] {n.text}" for n in notes]
    return "Notes from the task that just ended:\n\n" + "\n".join(lines)


async def classify(
    notes: tuple[Note, ...],
    *,
    scope: str,
    boundary: str,
    cfg: Config | None = None,
    provider: Any = None,
) -> Batch:
    """Decide what to keep from one scope. Writes nothing.

    Separated from the write so the *Must not* is visible: promotion takes a `Batch`, and
    the only way to get one is through here.

    `provider` is the boundary's own, not the module-global one. The agent whose scope this
    is was built around a provider - a worker's may differ from its caller's, and a test's
    is a script - and falling through to `get_provider()` here would classify a worker's
    notes with a model nobody chose for it. It is also how a test that never touches the
    network can still exercise this path: the global fallback reaches the real endpoint.
    """
    cfg = cfg or get_config()
    batch = Batch(scope=scope, boundary=boundary, notes=len(notes))
    askable: list[Note] = []
    for note in notes:
        if _runtime_authored(note.text):
            batch.synthetic += 1
            continue
        if contains_secret(note.text):
            # The gate screens candidates for secrets at review time, which is after they
            # have already been written and after they are renderable as pending claims.
            # This is the one path where the screen can run before the write, so it does.
            batch.secret += 1
            continue
        askable.append(note)
    if not askable:
        return batch

    provider = provider or get_provider(cfg)
    try:
        answer = await provider.complete_json(
            [
                {"role": "system", "content": CLASSIFY_SYSTEM},
                {"role": "user", "content": _prompt(tuple(askable))},
            ],
            Classification,
            params=params_for("consolidate", cfg),
        )
    except Exception as exc:
        # Every note stays unclassified and nothing is promoted. Recorded, not swallowed:
        # the count and the message are in `promotion_batch`, and the notes are still in the
        # journal for a later boundary or a human to look at.
        batch.classifier = "failed"
        batch.error = str(exc)
        batch.unclassified += len(askable)
        return batch
    batch.classifier = "model"
    _reconcile(batch, tuple(askable), answer)
    return batch


def _reconcile(batch: Batch, notes: tuple[Note, ...], answer: Classification) -> None:
    """Match decisions to notes after decoding, and count everything that does not match.

    Reconciliation rather than validation, for the reason `consolidate.py:coerce` gives one
    module over: the grammar can be held to a shape and not to a meaning, and raising here
    would abort the boundary over a model that answered sloppily.
    """
    by_key = {d.key: d for d in answer.decisions if d.key}
    decisions: list[Decision] = []
    for note in notes:
        raw = by_key.get(note.key)
        if raw is None or raw.target not in TARGETS:
            batch.unclassified += 1
            continue
        reason = raw.reason.strip() or "no reason given"
        if raw.target == DISCARD:
            batch.discarded += 1
            continue
        target, decided_by = raw.target, "model"
        if target == SEMANTIC and (note.tainted or note.private):
            # Kept, and kept as *what happened* rather than as *what is known*. A note
            # written with a stranger's web page in context is that page's claim, and one
            # written with the user's mail open may be a detail of it; neither is something
            # to assert about the user without a human in the loop. Counted so the
            # downgrade is visible rather than inferred from a total.
            target = EPISODIC
            decided_by = "rule:tainted" if note.tainted else "rule:private"
            context = "untrusted content" if note.tainted else "the user's own private data"
            reason = (
                f"{reason} (downgraded from semantic: the note was written while "
                f"{context} was in context)"
            )
            batch.downgraded += 1
        decisions.append(
            Decision(key=note.key, target=target, reason=reason, decided_by=decided_by)
        )
    batch.decisions = tuple(decisions)


# --- the boundary ------------------------------------------------------------


async def promote_scope(
    rj: RunJournal,
    *,
    scope: str,
    session_id: UUID | str,
    boundary: str,
    cfg: Config | None = None,
    provider: Any = None,
) -> Promoted:
    """Classify one scope's working memory at its boundary and write what it keeps.

    The whole call is at a boundary by construction: there is no argument for "some of the
    notes" and no caller inside a step. Call it immediately before the scope is discarded.

    Anything an earlier boundary of this run classified and failed to write is finished
    first, so a run does not accumulate pending promotions while continuing to make new
    ones.
    """
    cfg = cfg or get_config()
    done = await complete_pending(rj.run_id, writer=rj.writer, cfg=cfg)
    notes = WorkingMemory(rj=rj, scope=scope).notes()
    if not notes:
        # No event at all, matching 7b's empty-scope discard: a boundary that saw nothing has
        # nothing to report, and one "no notes" event per turn forever is how a feed stops
        # being read.
        return done
    batch = await classify(
        notes, scope=scope, boundary=boundary, cfg=cfg, provider=provider
    )
    if batch.accounted != batch.notes:  # pragma: no cover - guards the counting, not a path
        raise RuntimeError(
            f"promotion batch for {scope} accounts for {batch.accounted} of {batch.notes} "
            "notes; a note was neither promoted nor counted as excluded"
        )
    by_key = {n.key: n for n in notes}
    keys: list[str] = []
    for decision in batch.decisions:
        note = by_key[decision.key]
        digest = text_digest(note.text)
        key = promotion_key(
            run_id=rj.run_id,
            scope=scope,
            key=note.key,
            target=decision.target,
            text_sha256=digest,
        )
        rj.emit(
            fold.CLASSIFIED,
            {
                "promotion_key": key,
                "scope": scope,
                "key": note.key,
                "target": decision.target,
                "text": note.text,
                "chars": len(note.text),
                "text_sha256": digest,
                "decided_by": decision.decided_by,
                "reason": decision.reason,
                "session_id": str(session_id),
                "tainted": note.tainted,
                "private": note.private,
                "entry_version": fold.ENTRY_VERSION,
            },
        )
        keys.append(key)
    rj.emit(fold.BATCH, batch.as_event())
    written = await complete_pending(rj.run_id, writer=rj.writer, cfg=cfg)
    return Promoted(
        batch=batch,
        classified=tuple(keys),
        committed=done.committed + written.committed,
        reused=done.reused + written.reused,
        failed={**done.failed, **written.failed},
    )


async def complete_pending(
    run_id: str, *, writer: JournalWriter, cfg: Config | None = None
) -> Promoted:
    """Write every promotion this run has classified and not committed.

    This is the resume path, and it is the same code the boundary uses - there is no second
    implementation that only runs after a crash, because a recovery path nothing exercises
    is a recovery path nobody can trust. Safe to call on a run with nothing pending, safe to
    call twice, and safe to call in a process that knows nothing about the one that
    classified the notes: everything it needs is folded out of the journal.
    """
    cfg = cfg or get_config()
    writer.flush()
    pending = fold.pending_at(writer.store, run_id)
    out = Promoted()
    if not pending:
        return out
    ledger = EffectLedger(writer)
    committed: list[str] = []
    reused: list[str] = []
    for entry in pending:
        state = await _write_one(entry, run_id=run_id, writer=writer, ledger=ledger, cfg=cfg)
        if isinstance(state, str):
            out.failed[entry["promotion_key"]] = state
            continue
        (committed if state else reused).append(entry["promotion_key"])
    out.committed, out.reused = tuple(committed), tuple(reused)
    return out


async def _write_one(
    entry: dict[str, Any],
    *,
    run_id: str,
    writer: JournalWriter,
    ledger: EffectLedger,
    cfg: Config,
) -> bool | str:
    """One durable write, ledgered. True if it landed, False if it was already there,
    the error string if it failed.

    The failure is returned rather than raised: the promotion stays pending, which is the
    state a later boundary or a later resume acts on, and one unreachable store must not
    take down the turn that was ending. Nothing is lost by waiting, because the journal
    already holds the whole note.
    """
    key = entry["promotion_key"]
    target = entry["target"]
    effect = ledger.intend(
        run_id=run_id,
        step_id=promotion_step_id(entry["scope"]),
        tool=PROMOTION_TOOL,
        effect_class=PROMOTION_EFFECT_CLASS,
        args={
            "scope": entry["scope"],
            "key": entry["key"],
            "target": target,
            "text_sha256": entry["text_sha256"],
        },
    )
    if effect.key != key:  # pragma: no cover - the two derivations are one function
        raise RuntimeError(
            f"promotion {key[:12]} would be written under ledger key {effect.key[:12]}; "
            "the idempotency key and the dedup key must be the same string"
        )
    effect.dispatched()
    try:
        ref, sequence, inserted = await _store(entry, cfg=cfg)
    except Exception as exc:
        effect.failed(str(exc), result_ref=f"promotion:{key[:12]}")
        return str(exc)
    effect.committed(result_ref=ref)
    RunJournal(writer, run_id).emit(
        fold.COMMITTED,
        {
            "promotion_key": key,
            "target": target,
            "ref": ref,
            "sequence": sequence,
            "inserted": inserted,
            "entry_version": fold.ENTRY_VERSION,
        },
    )
    return inserted


async def _store(entry: dict[str, Any], *, cfg: Config) -> tuple[str, int | None, bool]:
    """The durable write itself. `(ref, sequence, inserted)`.

    Both branches are keyed on the promotion key in the store, which is what makes them
    idempotent across processes and therefore what makes `idempotent_write` true rather than
    aspirational.
    """
    session_id = UUID(entry["session_id"])
    if entry["target"] == EPISODIC:
        event = repo_archive.RawEvent(
            kind=ARCHIVE_KIND,
            # The scope that kept the note, so "who wrote this" survives into the archive.
            # Not `subagent:<role>`: that prefix is what `recent_messages` filters on, and
            # borrowing it would make this row's exclusion from prompt history depend on a
            # clause written for a different reason.
            actor=f"working:{entry['scope']}",
            content=entry["text"],
            payload={
                # The archive's own idempotency mechanism, from 0007: one unique partial
                # index over this key, so a second attempt inserts nothing.
                "dedup_key": entry["promotion_key"],
                "promotion_key": entry["promotion_key"],
                "scope": entry["scope"],
                "note_key": entry["key"],
                "private": entry["private"],
            },
            trust="untrusted" if entry["tainted"] else "trusted",
            session_id=session_id,
        )
        event_id = await repo_archive.append_event_once(event)
        inserted = event_id is not None
        row = await _archive_row(entry["promotion_key"])
        if row is None:  # pragma: no cover - only if the 0007 index is missing
            raise RuntimeError(
                f"episodic promotion {entry['promotion_key'][:12]} is neither in the archive "
                "nor was it inserted"
            )
        return f"archive:{row['event_id']}", int(row["id"]), inserted
    candidate_id, inserted = await repo_memory.insert_candidate_once(
        promotion_key=entry["promotion_key"],
        statement=entry["text"],
        proposed_by=PROPOSED_BY,
        kind="fact",
        confidence=CONFIDENCE,
        structured={
            "scope": entry["scope"],
            "note_key": entry["key"],
            "source": "working_memory",
        },
        # A real pointer rather than an exemption. `proposed_by="promotion"` is not on the
        # gate's evidence-exempt list, so this claim has to say where it came from, and this
        # is where: the run, the scope and the note key are enough to find the
        # `promotion_classified` event that holds the sentence verbatim.
        evidence=[
            {
                "session": entry["session_id"],
                "scope": entry["scope"],
                "note": entry["key"],
                "promotion_key": entry["promotion_key"],
            }
        ],
        source_trust="untrusted" if entry["tainted"] else "trusted",
        session_id=session_id,
    )
    # No sequence: `candidate_memories` has no monotonic column. The id is uuid7 and
    # therefore time-ordered, so the high-water mark is the ref. A count would not be a
    # position and is not written here as one.
    return f"candidate:{candidate_id}", None, inserted


async def _archive_row(promotion_key: str) -> dict[str, Any] | None:
    from ..db.pool import fetch_one

    return await fetch_one(
        "SELECT id, event_id FROM raw_events WHERE payload->>'dedup_key' = %s",
        (promotion_key,),
    )


def pending_for(run_id: str, *, store: JournalStore) -> tuple[dict[str, Any], ...]:
    """What a crash left outstanding for this run. The read side, for the CLI and tests."""
    return fold.pending_at(store, run_id)


__all__ = [
    "ARCHIVE_KIND",
    "CONFIDENCE",
    "DISCARD",
    "EPISODIC",
    "PROMOTION_EFFECT_CLASS",
    "PROMOTION_TOOL",
    "PROPOSED_BY",
    "SEMANTIC",
    "TARGETS",
    "Batch",
    "Classification",
    "Decision",
    "NoteDecision",
    "Promoted",
    "classify",
    "complete_pending",
    "pending_for",
    "promote_scope",
    "promotion_key",
    "promotion_step_id",
    "text_digest",
]
