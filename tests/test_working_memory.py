"""Working memory: whose it is, and when it stops existing.

Session 7b. The working bucket is the one memory type that is *execution state* - scratch
the agent keeps while doing a task - and the two ways it can be worse than not having it are
both quiet:

* **one agent reading another's.** Two workers running at once share a run, a journal, a
  writer and - because `run_subagent` hands a worker `Session(id=parent_session_id)` - a
  session id. Isolation built on any of those would be a single shared bucket that looks
  full and healthy from every angle, and a worker that had been sent to read a stranger's
  web page would be writing into the same scratchpad as the worker reading the user's mail.
  The tests below run two workers concurrently and check both what each model was actually
  shown and what the journal says each scope held.
* **state that outlives the task it belonged to.** "Discarded when the run completes" is
  what makes this a bucket rather than a slow leak into a store nobody prunes. The check is
  a fold, not a flag: after the turn, the run holds no scope with notes in it.

Two things these tests pin on purpose, because they are decisions and not consequences:
working memory never goes through `memory/retrieval.pack()` (see `memory/scopes.py`), and a
note that does not fit is refused rather than truncated.
"""

from __future__ import annotations

import asyncio

import pytest

from agentd.agent.delegation import TaskSpec
from agentd.agent.loop import AgentLoop, Session
from agentd.agent.results import WorkerReport
from agentd.agent.subagents import SubagentSpec, run_subagent
from agentd.agent.working_memory import ORCHESTRATOR, WorkingMemory, WorkingMemoryError
from agentd.journal import runtime as journal_runtime
from agentd.journal import working_memory as wm
from agentd.journal.checkpoints import Checkpointer
from agentd.journal.runtime import RunJournal
from agentd.journal.store import JournalStore
from agentd.journal.writer import JournalWriter
from agentd.llm.fake import FakeProvider
from agentd.memory import scopes
from agentd.policy.approvals import AutoApprover
from agentd.policy.engine import engine_from_config
from agentd.tools import builtin_working
from agentd.tools.base import ToolContext
from agentd.tools.registry import Registry, build_registry

RUN = "run-1"


@pytest.fixture
def writer(tmp_path) -> JournalWriter:
    w = JournalWriter(JournalStore(tmp_path / "journal.db"))
    yield w
    w.close()


def _registry() -> Registry:
    reg = Registry()
    reg.add(*builtin_working.TOOLS)
    return reg


def _spec(**kwargs) -> SubagentSpec:
    defaults = dict(
        name="researcher",
        prompt="be useful",
        tool_names=["working_memory_note", "working_memory_list"],
        max_steps=4,
    )
    defaults.update(kwargs)
    return SubagentSpec(**defaults)


def _worker_provider(text: str) -> FakeProvider:
    """A worker that notes one thing, reads its notes back, and reports.

    The key is the worker's own word rather than a shared "finding": two workers filing
    under one key into one shared bucket would collide into a *single* note, and a listing
    that showed one note would still read as isolation. Distinct keys are what make a shared
    bucket show up as two.
    """
    key = text.split("-")[0]
    return FakeProvider(
        turns=[
            [("working_memory_note", {"key": key, "note": text})],
            [("working_memory_list", {})],
            "reported",
        ],
        json_results=[WorkerReport(status="completed", answer=f"done: {text}")],
    )


def _tool_messages(provider: FakeProvider) -> str:
    """Everything this model was ever handed as a tool result, as one string.

    The point of asserting here rather than on the fold: isolation is about what an agent
    could *see*, and the prompt is where seeing happens.
    """
    return "\n".join(
        m.get("content") or ""
        for call in provider.calls
        for m in call.get("messages", [])
        if m.get("role") == "tool"
    )


def _store(cfg):
    """The journal this test's turns wrote to, flushed.

    The flush is not incidental: `working_memory_noted` and `working_memory_discarded` are
    buffered like the rest of a turn's chatter, so an assertion made without it would be
    about the writer's buffer rather than about the journal.
    """
    writer = journal_runtime.get_writer(cfg)
    writer.flush()
    return writer.store


