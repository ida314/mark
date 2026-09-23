"""Promotion: the crash that must cost nothing, and the duplicate that must not happen.

Session 7c. Promotion is the only path from a run's disposable scratch state into the
user's durable memory, which makes it the only place a process failure can corrupt what the
agent knows. The two ways it goes wrong are not symmetric and only one of them is loud:

* **a lost promotion** - the process died between deciding to keep a note and writing it.
  Annoying, recoverable, and visible the moment somebody looks for the note.
* **a duplicate semantic fact** - the write landed and the record of it did not, so the
  resume wrote it again. This one never throws. Two rows saying the same thing are
  indistinguishable from two independent observations of it, which is exactly the evidence
  the review gate counts, so a duplicate does not merely take up space - it makes the gate
  more confident. It degrades retrieval quietly for months. It is the failure the pass file
  names, and the reason the exit criterion is a fault-injection proof rather than a reading
  of the code.

Both orderings are injected below, in `test_a_crash_...`: the crash between the
classification and the write, and the crash between the write and the record of it. In both
the journal is then re-opened **off disk in a fresh writer**, the way a resume sees it - no
object from the dead process survives - and the recovery is the same `complete_pending` the
boundary itself calls, because a recovery path nothing else exercises is a recovery path
nobody can trust.

The rest of the file pins the decisions that make those two tests mean something: the
idempotency key does not move between steps (if it did, the resume's write would land beside
the first one under a fresh dedup key and both tests would still pass), a note the classifier
did not classify is not promoted, runtime-authored text is never promoted at all, and every
note a boundary saw is accounted for in the counts rather than quietly dropped.
"""

from __future__ import annotations

from uuid import UUID

import pytest

from agentd.agent.loop import AgentLoop, Session
from agentd.agent.working_memory import ORCHESTRATOR, WorkingMemory
from agentd.db import repo_memory
from agentd.db.pool import fetch_all, fetch_one
from agentd.journal import promotions as fold
from agentd.journal import runtime as journal_runtime
from agentd.journal.checkpoints import Checkpointer
from agentd.journal.ledger import EffectLedger
from agentd.journal.runtime import RunJournal
from agentd.journal.store import JournalStore
from agentd.journal.writer import JournalWriter
from agentd.llm.fake import FakeProvider
from agentd.llm.roles import set_provider
from agentd.memory import promotion
from agentd.memory.promotion import Classification, NoteDecision
from agentd.tools import builtin_working
from agentd.tools.registry import Registry

RUN = "run-promote"


class Crash(BaseException):
    """A process disappearing, not an exception a caller could have handled.

    `BaseException` on purpose: `memory/promotion.py` catches `Exception` around the durable
    write and turns it into a promotion that stays pending, which is the *handled* failure.
    A crash is the unhandled one, and a test that injected an ordinary exception would be
    exercising the error path instead of the recovery path.
    """


@pytest.fixture
def journal(tmp_path):
    """A journal file this test owns, opened and closed like a process would."""
    path = tmp_path / "promotion.db"
    writer = JournalWriter(JournalStore(path))
    yield writer, path
    writer.close()


def reopen(path) -> JournalWriter:
    """The journal as the next process sees it: off disk, with nothing carried over."""
    return JournalWriter(JournalStore(path))


def classifier(**by_key: str) -> FakeProvider:
    """A classifier that answers `{key: target}` and nothing else."""
    return FakeProvider(
        json_results=[
            Classification(
                decisions=[
                    NoteDecision(key=key, target=target, reason="because")
                    for key, target in by_key.items()
                ]
            )
        ]
    )


async def note(writer: JournalWriter, key: str, text: str, **flags) -> WorkingMemory:
    handle = WorkingMemory(rj=RunJournal(writer, RUN), scope=ORCHESTRATOR)
    handle.note(key, text, **flags)
    return handle


async def candidates() -> list[dict]:
    return await fetch_all("SELECT * FROM candidate_memories ORDER BY created_at")


async def archived() -> list[dict]:
    return await fetch_all(
        "SELECT * FROM raw_events WHERE kind = %s ORDER BY id", (promotion.ARCHIVE_KIND,)
    )


def events(writer: JournalWriter, *types: str) -> list:
    writer.flush()
    return [e for e in writer.store.read(RUN) if not types or e.type in types]


# --- the exit criterion: kill between classification and the write -------------


