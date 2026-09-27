"""Work that was already done, and the two ways a cache of it can be worse than no cache.

Session 6c. A worker is the most expensive thing this runtime does - minutes, tokens, and a
tool surface that can write files - so a resume that re-delegates three finished workers
pays for them twice. The cache exists to stop that, and the pass's exit criterion is
literally a measurement: kill a run after three completed workers, resume, and none of the
three re-run.

The failures worth testing are not "the cache missed". A miss costs a worker, which is what
the runtime did before this session existed. These are the expensive ones:

* **a false hit.** Some other delegation's answer, handed back as this one's, with no worker
  and no record that anything was substituted. `result_key` is the whole defence.
* **reuse across runs**, which is the pass's *Must not*: staleness semantics for repository
  and web state are not worked out, and a key that outlived its run would quietly claim they
  were. A fork is the same question one step further out.
* **an `uncertain` result served from a cache**, which decides "should this be re-run?"
  silently, for ever, in favour of never - in the one case where re-running may be right.
* **laundering.** A result earned while the turn held the user's private data is still
  untrusted the second time it is served, and a worker's candidate memories are proposed
  once, not once per cache hit.

The measured reuse rate is in `test_a_resumed_run_does_not_re_run_the_workers_it_finished`,
which counts it rather than asserting a boolean: 3 of 3, with zero `worker_created` events
after the crash.
"""

from __future__ import annotations

import pytest

from agentd.agent import result_cache
from agentd.agent.delegation import TaskSpec
from agentd.agent.loop import Session
from agentd.agent.results import WorkerReport, WorkerResult
from agentd.agent.subagents import SubagentSpec, run_subagent
from agentd.db import repo_memory
from agentd.journal import fork as F
from agentd.journal import resume as R
from agentd.journal import worker_results as wr
from agentd.journal.checkpoints import Checkpointer
from agentd.journal.runtime import RunJournal
from agentd.journal.store import JournalStore
from agentd.journal.writer import JournalWriter, is_synchronous
from agentd.llm.fake import FakeProvider
from agentd.policy.approvals import AutoApprover
from agentd.tools.registry import build_registry

RUN = "run-1"


@pytest.fixture
def store(tmp_path) -> JournalStore:
    return JournalStore(tmp_path / "journal.db")


@pytest.fixture
def writer(store: JournalStore) -> JournalWriter:
    w = JournalWriter(store)
    yield w
    w.close()


def _role(**kwargs) -> SubagentSpec:
    defaults = dict(name="researcher", prompt="be useful", tool_names=["fs_read"], max_steps=3)
    defaults.update(kwargs)
    return SubagentSpec(**defaults)


def _completes(answer: str, **fields) -> FakeProvider:
    return FakeProvider(
        turns=[f"I worked on it. {answer}"],
        json_results=[WorkerReport(status="completed", answer=answer, **fields)],
    )


class NeverRuns(FakeProvider):
    """A provider that fails where the failure happens.

    A cache hit is an absence - no worker, no events - and asserting an absence at the end
    of a test says "something was 1 instead of 0". This says which delegation re-ran.
    """

    async def stream(self, messages, tools=None, *, params):
        raise AssertionError("a worker ran for work this run had already completed")
        yield  # pragma: no cover - keeps this an async generator

    async def complete_json(self, messages, schema, *, params):
        raise AssertionError("a worker reported for work this run had already completed")


def _started(rj: RunJournal, turn_id: str = "t1") -> None:
    rj.emit(
        "agent_started",
        {
            "session_id": "s", "turn_id": turn_id, "role": "main", "actor": "main",
            "origin": "interactive", "channel": "cli", "autonomy": "act", "model": "m",
            "max_steps": 8, "input_chars": 2, "input_preview": "hi", "parent_turn_id": None,
        },
    )


async def _delegate(
    cfg, writer: JournalWriter, session: Session, spec: TaskSpec, provider, *, run_id: str = RUN
) -> WorkerResult:
    return await run_subagent(
        _role(), spec,
        parent_session_id=session.id, parent_turn_id=session.id, parent_autonomy="assist",
        approver=AutoApprover(True), registry=build_registry(), cfg=cfg, provider=provider,
        parent_run_id=run_id, parent_step_id="s1", journal=writer,
    )


def _types(writer: JournalWriter, run_id: str = RUN) -> list[str]:
    writer.flush()
    return [e.type for e in writer.store.read(run_id)]


# --- the key -----------------------------------------------------------------