def _events(cfg, run_id: str):
    return _store(cfg).read(run_id)


# --- two workers at once ------------------------------------------------------


async def test_two_concurrent_workers_cannot_read_each_others_working_memory(cfg):
    """The pass's exit criterion, through the real tool surface.

    Both workers run in one run, against one journal writer, under one session id, and each
    reads its scratchpad back through the same tool. What comes back is its own note and
    only its own - so the boundary holds where it matters, which is in the prompt.
    """
    session = await Session.create("test")
    alpha, bravo = _worker_provider("alpha-clause-is-on-page-four"), _worker_provider(
        "bravo-the-rent-is-due-friday"
    )

    async def delegate(provider: FakeProvider, task: str):
        return await run_subagent(
            _spec(),
            TaskSpec("researcher", task),
            parent_session_id=session.id, parent_turn_id=session.id,
            parent_autonomy="assist", approver=AutoApprover(True),
            registry=build_registry(), cfg=cfg, provider=provider, parent_run_id=RUN,
        )

    await asyncio.gather(
        delegate(alpha, "read the lease"), delegate(bravo, "read the invoice")
    )

    seen_by_alpha, seen_by_bravo = _tool_messages(alpha), _tool_messages(bravo)
    assert "alpha-clause-is-on-page-four" in seen_by_alpha
    assert "bravo-the-rent-is-due-friday" not in seen_by_alpha
    assert "bravo-the-rent-is-due-friday" in seen_by_bravo
    assert "alpha-clause-is-on-page-four" not in seen_by_bravo


async def test_the_two_workers_really_did_overlap(cfg):
    """Without this the isolation test above could pass by running them one after another.

    A sequential pair proves nothing about two agents sharing a journal: the failure being
    guarded against is a shared bucket, and a shared bucket is only observable while both
    halves are open.
    """
    session = await Session.create("test")
    alpha, bravo = _worker_provider("alpha-note"), _worker_provider("bravo-note")

    async def delegate(provider: FakeProvider, task: str):
        return await run_subagent(
            _spec(),
            TaskSpec("researcher", task),
            parent_session_id=session.id, parent_turn_id=session.id,
            parent_autonomy="assist", approver=AutoApprover(True),
            registry=build_registry(), cfg=cfg, provider=provider, parent_run_id=RUN,
        )

    await asyncio.gather(delegate(alpha, "one"), delegate(bravo, "two"))

    events = _events(cfg, RUN)
    workers = [
        (e.payload["worker_id"], e.seq) for e in events if e.type == "worker_created"
    ]
    assert len(workers) == 2
    (first_id, first_seq), (second_id, _) = workers
    finished = next(
        e.seq for e in events
        if e.type == "worker_finished" and e.payload["worker_id"] == first_id
    )
    interleaved = [
        e for e in events
        if first_seq < e.seq < finished and e.payload.get("worker_id") == second_id
    ]
    assert interleaved, "the second worker wrote nothing while the first was open"


async def test_each_worker_scope_held_only_its_own_note(cfg):
    """The same claim as the prompt-level one, read off the journal instead.

    `working_memory_discarded.notes` is the count at the moment the scope ended, so two
    workers that had been sharing a bucket would each report 2 here - the shape of the
    failure, not just its absence.
    """
    session = await Session.create("test")
    alpha, bravo = _worker_provider("alpha-note"), _worker_provider("bravo-note")

    async def delegate(provider: FakeProvider, task: str):
        return await run_subagent(
            _spec(),
            TaskSpec("researcher", task),
            parent_session_id=session.id, parent_turn_id=session.id,
            parent_autonomy="assist", approver=AutoApprover(True),
            registry=build_registry(), cfg=cfg, provider=provider, parent_run_id=RUN,
        )

    await asyncio.gather(delegate(alpha, "one"), delegate(bravo, "two"))

    events = _events(cfg, RUN)
    noted = [e.payload for e in events if e.type == wm.NOTED]
    assert len({n["scope"] for n in noted}) == 2, "two workers, two scopes"
    discards = [e.payload for e in events if e.type == wm.DISCARDED]
    assert [d["reason"] for d in discards] == ["worker_finished", "worker_finished"]
    assert [d["notes"] for d in discards] == [1, 1]