async def test_a_crash_between_classifying_a_note_and_writing_it_loses_no_promotion(
    cfg, journal, monkeypatch
):
    """Ordering one. The decision is on disk; the durable write never happened.

    This is the ordering the protocol is *designed* around - journal first, store second -
    so what it proves is that the design is wired up: the classified record survives the
    process, it carries the note in full rather than a pointer into a bucket that has since
    been tombstoned, and the resume can act on it with nothing else to go on.
    """
    writer, path = journal
    session = await Session.create("test")
    await note(writer, "rent", "Dylan pays rent on the first of the month")
    set_provider(classifier(rent="semantic"))

    real_store = promotion._store

    def crash(*_a, **_k):
        raise Crash("killed between the classification and the write")

    monkeypatch.setattr(promotion, "_store", crash)
    with pytest.raises(Crash):
        await promotion.promote_scope(
            RunJournal(writer, RUN), scope=ORCHESTRATOR,
            session_id=session.id, boundary="run", cfg=cfg,
        )
    writer.close()

    assert await candidates() == [], "the store was never reached"
    monkeypatch.setattr(promotion, "_store", real_store)
    survivor = reopen(path)
    try:
        pending = fold.pending_at(survivor.store, RUN)
        assert len(pending) == 1, "the decision did not survive the process that made it"
        assert pending[0]["text"] == "Dylan pays rent on the first of the month"

        done = await promotion.complete_pending(RUN, writer=survivor, cfg=cfg)
        assert len(done.committed) == 1
        rows = await candidates()
        assert len(rows) == 1, "the promotion was lost"
        assert rows[0]["statement"] == "Dylan pays rent on the first of the month"
        assert fold.pending_at(survivor.store, RUN) == ()
    finally:
        survivor.close()


async def test_a_crash_after_the_write_and_before_its_record_makes_no_second_fact(
    cfg, journal, monkeypatch
):
    """Ordering two, and the expensive one.

    The candidate is in postgres and the journal never heard about it, so the resume has no
    way to know the write happened and correctly tries again. Nothing in the journal, the
    ledger or the runtime can stop that second attempt from being made - the process that
    knew died. What stops it from *landing* is the unique index on the promotion key, which
    is why the key has to be derivable from the journal alone and has to be the same string
    in both processes.

    One row afterwards, and the second attempt says so out loud: `inserted=False`.
    """
    writer, path = journal
    session = await Session.create("test")
    await note(writer, "rent", "Dylan pays rent on the first of the month")
    set_provider(classifier(rent="semantic"))

    real_store = promotion._store

    async def write_then_die(entry, **kwargs):
        await real_store(entry, **kwargs)
        raise Crash("killed after the write and before the record of it")

    monkeypatch.setattr(promotion, "_store", write_then_die)
    with pytest.raises(Crash):
        await promotion.promote_scope(
            RunJournal(writer, RUN), scope=ORCHESTRATOR,
            session_id=session.id, boundary="run", cfg=cfg,
        )
    writer.close()

    assert len(await candidates()) == 1, "the write did land before the crash"
    monkeypatch.setattr(promotion, "_store", real_store)
    survivor = reopen(path)
    try:
        assert len(fold.pending_at(survivor.store, RUN)) == 1, (
            "the journal cannot know the write happened, so it must still look pending"
        )
        done = await promotion.complete_pending(RUN, writer=survivor, cfg=cfg)
        assert done.committed == ()
        assert len(done.reused) == 1, "the second attempt must recognise the first"
        rows = await candidates()
        assert len(rows) == 1, "a duplicate semantic fact - the failure this pass exists for"
        committed = events(survivor, fold.COMMITTED)
        assert [e.payload["inserted"] for e in committed] == [False]
    finally:
        survivor.close()


async def test_the_episodic_half_survives_the_same_crash_without_duplicating(
    cfg, journal, monkeypatch
):
    """The same proof for the other branch, which uses a different store and a different
    dedup mechanism (`raw_events_connector_dedup_idx`, from 0007). One of the two being
    idempotent is not the property; both are."""
    writer, path = journal
    session = await Session.create("test")
    await note(writer, "outcome", "the lease PDF turned out to be the 2024 one")
    set_provider(classifier(outcome="episodic"))

    real_store = promotion._store

    async def write_then_die(entry, **kwargs):
        await real_store(entry, **kwargs)
        raise Crash("killed after the archive row and before the record of it")

    monkeypatch.setattr(promotion, "_store", write_then_die)
    with pytest.raises(Crash):
        await promotion.promote_scope(
            RunJournal(writer, RUN), scope=ORCHESTRATOR,
            session_id=session.id, boundary="run", cfg=cfg,
        )
    writer.close()
    assert len(await archived()) == 1

    monkeypatch.setattr(promotion, "_store", real_store)
    survivor = reopen(path)
    try:
        done = await promotion.complete_pending(RUN, writer=survivor, cfg=cfg)
        assert len(done.reused) == 1
        rows = await archived()
        assert len(rows) == 1
        # The episodic branch is the one with a real position: `raw_events.id` is a bigint
        # identity column, which is what the watermark reports for it.
        mark = fold.watermark_at(survivor.store, RUN)
        assert mark["episodic"]["last_sequence"] == rows[0]["id"]
        assert mark["semantic"]["last_sequence"] is None
    finally:
        survivor.close()