def test_the_result_key_is_the_task_spec_and_not_a_second_derivation() -> None:
    """`result_key = hash(durable_role, task_spec, relevant_context_refs)` hashes three
    things that are all inside the canonical spec, so it is the spec's own digest - the same
    string `worker_created.task_digest` already carries. A second derivation over the same
    inputs would be free to drift from it, and nothing would notice until a resume served
    the wrong worker's answer."""
    spec = TaskSpec("researcher", "find the lease", relevant_context=["the flat in Ludlow St"])
    assert result_cache.result_key(spec) == spec.digest


def test_two_delegations_that_differ_only_in_phrasing_share_a_key() -> None:
    """The cheap direction, and the reason `TaskSpec` normalizes at all: a model that
    re-types its own background paragraph with different spacing has not asked a different
    question."""
    a = TaskSpec("researcher", "find  the lease\r\n", relevant_context=["a", "b"])
    b = TaskSpec("researcher", "find the lease", relevant_context=["b", "a", "b"])
    assert result_cache.result_key(a) == result_cache.result_key(b)


def test_two_delegations_that_differ_at_all_do_not_share_a_key() -> None:
    """The expensive direction. A false hit answers a question nobody asked, out of a worker
    that was doing something else, and leaves no worker behind to notice it by."""
    base = TaskSpec("researcher", "find the lease")
    others = [
        TaskSpec("coder", "find the lease"),
        TaskSpec("researcher", "find the Lease"),
        TaskSpec("researcher", "find the lease", relevant_context=["the flat in Ludlow St"]),
        TaskSpec("researcher", "find the lease", constraints=["do not call the landlord"]),
        TaskSpec("researcher", "find the lease", expected_output="the page number only"),
    ]
    keys = {result_cache.result_key(s) for s in [base, *others]}
    assert len(keys) == len(others) + 1


# --- what is kept, and what is not -------------------------------------------


async def test_a_worker_that_could_not_vouch_for_its_work_is_never_cached(cfg, writer) -> None:
    """`uncertain` is exactly the case where re-running may be the right answer. Serving it
    from a cache decides that question silently, once, for the rest of the run - and the
    caller who would have decided differently never learns there was a question."""
    session = await Session.create("test")
    spec = TaskSpec("researcher", "find the lease")
    provider = FakeProvider(
        turns=["I tried."],
        json_results=[WorkerReport(status="uncertain", answer="I cannot say what I changed.")],
    )
    result = await _delegate(cfg, writer, session, spec, provider)

    assert result.status == "uncertain"
    assert "worker_result_cached" not in _types(writer)
    assert result_cache.lookup(writer.store, RUN, spec) is None


async def test_a_blocked_worker_is_never_cached(cfg, writer) -> None:
    """`blocked` means the work did not happen, so there is nothing to serve. Caching it
    would turn "I could not reach the site" into this run's permanent answer about the
    site."""
    session = await Session.create("test")
    spec = TaskSpec("researcher", "read the landlord's site")
    provider = FakeProvider(
        turns=["it is down"],
        json_results=[WorkerReport(status="blocked", answer="The site refused every request.")],
    )
    await _delegate(cfg, writer, session, spec, provider)
    assert result_cache.lookup(writer.store, RUN, spec) is None


async def test_a_cached_result_is_on_disk_before_the_delegation_returns(cfg, writer) -> None:
    """The cache exists for the crash, so an entry that is still in this process's buffer is
    an entry the crash takes with it. Read through a second store on the same file, with
    nothing flushed: what a resume would find is what a resume finds."""
    session = await Session.create("test")
    spec = TaskSpec("researcher", "find the lease")
    await _delegate(cfg, writer, session, spec, _completes("Page four."))

    assert is_synchronous("worker_result_cached")
    cold = JournalStore(writer.store.path)
    assert result_cache.lookup(cold, RUN, spec).answer == "Page four."
    # And the flush that carried it took the worker's whole bracket with it.
    assert {"worker_created", "worker_finished"} <= {e.type for e in cold.read(RUN)}


async def test_a_reused_result_carries_no_transcript_and_says_why(cfg, writer) -> None:
    """6b left this open: "whether the transcript is persisted has to be answered before it
    is answered by accident". It is not persisted - the worker's prose is already in the
    archive, and every copy is another place it can reach a prompt from - so a reused result
    names the worker that earned it instead of carrying an empty transcript that reads as a
    worker with nothing to say."""
    session = await Session.create("test")
    spec = TaskSpec("researcher", "find the lease")
    first = await _delegate(cfg, writer, session, spec, _completes("Page four."))
    again = await _delegate(cfg, writer, session, spec, NeverRuns())

    assert first.transcript and first.reused_from == ""
    assert again.transcript == "" and again.reused_from
    assert again.for_orchestrator() == first.for_orchestrator()
    assert again.for_orchestrator(debug=True)["reused_from"] == again.reused_from
    entry = wr.cached_result(writer.store, RUN, spec.digest)
    assert "transcript" not in entry and entry["worker_id"] == again.reused_from