async def test_a_worker_leaves_no_working_memory_behind_when_it_finishes(cfg):
    """A worker's scratch state dies with its task scope, not with the run.

    What the caller gets is the result - the boundary crossing the pass file allows - and
    not the notes the worker got there with.
    """
    session = await Session.create("test")
    await run_subagent(
        _spec(),
        TaskSpec("researcher", "read the lease"),
        parent_session_id=session.id, parent_turn_id=session.id,
        parent_autonomy="assist", approver=AutoApprover(True),
        registry=build_registry(), cfg=cfg, provider=_worker_provider("a finding"),
        parent_run_id=RUN,
    )
    assert wm.scopes_with_notes(_store(cfg), RUN) == {}


# --- a run that completes -----------------------------------------------------


async def test_a_completed_run_leaves_no_working_memory_behind(cfg):
    """The other half of the exit criterion, over a whole turn.

    Asserted as a fold over the journal rather than as a flag somebody sets: `state =
    fold(reduce, journal, initial)`, so "nothing is left" has to be what the fold says.
    """
    provider = FakeProvider(
        turns=[
            [("working_memory_note", {"key": "plan", "note": "check the lease first"})],
            "all done",
        ]
    )
    loop = AgentLoop(
        cfg=cfg, registry=_registry(), engine=engine_from_config(cfg),
        approver=AutoApprover(True), provider=provider,
    )
    session = await Session.create("test")
    run_id = None
    async for _ in loop.run_turn(session, "make a plan", run_id=RUN):
        run_id = RUN

    assert run_id == RUN
    assert wm.scopes_with_notes(_store(cfg), RUN) == {}, "the run kept scratch state past its end"
    discards = [e.payload for e in _events(cfg, RUN) if e.type == wm.DISCARDED]
    assert [(d["scope"], d["reason"], d["notes"]) for d in discards] == [
        (ORCHESTRATOR, "run_completed", 1)
    ]


async def test_the_notes_of_a_completed_run_are_entombed_rather_than_deleted(cfg, writer):
    """Nothing in this runtime is overwritten, and a discard is no exception.

    The live view is empty; a fold taken at the position the notes were written at still
    shows them, which is what keeps a finished run auditable and what lets a later pass ask
    what a task had in hand when it decided something.
    """
    rj = RunJournal(writer, RUN)
    memory = WorkingMemory.for_turn(rj)
    memory.note("plan", "check the lease first")
    writer.flush()
    before_discard = writer.store.last_seq(RUN)

    assert memory.discard("run_completed") == 1
    writer.flush()

    assert memory.notes() == ()
    kept = wm.notes_at(writer.store, RUN, ORCHESTRATOR, before_discard)
    assert [n["text"] for n in kept] == ["check the lease first"]
    assert [e.type for e in writer.store.read(RUN)] == [wm.NOTED, wm.DISCARDED]


# --- the isolation mechanism itself ------------------------------------------


def test_one_scope_cannot_see_another_in_the_same_run(writer):
    """The mechanism, without a model in the way: two scopes, one run, one writer."""
    rj = RunJournal(writer, RUN)
    worker_a = WorkingMemory.for_turn(rj.for_worker("worker-a"))
    worker_b = WorkingMemory.for_turn(rj.for_worker("worker-b"))
    orchestrator = WorkingMemory.for_turn(rj)

    worker_a.note("finding", "a's finding")
    worker_b.note("finding", "b's finding")
    orchestrator.note("finding", "the caller's finding")

    assert [n.text for n in worker_a.notes()] == ["a's finding"]
    assert [n.text for n in worker_b.notes()] == ["b's finding"]
    assert [n.text for n in orchestrator.notes()] == ["the caller's finding"]
    # The same key in three scopes is three notes, not one race for one row.
    assert wm.scopes_with_notes(writer.store, RUN) == {
        "worker-a": 1, "worker-b": 1, ORCHESTRATOR: 1,
    }