async def test_the_promotion_key_does_not_move_when_the_resume_runs_in_a_later_step(cfg):
    """The hazard 4c recorded against `hash(run_id, step_id, tool, args)`, and the reason
    the two crash tests above are not vacuous.

    `step_id` is in the key shape, so the same logical call made again in a later step does
    *not* collide with the row already open. For promotion that is the whole bug: a resume
    runs in a different step from the boundary that classified the note, and the default
    derivation would hand it a fresh key, a fresh ledger row and - because the key is also
    the dedup key - a fresh row in the store. The duplicate would arrive through the
    mechanism meant to prevent it, and every other test here would still be green.

    So the step is the boundary. There is no step counter in it at all.
    """
    assert promotion.promotion_step_id(ORCHESTRATOR) == "promote:orchestrator"
    args = dict(
        run_id=RUN, scope=ORCHESTRATOR, key="rent", target="semantic",
        text_sha256=promotion.text_digest("Dylan pays rent on the first"),
    )
    assert promotion.promotion_key(**args) == promotion.promotion_key(**args)
    assert "step" not in promotion.promotion_step_id(ORCHESTRATOR).split(":")[1]
    # Two scopes at two boundaries are two promotions; one scope is one.
    assert promotion.promotion_key(**{**args, "scope": "worker-1"}) != promotion.promotion_key(
        **args
    )
    assert promotion.promotion_key(**{**args, "target": "episodic"}) != promotion.promotion_key(
        **args
    )


async def test_the_ledger_records_the_write_under_that_same_key(cfg, journal):
    """Memory writes are effects, with the Pass 3 machinery rather than a parallel path.

    The ledger does not prevent the duplicate and is not asked to - it says so itself. What
    it has to do is name the write under the key the store dedups on, so that reconciling an
    uncertain promotion and looking the row up are the same question.
    """
    writer, _path = journal
    session = await Session.create("test")
    await note(writer, "rent", "Dylan pays rent on the first")
    set_provider(classifier(rent="semantic"))
    await promotion.promote_scope(
        RunJournal(writer, RUN), scope=ORCHESTRATOR,
        session_id=session.id, boundary="run", cfg=cfg,
    )
    rows = [r for r in EffectLedger(writer).entries(RUN) if r.tool == promotion.PROMOTION_TOOL]
    assert len(rows) == 1
    assert rows[0].effect_class == "idempotent_write"
    assert rows[0].state == "committed"
    classified = events(writer, fold.CLASSIFIED)[0]
    assert rows[0].idempotency_key == classified.payload["promotion_key"]
    candidate = (await candidates())[0]
    assert candidate["structured"]["promotion_key"] == rows[0].idempotency_key


# --- what may and may not be promoted -----------------------------------------


async def test_runtime_authored_text_is_never_promoted_as_fact(cfg, journal):
    """The rule carried in from the Pass 4/5 boundary: "messages with synthetic=True are
    never summarized or promoted as fact".

    7a found that the existing promotion path satisfies this *structurally* - it reads
    `raw_events`, which has no synthetic column - and warned that the protection is an
    accident of the input. This path's input is a note an agent wrote, so the accident does
    not carry over and the rule is a check: text beginning with one of
    `observations.RUNTIME_MARKERS` is refused before the classifier is even asked.

    Counted, not filtered quietly. Pass 5's handoff generator counts its exclusions into the
    stored object for the same reason: "none were excluded" and "nobody looked" have to be
    different observations.
    """
    from agentd.agent.observations import RUNTIME_PREFIX

    writer, _path = journal
    session = await Session.create("test")
    await note(writer, "resumed", f"{RUNTIME_PREFIX} the call was interrupted by a restart")
    await note(writer, "real", "Dylan's landlord is called Marco")
    set_provider(classifier(resumed="semantic", real="semantic"))

    out = await promotion.promote_scope(
        RunJournal(writer, RUN), scope=ORCHESTRATOR,
        session_id=session.id, boundary="run", cfg=cfg,
    )
    assert out.batch.synthetic == 1
    assert [d.key for d in out.batch.decisions] == ["real"]
    statements = [r["statement"] for r in await candidates()]
    assert statements == ["Dylan's landlord is called Marco"]
    batch = events(writer, fold.BATCH)[0].payload
    assert batch["synthetic"] == 1 and batch["semantic"] == 1


async def test_a_note_carrying_a_secret_reaches_neither_store(cfg, journal):
    """`contains_secret` runs at review time today, which is after a candidate has been
    written and after it is renderable as a pending claim (7a, open question 5). Promotion is
    the one path where the screen can run *before* the write, so it does - and a note that
    trips it is not promoted to either bucket, because the archive is no safer a home for a
    live credential than the fact table is."""
    writer, _path = journal
    session = await Session.create("test")
    await note(writer, "creds", "the api_key = sk-abcdefghijklmnopqrstuvwx for the portal")
    set_provider(classifier(creds="semantic"))

    out = await promotion.promote_scope(
        RunJournal(writer, RUN), scope=ORCHESTRATOR,
        session_id=session.id, boundary="run", cfg=cfg,
    )
    assert out.batch.secret == 1 and out.batch.decisions == ()
    assert await candidates() == []
    assert await archived() == []