async def test_a_reused_result_is_still_untrusted_if_what_earned_it_was(writer) -> None:
    """Taint is a property of the work, not of the moment it is delivered. A cache that
    dropped it would let a result earned while the turn held the user's mail come back
    trusted the second time, which is the interlock defeated by an optimisation."""
    rj = RunJournal(writer, RUN)
    spec = TaskSpec("researcher", "read the mail thread")
    result_cache.remember(
        rj, spec,
        WorkerResult(status="completed", answer="They replied on Tuesday.", tainted=True),
        worker_id="w-1",
    )
    served = result_cache.serve(rj, spec)
    assert served.tainted is True


async def test_a_cache_hit_does_not_propose_the_same_memories_again(cfg, writer) -> None:
    """The other recurring bug in this codebase: the duplicate. One worker proposed these
    once and the review gate has them; re-inserting them per cache hit would let a single
    delegation, repeated, manufacture the look of independent corroboration."""
    session = await Session.create("test")
    spec = TaskSpec("researcher", "find the lease")
    provider = _completes(
        "Page four.",
        candidate_memories=[
            {"statement": "Dylan's lease renews in June", "confidence": 0.7,
             "category": "biographical"}
        ],
    )
    first = await _delegate(cfg, writer, session, spec, provider)
    assert len(first.candidate_memories) == 1
    assert len(await repo_memory.pending_candidates()) == 1

    again = await _delegate(cfg, writer, session, spec, NeverRuns())
    assert again.candidate_memories == ()
    assert len(await repo_memory.pending_candidates()) == 1


# --- one run, twice ----------------------------------------------------------


async def test_a_redundant_delegation_in_one_run_is_a_hit_and_not_a_second_bill(
    cfg, writer
) -> None:
    """The pass's second use. The orchestrator asking the same question twice in one run is
    common - a retry, a second step that forgot - and before this session each one started a
    worker."""
    session = await Session.create("test")
    spec = TaskSpec("researcher", "find the lease")
    first = await _delegate(cfg, writer, session, spec, _completes("Page four."))
    again = await _delegate(cfg, writer, session, spec, NeverRuns())

    assert again.answer == first.answer == "Page four."
    types = _types(writer)
    assert types.count("worker_created") == 1
    assert types.count("worker_result_reused") == 1


async def test_a_cache_hit_leaves_a_record_that_the_delegation_happened(cfg, writer) -> None:
    """A hit writes no `worker_created` and no `worker_finished`, so without this event the
    journal would say the second delegation was never made. It is also the only thing a
    reuse rate can be counted from."""
    session = await Session.create("test")
    spec = TaskSpec("researcher", "find the lease")
    await _delegate(cfg, writer, session, spec, _completes("Page four."))
    await _delegate(cfg, writer, session, spec, NeverRuns())

    writer.flush()
    events = writer.store.read(RUN)
    created = [e for e in events if e.type == "worker_created"]
    reused = [e for e in events if e.type == "worker_result_reused"]
    assert len(created) == 1 and len(reused) == 1
    assert reused[0].payload["result_key"] == spec.digest
    assert reused[0].payload["name"] == "researcher"
    # It names the worker that earned what was served, which is the one that did run.
    assert reused[0].payload["source_worker_id"] == created[0].payload["worker_id"]
    assert reused[0].payload["answer_chars"] == len("Page four.")
    assert reused[0].payload["parent_step_id"] == "s1"


# --- the exit criterion ------------------------------------------------------


async def test_a_resumed_run_does_not_re_run_the_workers_it_finished(cfg, writer) -> None:
    """The pass's exit criterion, measured. Three workers complete, the process dies with no
    `agent_finished` and no checkpoint of its own, a second process picks the run up off the
    file, and the same three delegations are made again.

    Reuse rate: 3 of 3, and zero `worker_created` events after the resume. The provider in
    the second half raises if a worker so much as starts.
    """
    session = await Session.create("test")
    specs = [
        TaskSpec("researcher", "find the lease"),
        TaskSpec("researcher", "find the renewal clause"),
        TaskSpec("researcher", "find the landlord's address"),
    ]
    answers = ["Page four.", "Clause 12.", "Ludlow St."]
    _started(RunJournal(writer, RUN))
    for spec, answer in zip(specs, answers, strict=True):
        await _delegate(cfg, writer, session, spec, _completes(answer))
    writer.flush()
    before = _types(writer)
    assert before.count("worker_finished") == 3

    # The process dies here: the writer is dropped without `agent_finished`, and a second
    # process opens the same file.
    survivor = JournalWriter(JournalStore(writer.store.path))
    try:
        resumed = R.resume(RUN, writer=survivor, reason="crash")
        assert resumed.applied and resumed.plan.state == R.INTERRUPTED

        served = [
            await _delegate(cfg, survivor, session, spec, NeverRuns()) for spec in specs
        ]
        survivor.flush()
        after = [e.type for e in survivor.store.read(RUN)]
    finally:
        survivor.close()

    assert [r.answer for r in served] == answers
    assert all(r.status == "completed" and r.reused_from for r in served)

    reused = after.count("worker_result_reused")
    assert reused == len(specs), f"reuse rate {reused}/{len(specs)}"
    assert after.count("worker_created") == before.count("worker_created") == 3