def test_the_orchestrators_scope_is_a_name_and_not_an_absent_worker_id(writer):
    """`worker_id IS NULL` as a bucket is this codebase's recurring bug in its other shape:
    an absent value becoming a shared grouping key that unrelated writers collide in."""
    rj = RunJournal(writer, RUN)
    assert WorkingMemory.for_turn(rj).scope == ORCHESTRATOR
    assert WorkingMemory.for_turn(rj.for_worker("w-1")).scope == "w-1"
    WorkingMemory.for_turn(rj).note("k", "v")
    writer.flush()
    noted = writer.store.read(RUN)[0].payload
    assert noted["scope"] == ORCHESTRATOR
    assert "worker_id" not in noted
    with pytest.raises(ValueError, match="scope"):
        wm.notes_at(writer.store, RUN, "")


def test_working_memory_never_crosses_a_run(writer):
    """Run scoping is the second equality in the query, and it is not derived from the first.

    A worker id is unique, so a bug that dropped the run from the query would still look
    isolated between workers. The orchestrator's scope is the same string in every run,
    which is where such a bug shows.
    """
    WorkingMemory.for_turn(RunJournal(writer, "run-a")).note("k", "run a's note")
    WorkingMemory.for_turn(RunJournal(writer, "run-b")).note("k", "run b's note")
    writer.flush()

    assert [n["text"] for n in wm.notes_at(writer.store, "run-a", ORCHESTRATOR)] == [
        "run a's note"
    ]
    assert [n["text"] for n in wm.notes_at(writer.store, "run-b", ORCHESTRATOR)] == [
        "run b's note"
    ]


def test_a_tool_call_with_no_task_scope_refuses_rather_than_inventing_one(writer):
    """`policy/replay.execute_approved` runs a queued call long after its turn ended.

    The wrong answers here are both plausible: an empty scratchpad ("nothing noted yet"),
    or a fallback scope everything shares. It says there is nowhere to put this, and fails.
    """
    result = asyncio.run(
        builtin_working.working_memory_note.handler(
            {"key": "k", "note": "v"}, ToolContext(run_id=RUN)
        )
    )
    assert result.ok is False
    assert "no task scope" in result.content
    assert writer.store.read(RUN) == []


# --- checkpoints --------------------------------------------------------------


def test_a_checkpoint_accounts_for_the_working_memory_of_its_own_position(cfg, writer):
    """"Captured by checkpoints" without a column, and without a second derivation.

    A checkpoint is a position (`covers_seq`); the fold at that position is what the run
    held there. That is the relationship `messages_ref` already has with the messages, and
    it is why nothing was added to the `checkpoint` table for this - which matters on this
    machine in particular, where `[checkpoints] enabled` is false and no checkpoint row has
    ever been written.
    """
    on = cfg.model_copy(deep=True)
    on.checkpoints.enabled = True
    rj = RunJournal(writer, RUN)
    rj.emit(
        "agent_started",
        {
            "session_id": "s", "turn_id": "t", "role": "main", "actor": "main",
            "origin": "test", "channel": "cli", "autonomy": "assist", "model": "m",
            "max_steps": 1, "input_chars": 1, "input_preview": "x", "parent_turn_id": None,
        },
    )
    memory = WorkingMemory.for_turn(rj)
    memory.note("early", "known before the boundary")

    checkpoint = Checkpointer(writer, cfg=on).write(RUN, trigger="manual")
    memory.note("late", "learned after it")
    writer.flush()

    at_boundary = wm.notes_at(writer.store, RUN, ORCHESTRATOR, checkpoint.covers_seq)
    assert [n["key"] for n in at_boundary] == ["early"]
    assert [n.key for n in memory.notes()] == ["early", "late"]


# --- the decision not to render this through retrieval ------------------------