async def test_a_note_written_from_untrusted_content_never_becomes_a_semantic_candidate(
    cfg, journal
):
    """Untrusted content taints a turn, and a note written in one is the web page's claim
    rather than the user's. It is still worth keeping - as *what happened*, not as *what is
    known* - so the classifier's `semantic` is downgraded to `episodic` rather than dropped,
    and the downgrade is counted so it is visible rather than inferred from a total."""
    writer, _path = journal
    session = await Session.create("test")
    await note(writer, "claim", "the page says the deadline moved to June", tainted=True)
    set_provider(classifier(claim="semantic"))

    out = await promotion.promote_scope(
        RunJournal(writer, RUN), scope=ORCHESTRATOR,
        session_id=session.id, boundary="run", cfg=cfg,
    )
    assert out.batch.downgraded == 1
    assert [d.target for d in out.batch.decisions] == ["episodic"]
    assert await candidates() == []
    rows = await archived()
    assert len(rows) == 1 and rows[0]["trust"] == "untrusted"


async def test_a_note_written_while_private_data_was_in_context_is_downgraded_too(
    cfg, journal
):
    """The other flag 7b recorded on a note and left without a consumer. It is a different
    claim from `tainted` - "the user's own data was in context" rather than "somebody else's
    words were" - and it shuts a different door, but for promotion the answer is the same
    conservative one: keep what happened, do not assert a new standing fact about the user
    from material nobody has reviewed."""
    writer, _path = journal
    session = await Session.create("test")
    await note(writer, "bill", "the electricity bill is in Dylan's name", private=True)
    set_provider(classifier(bill="semantic"))

    out = await promotion.promote_scope(
        RunJournal(writer, RUN), scope=ORCHESTRATOR,
        session_id=session.id, boundary="run", cfg=cfg,
    )
    assert out.batch.downgraded == 1
    assert [d.decided_by for d in out.batch.decisions] == ["rule:private"]
    assert await candidates() == []
    assert len(await archived()) == 1


async def test_a_note_the_classifier_said_nothing_about_is_not_promoted(cfg, journal):
    """No default, and no cross-field validator that would have turned this into an
    `LLMError` and an aborted turn. The decoded answer is reconciled against the notes
    afterwards, and a note with no usable decision is counted as unclassified and left where
    it is. "The classifier did not say" must not become "keep it"."""
    writer, _path = journal
    session = await Session.create("test")
    await note(writer, "a", "something the model forgot to answer about")
    await note(writer, "b", "Dylan's landlord is called Marco")
    set_provider(
        FakeProvider(
            json_results=[
                Classification(
                    decisions=[
                        NoteDecision(key="b", target="semantic", reason="durable"),
                        NoteDecision(key="ghost", target="semantic", reason="not a note"),
                    ]
                )
            ]
        )
    )
    out = await promotion.promote_scope(
        RunJournal(writer, RUN), scope=ORCHESTRATOR,
        session_id=session.id, boundary="run", cfg=cfg,
    )
    assert out.batch.unclassified == 1
    assert [r["statement"] for r in await candidates()] == ["Dylan's landlord is called Marco"]


async def test_a_target_that_is_not_a_bucket_is_unclassified_rather_than_guessed(
    cfg, journal
):
    """An unknown target is not a fourth bucket and not a nudge towards the nearest one."""
    writer, _path = journal
    session = await Session.create("test")
    await note(writer, "a", "Dylan's landlord is called Marco")
    set_provider(classifier(a="probably-semantic"))
    out = await promotion.promote_scope(
        RunJournal(writer, RUN), scope=ORCHESTRATOR,
        session_id=session.id, boundary="run", cfg=cfg,
    )
    assert out.batch.unclassified == 1 and out.batch.decisions == ()
    assert await candidates() == []


async def test_a_classifier_that_will_not_answer_promotes_nothing_and_says_why(
    cfg, journal
):
    """`complete_json` gets one repair attempt and then raises, and at a turn-end boundary
    an exception is an aborted turn. So the failure is caught, every note is counted
    unclassified, and the message goes into `promotion_batch` - recorded rather than
    swallowed, and emphatically not a fallback to whichever bucket looks likely."""

    class Broken:
        async def complete_json(self, *_a, **_k):
            raise RuntimeError("the model endpoint hung for 600s")

    writer, _path = journal
    session = await Session.create("test")
    await note(writer, "a", "Dylan's landlord is called Marco")
    set_provider(Broken())
    out = await promotion.promote_scope(
        RunJournal(writer, RUN), scope=ORCHESTRATOR,
        session_id=session.id, boundary="run", cfg=cfg,
    )
    assert out.batch.classifier == "failed"
    assert "600s" in out.batch.error
    assert out.batch.unclassified == 1
    assert await candidates() == []
    assert events(writer, fold.CLASSIFIED) == []