async def test_the_checkpoint_records_what_the_journal_has_cached(cfg, writer) -> None:
    """`worker_results[]` was the slot Pass 4 left inert. It is a copy of the fold, taken at
    `covers_seq` - an acceleration structure that may lag the journal and may never disagree
    with it - and not a second tally kept by the checkpointer, which would be free to say a
    run had earned something it had not."""
    cfg.checkpoints.enabled = True
    session = await Session.create("test")
    _started(RunJournal(writer, RUN))
    for spec, answer in [
        (TaskSpec("researcher", "find the lease"), "Page four."),
        (TaskSpec("researcher", "find the clause"), "Clause 12."),
    ]:
        await _delegate(cfg, writer, session, spec, _completes(answer))
    writer.flush()

    latest = Checkpointer(writer).latest(RUN)
    assert [r["answer"] for r in latest.worker_results] == ["Page four.", "Clause 12."]
    assert latest.worker_results == wr.cached_results_at(
        writer.store, RUN, latest.covers_seq
    )
    # Read back off the row, not off the object that was returned by the write.
    stored = Checkpointer(writer).get(latest.checkpoint_id)
    assert stored.worker_results == latest.worker_results


# --- run-scoped, which is the pass's Must not --------------------------------


async def test_a_result_cached_in_another_run_is_never_reused(cfg, writer) -> None:
    """Cross-run reuse is the pass's *Must not*: nothing here knows whether the repository,
    the web page or the file the worker read has changed since, and a cache that crossed
    runs would be asserting that it had not."""
    session = await Session.create("test")
    spec = TaskSpec("researcher", "find the lease")
    await _delegate(cfg, writer, session, spec, _completes("Page four."), run_id="run-a")
    assert result_cache.lookup(writer.store, "run-a", spec) is not None
    assert result_cache.lookup(writer.store, "run-b", spec) is None

    # And a delegation in the second run runs a worker, rather than quietly finding one.
    again = await _delegate(cfg, writer, session, spec, _completes("Page five."), run_id="run-b")
    assert again.answer == "Page five." and again.reused_from == ""
    assert _types(writer, "run-b").count("worker_created") == 1


async def test_a_forked_run_does_not_inherit_its_parents_results(cfg, writer) -> None:
    """A fork is a new run, and the same reasoning that denies it the parent's effect ledger
    denies it the parent's results: the user rewound because something was wrong, and the
    work done after the fork point is exactly the work they may want done again."""
    session = await Session.create("test")
    spec = TaskSpec("researcher", "find the lease")
    _started(RunJournal(writer, RUN))
    await _delegate(cfg, writer, session, spec, _completes("Page four."))
    writer.flush()
    at_seq = writer.store.read(RUN)[0].seq

    forked = F.fork(RUN, at_seq=at_seq, writer=writer, reason="wrong flat")
    assert result_cache.lookup(writer.store, forked.run_id, spec) is None


# --- the entry -----------------------------------------------------------------


async def test_an_entry_written_under_other_rules_is_a_miss_not_a_wrong_answer(writer) -> None:
    """The fields of an entry are versioned, separately from the key. A build that changed
    what an entry holds must re-run the work rather than read fields that may not mean what
    they say - and a miss is what this runtime did before the cache existed, so it is a
    behaviour that is known to be correct."""
    rj = RunJournal(writer, RUN)
    spec = TaskSpec("researcher", "find the lease")
    entry = result_cache.entry_for(
        spec, WorkerResult(status="completed", answer="Page four.")
    )
    # Written the way a later build would write it. The journal is append-only, so the
    # entry a future version left behind is a row this version has to read past.
    rj.emit(wr.CACHED, {**entry, "entry_version": wr.ENTRY_VERSION + 1}, worker_id="w-1")
    writer.flush()

    assert result_cache.lookup(writer.store, RUN, spec) is None