def test_working_memory_is_not_one_of_the_kinds_retrieval_renders():
    """A decision, pinned so that changing it takes changing the decision.

    `_render` maps `item.kind` through a dict literal and `pack()` is wrapped in a bare
    `except` in `agent/loop.py`, so a kind that reaches retrieval without being in all four
    of `TYPE_PRIOR`, `CHANNEL_WEIGHTS`, `SECTION_SHARE` and the section map costs the prompt
    every piece of memory and says only "(memory retrieval unavailable)". The `"raw"` kind,
    which is in two of the four, is the standing proof that nothing enforces this.

    So the working bucket stays out, and this test fails the moment somebody adds it to one
    dict literal without deciding to.
    """
    from agentd.memory import retrieval

    assert scopes.BUCKETS[scopes.WORKING].retrieval is False
    assert scopes.WORKING not in retrieval.TYPE_PRIOR
    assert scopes.WORKING not in retrieval.CHANNEL_WEIGHTS
    assert scopes.WORKING not in retrieval.SECTION_SHARE
    assert scopes.WORKING not in scopes.RETRIEVAL_KINDS
    # And the buckets that *are* retrieved name kinds retrieval actually knows about.
    assert {"fact", "episode"} <= scopes.RETRIEVAL_KINDS
    assert scopes.RETRIEVAL_KINDS >= set(retrieval.SECTION_SHARE) - {
        "facts", "claims", "procedures", "episodes"
    }


def test_the_three_buckets_differ_in_store_scope_and_lifetime():
    """Three buckets, or one bucket described three ways. The fields are the difference.

    Storage is shared where it already was - episodic and semantic are two tables in the one
    Postgres schema, unmigrated - and separate only for working memory, which needs a
    lifecycle that store cannot express.
    """
    assert set(scopes.BUCKETS) == {scopes.WORKING, scopes.EPISODIC, scopes.SEMANTIC}
    working = scopes.BUCKETS[scopes.WORKING]
    episodic = scopes.BUCKETS[scopes.EPISODIC]
    semantic = scopes.BUCKETS[scopes.SEMANTIC]

    assert episodic.store == semantic.store == "postgres"
    assert working.store == "journal"
    assert scopes.EXECUTION_STATE == {scopes.WORKING}
    assert episodic.ends_with == semantic.ends_with == "never"
    assert working.scope.startswith("run")
    # 7a's finding, carried as data rather than as prose: the episodic bucket is declared
    # and wired and has never been written on the live system.
    assert episodic.live is False
    with pytest.raises(KeyError, match="exactly three"):
        scopes.bucket("procedural")


# --- what a note refuses to be ------------------------------------------------


def test_a_note_that_does_not_fit_is_refused_rather_than_truncated(writer):
    """A clipped note reads exactly like a whole one, and this bucket is what a later pass
    classifies for promotion - so the failure has to be visible at the moment it happens."""
    memory = WorkingMemory.for_turn(RunJournal(writer, RUN))
    with pytest.raises(WorkingMemoryError, match="refused rather than truncated"):
        memory.note("big", "x" * (wm.NOTE_MAX_CHARS + 1))
    with pytest.raises(WorkingMemoryError, match="empty"):
        memory.note("blank", "   ")
    with pytest.raises(WorkingMemoryError, match="key"):
        memory.note("  ", "something")
    writer.flush()
    assert writer.store.read(RUN) == []


async def test_a_note_remembers_the_provenance_of_the_turn_that_wrote_it(cfg, writer):
    """Both flags, recorded when they can still be observed.

    A note written while the user's mail was in context is not the same object as one the
    user dictated, and a promotion pass a week later has no way to find that out. An absent
    flag would read as a clean one, which is the laundering this codebase keeps almost doing.
    """
    handle = WorkingMemory.for_turn(RunJournal(writer, RUN))
    ctx = ToolContext(run_id=RUN, tainted=True, private=True)
    ctx.extra["working_memory"] = handle
    await builtin_working.working_memory_note.handler(
        {"key": "k", "note": "from the web"}, ctx
    )

    note = handle.get("k")
    assert note is not None and note.tainted is True and note.private is True

    listed = await builtin_working.working_memory_list.handler({}, ctx)
    assert listed.trust == "untrusted"