async def test_every_note_a_boundary_saw_is_accounted_for_in_the_counts(cfg, journal):
    """`notes == synthetic + secret + discarded + unclassified + episodic + semantic`.

    The identity is what makes "nothing was quietly dropped" a check rather than a claim. A
    filter added later that forgets to count itself breaks this, which is the point: the way
    a note goes missing here is not an exception, it is a `continue`.
    """
    from agentd.agent.observations import RUNTIME_PREFIX

    writer, _path = journal
    session = await Session.create("test")
    await note(writer, "synthetic", f"{RUNTIME_PREFIX} a restart happened")
    await note(writer, "secret", "password: hunter2hunter2")
    await note(writer, "scratch", "checked page 1, nothing there")
    await note(writer, "silent", "the model will not answer about this one")
    await note(writer, "happened", "the lease turned out to be the 2024 one")
    await note(writer, "known", "Dylan's landlord is called Marco")
    set_provider(
        classifier(scratch="discard", happened="episodic", known="semantic")
    )
    out = await promotion.promote_scope(
        RunJournal(writer, RUN), scope=ORCHESTRATOR,
        session_id=session.id, boundary="run", cfg=cfg,
    )
    batch = out.batch
    assert batch.notes == 6
    assert (batch.synthetic, batch.secret, batch.discarded, batch.unclassified) == (1, 1, 1, 1)
    assert (batch.episodic, batch.semantic) == (1, 1)
    assert batch.accounted == batch.notes
    payload = events(writer, fold.BATCH)[0].payload
    assert sum(
        payload[name]
        for name in ("synthetic", "secret", "discarded", "unclassified", "episodic", "semantic")
    ) == payload["notes"]


async def test_a_promoted_candidate_is_never_proposed_as_the_user(cfg, journal):
    """`review.process_candidate` gives `proposed_by="user"` a lower confidence floor *and*
    an exemption from the evidence requirement. A note the model wrote arriving under that
    name would be the model's own inference wearing the user's authority - the same
    laundering `memory_remember` refuses one module over.

    It is also not accepted unattended: the confidence sits below the gate's acceptance
    threshold and above its floor, so a scratch note becomes something a judge or a human
    looks at, not something that is simply true now.
    """
    from agentd.memory.review import ACCEPT_CONFIDENCE, MIN_CONFIDENCE

    writer, _path = journal
    session = await Session.create("test")
    await note(writer, "known", "Dylan's landlord is called Marco")
    set_provider(classifier(known="semantic"))
    await promotion.promote_scope(
        RunJournal(writer, RUN), scope=ORCHESTRATOR,
        session_id=session.id, boundary="run", cfg=cfg,
    )
    row = (await candidates())[0]
    assert row["proposed_by"] == "promotion" != "user"
    assert MIN_CONFIDENCE < row["confidence"] < ACCEPT_CONFIDENCE
    assert row["status"] == "pending", "review.py stays the only writer of facts"
    # Not exempt from the evidence requirement, so it carries real evidence: where the note
    # came from, precisely enough to find the journal event holding the sentence verbatim.
    assert row["evidence"][0]["note"] == "known"
    assert row["evidence"][0]["promotion_key"]


async def test_nothing_is_written_for_a_scope_that_kept_no_notes(cfg, journal):
    """A boundary that saw nothing says nothing. One "no notes here" event per turn forever
    is how a feed stops being read - the same reasoning 7b used for the empty discard."""
    writer, _path = journal
    session = await Session.create("test")
    set_provider(FakeProvider())
    out = await promotion.promote_scope(
        RunJournal(writer, RUN), scope=ORCHESTRATOR,
        session_id=session.id, boundary="run", cfg=cfg,
    )
    assert out.batch is None
    assert events(writer, fold.BATCH, fold.CLASSIFIED, fold.COMMITTED) == []
    assert fold.watermark_at(writer.store, RUN) is None


# --- the checkpoint slots ------------------------------------------------------