async def test_two_entries_under_one_key_do_not_change_the_answer_already_given(
    writer,
) -> None:
    """Two entries mean the same work was done twice - a write and a lookup that crossed,
    and routinely possible the day workers run in parallel. The first is kept, so a fold
    taken later cannot hand back a different answer than the fold that already served
    somebody: a cache that changes its mind is a cache whose hits are not reproducible."""
    rj = RunJournal(writer, RUN)
    spec = TaskSpec("researcher", "find the lease")
    for answer, worker in [("Page four.", "w-1"), ("Page five.", "w-2")]:
        result_cache.remember(
            rj, spec, WorkerResult(status="completed", answer=answer), worker_id=worker
        )

    assert result_cache.lookup(writer.store, RUN, spec).answer == "Page four."
    assert len(wr.cached_results_at(writer.store, RUN)) == 1


def test_a_cache_entry_is_read_by_key_with_nothing_filled_in() -> None:
    """The house bug, refused at the one place a stored result becomes a live one: a missing
    field raises rather than becoming an empty answer or an untainted flag, both of which
    are plausible enough to survive review and wrong in the direction that matters."""
    entry = result_cache.entry_for(
        TaskSpec("researcher", "find the lease"),
        WorkerResult(status="completed", answer="Page four.", evidence=("file://lease",)),
    ) | {"worker_id": "w-1"}
    assert result_cache.result_from_entry(entry).evidence == ("file://lease",)
    for field in ("answer", "tainted", "evidence", "worker_id"):
        with pytest.raises(KeyError):
            result_cache.result_from_entry({k: v for k, v in entry.items() if k != field})


# --- session 9a: a result the runtime could not corroborate -------------------


async def test_an_invalidated_result_is_never_cached(cfg, writer) -> None:
    """The third refusal, next to `blocked` and `uncertain`. Caching a disproved claim would
    serve it back for the rest of the run, free and with the verification stripped off - a
    fabrication is not cheaper the second time, only faster."""
    session = await Session.create("test")
    target = cfg.paths.roots()[0] / "parser.py"
    target.write_text("x")
    spec = TaskSpec("coder", "fix the parser and run the tests")
    provider = FakeProvider(
        turns=[[("fs_read", {"path": str(target)})], "Fixed it and ran the tests."],
        json_results=[WorkerReport(status="completed", answer="Fixed. All 19 tests pass.")],
    )
    result = await run_subagent(
        SubagentSpec(name="coder", prompt="be useful", tool_names=["fs_read"], max_steps=3),
        spec, parent_session_id=session.id, parent_turn_id=session.id, parent_autonomy="act",
        approver=AutoApprover(True), registry=build_registry(), cfg=cfg, provider=provider,
        parent_run_id=RUN, journal=writer,
    )
    assert result.validation == "invalidated"
    assert "worker_result_cached" not in _types(writer)
    assert result_cache.lookup(writer.store, RUN, spec) is None


async def test_a_doubtful_result_is_cached_with_its_doubt_attached(cfg, writer) -> None:
    """The other half, and the reason `remember` refuses `invalidated` rather than everything
    that is not `valid`: re-running a worker on every hit to re-derive a doubt that is already
    known costs a worker for nothing. What must not happen is the doubt being lost in the
    copy, so `validation` travels in the entry exactly as `tainted` does."""
    session = await Session.create("test")
    spec = TaskSpec("researcher", "what is in the lease")
    # No tool calls at all: 8d's shape, a soft flag, `uncertain` rather than `invalidated`.
    result = await _delegate(cfg, writer, session, spec, _completes("Page four."))
    assert result.validation == "uncertain"
    assert [f.code for f in result.flags] == ["completed_without_tools"]

    served = result_cache.lookup(writer.store, RUN, spec)
    assert served is not None
    assert served.validation == "uncertain"
    assert [f.code for f in served.flags] == ["completed_without_tools"]
    assert served.flags[0].detail  # the runtime's sentence survived the round trip


def test_an_entry_written_under_the_old_rules_is_skipped_rather_than_read(writer) -> None:
    """`entry_version` went 1 -> 2 when verification was added. A v1 entry was written before
    anything checked it, and serving it as `valid` would assert a check that never ran."""
    rj = RunJournal(writer, RUN)
    spec = TaskSpec("researcher", "the old one")
    entry = result_cache.entry_for(spec, WorkerResult(status="completed", answer="stale"))
    rj.emit(wr.CACHED, {**entry, "entry_version": 1}, worker_id="w-old", sync=True)
    assert result_cache.lookup(writer.store, RUN, spec) is None