async def test_a_checkpoint_carries_the_pending_promotions_and_the_watermark(
    cfg, journal, monkeypatch
):
    """The two slots Pass 4 cut and left inert, filled the way 6c filled `worker_results[]`.

    Both come out of the same fold the resume acts on, so the snapshot cannot claim a
    promotion is outstanding that the journal says landed, or the other way round. A
    checkpoint may lag the journal; it may never disagree with it.
    """
    writer, path = journal
    cfg.checkpoints.enabled = True
    session = await Session.create("test")
    RunJournal(writer, RUN).emit(
        "agent_started",
        {
            "session_id": str(session.id), "turn_id": RUN, "role": "main", "actor": "main",
            "origin": "test", "channel": "test", "autonomy": "assist", "model": "fake",
            "max_steps": 4, "input_chars": 0, "input_preview": "",
            "parent_turn_id": None,
        },
    )
    await note(writer, "known", "Dylan's landlord is called Marco")
    set_provider(classifier(known="semantic"))

    def crash(*_a, **_k):
        raise Crash("killed before the write")

    monkeypatch.setattr(promotion, "_store", crash)
    with pytest.raises(Crash):
        await promotion.promote_scope(
            RunJournal(writer, RUN), scope=ORCHESTRATOR,
            session_id=session.id, boundary="run", cfg=cfg,
        )

    snapshot = Checkpointer(writer, cfg=cfg).write(RUN, trigger="manual")
    assert len(snapshot.pending_promotions) == 1
    entry = snapshot.pending_promotions[0]
    assert entry["key"] == "known" and entry["target"] == "semantic"
    # The digest, not the body: the journal already holds the sentence and a snapshot is an
    # acceleration structure, not a second copy.
    assert "text" not in entry and entry["text_sha256"]
    assert snapshot.memory_watermark["pending"] == 1
    assert snapshot.memory_watermark["semantic"]["committed"] == 0

    stored = Checkpointer(writer, cfg=cfg).get(snapshot.checkpoint_id)
    assert stored.pending_promotions == snapshot.pending_promotions
    assert stored.memory_watermark == snapshot.memory_watermark
    announced = events(writer, "checkpoint_written")[0].payload["memory_watermark"]
    assert announced == snapshot.memory_watermark


async def test_a_run_that_promoted_nothing_records_a_null_watermark(cfg, journal):
    """Null and `{}` are different statements, and 4a cut the field for that distinction.

    A run with no promotions has no positions to report. An object of zeros would read as a
    measurement - two memory positions observed and found to be nothing - which is this
    codebase's characteristic bug with the lights on.
    """
    writer, _path = journal
    cfg.checkpoints.enabled = True
    session = await Session.create("test")
    RunJournal(writer, RUN).emit(
        "agent_started",
        {
            "session_id": str(session.id), "turn_id": RUN, "role": "main", "actor": "main",
            "origin": "test", "channel": "test", "autonomy": "assist", "model": "fake",
            "max_steps": 4, "input_chars": 0, "input_preview": "",
            "parent_turn_id": None,
        },
    )
    snapshot = Checkpointer(writer, cfg=cfg).write(RUN, trigger="manual")
    assert snapshot.pending_promotions == ()
    assert snapshot.memory_watermark is None


# --- the boundaries, through the live path -------------------------------------


async def test_a_turn_promotes_at_its_own_end_and_not_in_the_middle_of_itself(cfg):
    """The pass file's second *Must not*, read off the journal of a real turn.

    The turn writes two notes in two separate steps. If promotion were opportunistic, the
    first note would be classified while the second step was still running. There is exactly
    one batch, and it sits after the last tool call and before `agent_finished`.
    """
    session = await Session.create("test")
    provider = FakeProvider(
        turns=[
            [("working_memory_note", {"key": "one", "note": "the lease is the 2024 one"})],
            [("working_memory_note", {"key": "two", "note": "Dylan's landlord is Marco"})],
            "done",
        ],
        json_results=[
            Classification(
                decisions=[
                    NoteDecision(key="one", target="episodic", reason="what happened"),
                    NoteDecision(key="two", target="semantic", reason="durable"),
                ]
            )
        ],
    )
    registry = Registry()
    registry.add(*builtin_working.TOOLS)
    loop = AgentLoop(cfg=cfg, registry=registry, provider=provider)
    async for _ in loop.run_turn(session, "look into the lease"):
        pass

    writer = journal_runtime.get_writer(cfg)
    writer.flush()
    run = [e for e in writer.store.read_all() if e.type in (
        fold.BATCH, fold.CLASSIFIED, fold.COMMITTED, "tool_finished", "agent_finished",
        "working_memory_discarded",
    )]
    kinds = [e.type for e in run]
    assert kinds.count(fold.BATCH) == 1, "one batch per boundary, never one per note"
    batch_at = kinds.index(fold.BATCH)
    assert kinds.index("agent_finished") > batch_at
    assert max(i for i, k in enumerate(kinds) if k == "tool_finished") < batch_at
    # And after the promotion, the scope is emptied - in that order, or the boundary would
    # be classifying a bucket the previous statement threw away.
    assert kinds.index("working_memory_discarded") > batch_at
    assert [r["statement"] for r in await candidates()] == ["Dylan's landlord is Marco"]
    assert len(await archived()) == 1


async def test_a_workers_notes_are_promoted_once_at_its_own_task_boundary(cfg):
    """A worker's task boundary is `worker_finished`, not its caller's turn end.

    The worker's own `run_turn` is skipped (`agent/loop.py` checks `worker_id`), so there is
    exactly one batch for the worker's scope. Two would classify one note twice, and the
    second would be reading a scope its caller was about to discard.
    """
    from agentd.agent.delegation import TaskSpec
    from agentd.agent.results import WorkerReport
    from agentd.agent.subagents import SubagentSpec, run_subagent
    from agentd.policy.approvals import AutoApprover
    from agentd.tools.registry import build_registry

    session = await Session.create("test")
    provider = FakeProvider(
        turns=[
            [("working_memory_note", {"key": "found", "note": "Dylan's landlord is Marco"})],
            "reported",
        ],
        json_results=[
            WorkerReport(status="completed", answer="done"),
            Classification(
                decisions=[NoteDecision(key="found", target="semantic", reason="durable")]
            ),
        ],
    )
    await run_subagent(
    SubagentSpec(
        name="researcher", prompt="be useful",
        tool_names=["working_memory_note"], max_steps=4,
    ),
    TaskSpec("researcher", "find the landlord"),
    parent_session_id=session.id, parent_turn_id=session.id,
    parent_autonomy="assist", approver=AutoApprover(True),
    registry=build_registry(), cfg=cfg, provider=provider, parent_run_id=RUN,
    )
    writer = journal_runtime.get_writer(cfg)
    writer.flush()
    batches = [e for e in writer.store.read(RUN) if e.type == fold.BATCH]
    assert len(batches) == 1
    assert batches[0].payload["boundary"] == "task"
    assert batches[0].payload["scope"] != ORCHESTRATOR
    rows = await candidates()
    assert [r["statement"] for r in rows] == ["Dylan's landlord is Marco"]
    assert rows[0]["structured"]["scope"] == batches[0].payload["scope"]


async def test_the_episodic_row_is_consolidation_input_and_not_prompt_history(cfg, journal):
    """Where an episodic promotion actually lands, and the two queries that decide what that
    means. `events_for_session` sees it, so it reaches the consolidator; `recent_messages`
    does not, so it is never replayed into anybody's conversation as something that was
    said."""
    from agentd.db import repo_archive

    writer, _path = journal
    session = await Session.create("test")
    await note(writer, "outcome", "the lease turned out to be the 2024 one")
    set_provider(classifier(outcome="episodic"))
    await promotion.promote_scope(
    RunJournal(writer, RUN), scope=ORCHESTRATOR,
    session_id=session.id, boundary="run", cfg=cfg,
    )
    for_consolidation = await repo_archive.events_for_session(session.id)
    assert [e["kind"] for e in for_consolidation] == [promotion.ARCHIVE_KIND]
    assert await repo_archive.recent_messages(session.id) == []


async def test_the_candidate_and_the_archive_row_carry_the_session_they_came_from(
    cfg, journal
):
    """An archive row with no session is invisible to consolidation - it is in no session's
    event range - and a candidate with no session cannot be traced back to the conversation
    that produced it. Both are plausible NULLs that nothing would report, so the session id
    is required on `promotion_classified` rather than reconstructed at write time from a
    process that may no longer exist."""
    writer, _path = journal
    session = await Session.create("test")
    await note(writer, "known", "Dylan's landlord is Marco")
    await note(writer, "happened", "the lease was the 2024 one")
    set_provider(classifier(known="semantic", happened="episodic"))
    await promotion.promote_scope(
    RunJournal(writer, RUN), scope=ORCHESTRATOR,
    session_id=session.id, boundary="run", cfg=cfg,
    )
    assert (await candidates())[0]["session_id"] == session.id
    assert (await archived())[0]["session_id"] == session.id
    assert all(
    e.payload["session_id"] == str(session.id) for e in events(writer, fold.CLASSIFIED)
    )


async def test_a_second_run_of_the_same_boundary_writes_nothing_new(cfg, journal):
    """Idempotent end to end, not only after a crash: calling the boundary twice is the
    shape a retry has, and it must converge rather than accumulate."""
    writer, _path = journal
    session = await Session.create("test")
    await note(writer, "known", "Dylan's landlord is Marco")
    set_provider(
    FakeProvider(
        json_results=[
            Classification(
                decisions=[NoteDecision(key="known", target="semantic", reason="durable")]
            ),
            Classification(
                decisions=[NoteDecision(key="known", target="semantic", reason="durable")]
            ),
        ]
    )
    )
    rj = RunJournal(writer, RUN)
    first = await promotion.promote_scope(
        rj, scope=ORCHESTRATOR, session_id=session.id, boundary="run", cfg=cfg
    )
    second = await promotion.promote_scope(
        rj, scope=ORCHESTRATOR, session_id=session.id, boundary="run", cfg=cfg
    )
    assert len(first.committed) == 1 and second.committed == ()
    assert len(await candidates()) == 1


async def test_insert_candidate_once_is_what_refuses_the_duplicate(cfg):
    """The guard, on its own, without the runtime around it.

    Worth a test of its own because everything above depends on it and because the failure
    mode - two rows instead of one - is silent. The second call returns the first row's id
    and `inserted=False`; it does not raise, and it does not overwrite, because the review
    gate may already have decided about the row and a crash must not be able to walk that
    decision back to `pending`.
    """
    first, inserted_first = await repo_memory.insert_candidate_once(
        promotion_key="pk-1", statement="one", proposed_by="promotion"
    )
    second, inserted_second = await repo_memory.insert_candidate_once(
        promotion_key="pk-1", statement="one, said differently", proposed_by="promotion"
    )
    assert inserted_first is True and inserted_second is False
    assert first == second
    rows = await candidates()
    assert len(rows) == 1 and rows[0]["statement"] == "one"
    other, inserted_other = await repo_memory.insert_candidate_once(
        promotion_key="pk-2", statement="two", proposed_by="promotion"
    )
    assert inserted_other is True and other != first


async def test_a_candidate_without_a_promotion_key_is_not_constrained(cfg):
    """The index is partial, and it has to be: the consolidator proposes candidates with no
    promotion key at all, and two of those are two proposals rather than a conflict."""
    await repo_memory.insert_candidate(statement="from the consolidator", proposed_by="x")
    await repo_memory.insert_candidate(statement="from the consolidator", proposed_by="x")
    assert len(await candidates()) == 2


async def test_the_watermark_is_positions_and_never_a_count_used_as_one(cfg, journal):
    """7a's open question 3: there is no write position on any memory table to build a
    watermark out of, and a count is not a position.

    So the watermark says what each store can actually support. `raw_events` has a bigint
    identity column, so episodic gets a real sequence. `candidate_memories` has none; its
    ids are uuid7 and therefore time-ordered, so semantic gets a high-water *ref* and an
    explicit null sequence rather than an invented number. `committed` sits beside the
    position, never instead of it.
    """
    writer, _path = journal
    session = await Session.create("test")
    await note(writer, "known", "Dylan's landlord is Marco")
    await note(writer, "happened", "the lease was the 2024 one")
    set_provider(classifier(known="semantic", happened="episodic"))
    await promotion.promote_scope(
        RunJournal(writer, RUN), scope=ORCHESTRATOR,
        session_id=session.id, boundary="run", cfg=cfg,
    )
    mark = fold.watermark_at(writer.store, RUN)
    assert mark["pending"] == 0
    assert mark["semantic"]["committed"] == 1
    assert mark["semantic"]["last_sequence"] is None
    assert mark["semantic"]["last_ref"].startswith("candidate:")
    row = await fetch_one("SELECT id FROM raw_events WHERE kind = %s", (promotion.ARCHIVE_KIND,))
    assert mark["episodic"]["last_sequence"] == row["id"]
    assert mark["last_seq"] > 0
    # The uuid7 in the semantic ref is the high-water mark, so it has to be a real id.
    UUID(mark["semantic"]["last_ref"].split(":", 1)[1])


async def test_a_workers_turn_never_promotes_its_callers_working_memory(cfg):
    """The guard in `AgentLoop._promote`, and the reason it is not cosmetic.

    The run boundary always promotes the *orchestrator's* scope, because a worker's own
    scope is promoted at its task boundary in `agent/subagents.py`. A worker's turn unwinds
    through the same `run_turn`, so without `if rj.worker_id is not None: return` a worker
    would reach into its caller's bucket, classify notes it is not allowed to read, and
    write them - **while the caller's turn is still running**, which is the mid-task
    promotion the pass file forbids, arriving through a scope crossing 7b spent a session
    closing.

    Found by mutation: deleting that guard broke no test, because the scope it would have
    promoted is empty in every other test here. This one puts a note in it.
    """
    from agentd.agent.delegation import TaskSpec
    from agentd.agent.results import WorkerReport
    from agentd.agent.subagents import SubagentSpec, run_subagent
    from agentd.policy.approvals import AutoApprover
    from agentd.tools.registry import build_registry

    # The process writer, not the fixture's: `run_subagent` resolves its own through
    # `get_writer(cfg)`, and a worker has to be writing into the same file as the caller
    # whose scope this test is about.
    writer = journal_runtime.get_writer(cfg)
    session = await Session.create("test")
    await note(writer, "callers", "Dylan's landlord is Marco")
    await run_subagent(
        SubagentSpec(name="researcher", prompt="be useful", tool_names=[], max_steps=2),
        TaskSpec("researcher", "do something else entirely"),
        parent_session_id=session.id, parent_turn_id=session.id,
        parent_autonomy="assist", approver=AutoApprover(True),
        registry=build_registry(), cfg=cfg,
        provider=FakeProvider(
            turns=["reported"],
            json_results=[
                WorkerReport(status="completed", answer="done"),
                # If the guard were gone this is the answer that would promote the
                # caller's note, so the script has to be willing to.
                Classification(
                    decisions=[
                        NoteDecision(key="callers", target="semantic", reason="durable")
                    ]
                ),
            ],
        ),
        parent_run_id=RUN,
    )

    assert events(writer, fold.BATCH) == [], "a worker classified its caller's scope"
    assert await candidates() == []
    # And the note is still the caller's, untouched, waiting for its own boundary.
    held = WorkingMemory(rj=RunJournal(writer, RUN), scope=ORCHESTRATOR).notes()
    assert [n.key for n in held] == ["callers"]
