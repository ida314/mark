"""A checkpoint is the runtime's promise that it could pick this run up again.

Every property asserted here is about that promise being honest rather than merely present.
A snapshot that claims a journal position the journal never reached, that copies the
conversation instead of pointing at it, that names an orchestrator nobody started, or that
is taken while a worker is half-finished would all be *written* successfully and would all
lie to the resume path that Pass 4b builds on top of them.

The two that matter most, because they are the ones a green test suite would otherwise let
through:

- **`event_seq > covers_seq`.** The checkpoint may lag the journal and may never lead it.
  If the snapshot were taken after the announcement, a resume would replay from a position
  the snapshot already accounted for, and the only symptom would be work done twice.
- **Three fields are still inert on purpose.** `worker_results`, `pending_promotions` and
  `memory_watermark` belong to Passes 6 and 7. They are stored and read back as empty lists
  and nulls, and the difference between those two is load bearing: a list is "there were
  none", a null is "this pass recorded none". An empty dict in the watermark would read as a
  measurement of zero. `handoff_object` was the fourth until session 5b filled it; a
  checkpoint written without one still stores NULL, which is what the tests below assert.
"""

from __future__ import annotations

import sqlite3

import pytest

from agentd.agent.delegation import TaskSpec
from agentd.agent.loop import AgentLoop, Session
from agentd.agent.results import WorkerReport
from agentd.agent.subagents import SubagentSpec, run_subagent
from agentd.journal import checkpoints as cps
from agentd.journal.checkpoints import (
    Checkpointer,
    CheckpointError,
    MidWorkerCheckpoint,
    NoOrchestrator,
    checkpoint_at,
)
from agentd.journal.ledger import EffectLedger
from agentd.journal.runtime import RunJournal
from agentd.journal.store import MIGRATIONS, SCHEMA_VERSION, JournalStore, Retention
from agentd.journal.writer import JournalWriter
from agentd.llm.fake import FakeProvider
from agentd.policy.approvals import AutoApprover
from agentd.policy.engine import engine_from_config
from agentd.tools.base import Tool, ToolResult, obj
from agentd.tools.registry import Registry, build_registry


@pytest.fixture
def on(cfg):
    """The feature flag, on. It is off in `config/default.toml` while nothing reads a
    checkpoint yet, so every test that expects one has to say so."""
    cfg.checkpoints.enabled = True
    return cfg


def _writer(tmp_path) -> JournalWriter:
    return JournalWriter(JournalStore(tmp_path / "journal.db"))


def _started(rj: RunJournal, turn_id: str = "t1") -> None:
    rj.emit(
        "agent_started",
        {
            "session_id": "s", "turn_id": turn_id, "role": "main", "actor": "main",
            "origin": "interactive", "channel": "cli", "autonomy": "act", "model": "m",
            "max_steps": 8, "input_chars": 2, "input_preview": "hi", "parent_turn_id": None,
        },
    )


def _tool(*, name: str = "sends_mail", effect_class: str = "unsafe_write") -> Tool:
    async def handler(args, ctx):
        return ToolResult(content="sent")

    return Tool(
        name=name, description="does a thing", parameters=obj(body={}),
        handler=handler, effect_class=effect_class, risk="read",
    )


def _loop(cfg, writer: JournalWriter, provider, *tools: Tool) -> AgentLoop:
    registry = Registry()
    if tools:
        registry.add(*tools)
    return AgentLoop(
        cfg=cfg, registry=registry, engine=engine_from_config(cfg),
        approver=AutoApprover(True), provider=provider, journal=writer,
    )


# --- the record --------------------------------------------------------------


def test_a_checkpoint_points_at_the_conversation_instead_of_copying_it(on, tmp_path) -> None:
    """`messages_ref` is a pointer. The journal already holds what was said, and a second
    copy in a second store is a second thing to keep true - and a second place the user's
    words live."""
    writer = _writer(tmp_path)
    rj = RunJournal(writer, "run-1")
    _started(rj)
    secret = "the spare key is under the third flowerpot"
    rj.emit(
        "message_appended",
        {"role": "user", "actor": "user", "chars": len(secret), "preview": secret},
    )
    cp = Checkpointer(writer).write("run-1", trigger="turn_end")

    assert cp.messages_ref.run_id == "run-1"
    assert cp.messages_ref.through_seq == cp.covers_seq == 2
    assert cp.messages_ref.message_events == 1
    row = writer.store.query("SELECT * FROM checkpoint")[0]
    stored = " ".join(str(v) for v in dict(row).values())
    assert "flowerpot" not in stored
    writer.close()


def test_a_snapshot_never_claims_a_position_the_journal_has_not_reached(on, tmp_path) -> None:
    """`state = fold(reduce, journal, initial)`: the snapshot is an acceleration structure
    over the journal and may lag it, which means its announcement is always later than what
    it covers. Enforced by the storage, not by this call path."""
    writer = _writer(tmp_path)
    _started(RunJournal(writer, "run-1"))
    cp = Checkpointer(writer).write("run-1", trigger="manual")
    assert cp.event_seq > cp.covers_seq

    with pytest.raises(sqlite3.IntegrityError):
        with writer.store.transaction() as conn:
            conn.execute(
                "INSERT INTO checkpoint (checkpoint_id, run_id, covers_seq, event_seq, "
                "created_at, trigger, orchestrator_id, messages_ref, open_workers, "
                "effects_cursor, worker_results, pending_promotions) "
                "VALUES ('c2','run-1',9,9,'now','manual','t1','{}','[]','{}','[]','[]')"
            )
    writer.close()


def test_the_slots_later_passes_fill_are_empty_rather_than_absent(on, tmp_path) -> None:
    """Passes 5, 6 and 7 fill a slot instead of migrating a schema. An empty list and a null
    are different statements and stay different: "there were none" is not "nobody looked"."""
    writer = _writer(tmp_path)
    _started(RunJournal(writer, "run-1"))
    written = Checkpointer(writer).write("run-1", trigger="turn_end")
    read_back = Checkpointer(writer).get(written.checkpoint_id)

    assert read_back == written
    assert read_back.handoff_object is None
    assert read_back.memory_watermark is None
    assert read_back.worker_results == ()
    assert read_back.pending_promotions == ()
    row = writer.store.query("SELECT * FROM checkpoint")[0]
    assert row["handoff_object"] is None and row["memory_watermark"] is None
    assert row["worker_results"] == "[]" and row["pending_promotions"] == "[]"
    writer.close()


def test_the_row_and_the_event_that_announces_it_agree(on, tmp_path) -> None:
    """A checkpoint whose announcement was lost is a snapshot nothing points at, and one
    that disagrees with its announcement is worse: two records of one fact."""
    writer = _writer(tmp_path)
    _started(RunJournal(writer, "run-1"))
    cp = Checkpointer(writer).write("run-1", trigger="pre_effect")

    events = [e for e in writer.store.read("run-1") if e.type == "checkpoint_written"]
    assert len(events) == 1
    announced = events[0]
    assert announced.seq == cp.event_seq
    assert announced.payload["checkpoint_id"] == cp.checkpoint_id
    assert announced.payload["covers_seq"] == cp.covers_seq
    assert announced.payload["trigger"] == "pre_effect"
    # Pass 7's field, said out loud as absent rather than as an empty measurement.
    assert announced.payload["memory_watermark"] is None
    assert announced.payload["bytes"] > 0
    writer.close()


def test_every_trigger_in_the_vocabulary_can_actually_be_written(on, tmp_path) -> None:
    """`manual` is a person asking and `handoff` had no producer until session 5a. Both are
    written here so neither is a slot nobody ever filled."""
    writer = _writer(tmp_path)
    checkpointer = Checkpointer(writer)
    for i, trigger in enumerate(cps.TRIGGERS):
        run = f"run-{i}"
        _started(RunJournal(writer, run), turn_id=f"t{i}")
        assert checkpointer.write(run, trigger=trigger).trigger == trigger
    writer.close()


def test_an_invented_trigger_is_refused(on, tmp_path) -> None:
    writer = _writer(tmp_path)
    _started(RunJournal(writer, "run-1"))
    with pytest.raises(CheckpointError) as exc:
        Checkpointer(writer).write("run-1", trigger="looked_like_a_good_moment")
    assert "unknown checkpoint trigger" in str(exc.value)
    assert writer.store.query("SELECT * FROM checkpoint") == []
    writer.close()


def test_a_run_with_no_orchestrator_is_refused_rather_than_given_a_plausible_one(
    on, tmp_path
) -> None:
    """Session 3b keys a detached tool call under `detached:<action_id>`, which is a run
    holding two effect events and no turn. Naming an orchestrator for it would invent the
    one field a resume uses to decide who is being resumed."""
    writer = _writer(tmp_path)
    RunJournal(writer, "detached:abc").emit(
        "effect_intended",
        {
            "effect_id": "e1", "step_id": "s1", "tool_name": "fs_write",
            "effect_class": "unsafe_write", "idempotency_key": "k", "args_digest": "d",
        },
    )
    with pytest.raises(NoOrchestrator):
        Checkpointer(writer).write("detached:abc", trigger="pre_effect")
    with pytest.raises(NoOrchestrator):
        Checkpointer(writer).write("never-happened", trigger="manual")
    writer.close()


def test_a_workers_turn_does_not_name_itself_the_orchestrator(on, tmp_path) -> None:
    """A worker's `agent_started` is in its caller's run, tagged with `worker_id`. The
    orchestrator is the turn that opened the run, which is the untagged one."""
    writer = _writer(tmp_path)
    rj = RunJournal(writer, "run-1")
    _started(rj, turn_id="caller")
    worker = rj.for_worker("w-1")
    _started(worker, turn_id="worker-turn")
    worker.emit(
        "worker_finished",
        {
            "worker_id": "w-1", "name": "researcher", "status": "completed",
            "answer_chars": 1, "report_valid": True, "tainted": False, "evidence": 0,
            "actions_taken": 0, "followups": 0, "candidates": 0, "tokens": 0,
            "duration_ms": 1,
            "validation": "valid", "flags": [],
        },
    )
    cp = Checkpointer(writer).write("run-1", trigger="worker_finished")
    assert cp.orchestrator_id == "caller"
    writer.close()


# --- workers are the unit of atomicity ---------------------------------------


def test_a_worker_in_flight_refuses_a_snapshot(on, tmp_path) -> None:
    """The architecture's "do not checkpoint inside a worker": a worker is re-delegated
    from its task spec, never resumed from the middle, so a snapshot taken halfway through
    one describes a state nothing can be restored to."""
    writer = _writer(tmp_path)
    rj = RunJournal(writer, "run-1")
    _started(rj)
    rj.emit(
        "worker_created",
        {
            "worker_id": "w-1", "name": "researcher", "role": "subagent", "autonomy": "act",
            "max_steps": 5, "tools": [], "task_chars": 4, "task_preview": "find the thing",
            "task_digest": "d" * 64, "parent_step_id": "s1",
        },
        worker_id="w-1",
    )
    with pytest.raises(MidWorkerCheckpoint) as exc:
        Checkpointer(writer).write("run-1", trigger="turn_end")
    assert "w-1" in str(exc.value)
    # The boundary door reads that as "no checkpoint here", because it is the rule and not
    # a failure - but nothing is written either way.
    assert checkpoint_at("turn_end", run_id="run-1", writer=writer) is None
    assert writer.store.query("SELECT * FROM checkpoint") == []

    rj.emit(
        "worker_finished",
        {
            "worker_id": "w-1", "name": "researcher", "status": "completed",
            "answer_chars": 1, "report_valid": True, "tainted": False, "evidence": 0,
            "actions_taken": 0, "followups": 0, "candidates": 0, "tokens": 0,
            "duration_ms": 1,
            "validation": "valid", "flags": [],
        },
        worker_id="w-1",
    )
    cp = checkpoint_at("worker_finished", run_id="run-1", writer=writer)
    assert cp is not None and cp.open_workers == ()
    writer.close()


async def test_a_delegating_turn_checkpoints_when_the_worker_returns_and_not_before(
    on, tmp_path
) -> None:
    """The worker_finished boundary, end to end. The worker's own turn ends inside the
    caller's run and writes no checkpoint of its own, because at that moment it is still
    the open worker."""
    writer = _writer(tmp_path)
    provider = FakeProvider(
        turns=["I looked and found it."],
        json_results=[WorkerReport(status="completed", answer="Found it.")],
    )
    session = await Session.create("test")
    _started(RunJournal(writer, "run-1"), turn_id="caller")
    await run_subagent(
        SubagentSpec(name="researcher", prompt="be useful", tool_names=["fs_read"], max_steps=3),
        TaskSpec("researcher", "find the thing"),
        parent_session_id=session.id, parent_turn_id=session.id, parent_autonomy="assist",
        approver=AutoApprover(True), registry=build_registry(), cfg=on, provider=provider,
        parent_run_id="run-1", journal=writer,
    )

    written = Checkpointer(writer).entries("run-1")
    assert [c.trigger for c in written] == ["worker_finished"]
    types = [e.type for e in writer.store.read("run-1")]
    # The snapshot is announced after the worker's result, never between its events. Session
    # 6c put one event in between: the cached result, which is written *before* the boundary
    # on purpose, so that the snapshot accounts for it rather than describing a run that had
    # not yet earned it.
    assert types[types.index("worker_finished") + 1 :] == [
        "worker_result_cached", "checkpoint_written",
    ]
    assert written[0].worker_results[0]["answer"] == "Found it."
    writer.close()


# --- the boundaries a turn crosses -------------------------------------------


async def test_a_normal_turn_is_checkpointed_at_its_boundaries(on, tmp_path) -> None:
    """The pass's exit criterion for 4a, as a sequence: a snapshot before the unsafe write
    is even announced, and one when the turn is over."""
    writer = _writer(tmp_path)
    provider = FakeProvider(turns=[[("sends_mail", {})], "sent it"])
    loop = _loop(on, writer, provider, _tool())
    session = await Session.create("test")
    async for _ in loop.run_turn(session, "send the mail"):
        pass

    run_id = writer.store.runs()[0].run_id
    assert [c.trigger for c in Checkpointer(writer).entries(run_id)] == [
        "pre_effect", "turn_end",
    ]
    types = [e.type for e in writer.store.read(run_id)]
    # Before the announcement of the effect, not after it: the snapshot is what a resume
    # stands on when it finds that effect unresolved.
    assert types.index("checkpoint_written") < types.index("effect_intended")
    assert types[-1] == "checkpoint_written"
    assert types[-2] == "agent_finished"
    writer.close()


async def test_a_read_only_turn_is_checkpointed_once_at_the_end(on, tmp_path) -> None:
    """`pre_effect` fires only for `unsafe_write`. An `idempotent_write` converges on
    re-execution and a `read` changes nothing outside, so putting a flush and two commits in
    front of either buys nothing and would bury the snapshots that matter."""
    writer = _writer(tmp_path)
    provider = FakeProvider(turns=[[("updates_a_row", {})], "done"])
    loop = _loop(on, writer, provider, _tool(name="updates_a_row", effect_class="idempotent_write"))
    session = await Session.create("test")
    async for _ in loop.run_turn(session, "update it"):
        pass

    run_id = writer.store.runs()[0].run_id
    assert [c.trigger for c in Checkpointer(writer).entries(run_id)] == ["turn_end"]
    writer.close()


async def test_nothing_is_checkpointed_while_the_feature_is_off(cfg, tmp_path) -> None:
    """The flag is the whole of the difference: with it off a turn writes exactly the
    journal it wrote before this session, and the cost of a checkpoint is not paid."""
    assert cfg.checkpoints.enabled is False  # the shipped default
    writer = _writer(tmp_path)
    provider = FakeProvider(turns=[[("sends_mail", {})], "sent it"])
    loop = _loop(cfg, writer, provider, _tool())
    session = await Session.create("test")
    async for _ in loop.run_turn(session, "send the mail"):
        pass

    run_id = writer.store.runs()[0].run_id
    assert Checkpointer(writer).entries(run_id) == []
    assert "checkpoint_written" not in {e.type for e in writer.store.read(run_id)}
    writer.close()


async def test_a_turn_the_consumer_walked_away_from_is_still_checkpointed(on, tmp_path) -> None:
    """Every way out of a turn reaches the boundary, including the one nobody writes code
    for: a consumer that stops iterating gets `GeneratorExit` thrown at the suspended yield.
    A run that ended like that, having already sent the mail, is exactly the run a resume is
    asked about."""
    writer = _writer(tmp_path)
    provider = FakeProvider(turns=[[("sends_mail", {})], "done"])
    loop = _loop(on, writer, provider, _tool())
    session = await Session.create("test")
    stream = loop.run_turn(session, "send it")
    await anext(stream)
    await stream.aclose()  # the consumer walks away mid-turn

    run_id = writer.store.runs()[0].run_id
    events = writer.store.read(run_id)
    assert events[-2].type == "agent_finished"
    assert events[-2].payload["status"] == "cancelled"
    assert [c.trigger for c in Checkpointer(writer).entries(run_id)] == [
        "pre_effect", "turn_end",
    ]
    writer.close()


async def test_a_failing_turn_keeps_its_own_error_when_the_boundary_cannot_write(
    on, tmp_path, monkeypatch
) -> None:
    """Session 4a left this as a decision owed to 4b (open question 3): the turn_end boundary
    sits inside `_TurnRecord.__exit__`, so a checkpoint that fails while the turn is already
    failing replaces the turn's exception.

    Decided in 4b: leave it there, and do not swallow it. Python chains the two - the turn's
    error survives as `__context__` and is printed under "during handling of the above
    exception" - so nothing is lost, and the alternative costs more than it buys: a boundary
    that cannot write is exactly the condition the *next* crash will be asked about, and a
    resume cannot report a checkpoint nobody knew had failed.
    """

    class Failing:
        name = "failing"

        async def stream(self, messages, tools=None, *, params):
            raise RuntimeError("the model host vanished")
            yield  # pragma: no cover - makes this an async generator

    def refuse(*args, **kwargs):
        raise CheckpointError("the journal file is read-only")

    monkeypatch.setattr("agentd.agent.loop.checkpoint_at", refuse)
    writer = _writer(tmp_path)
    loop = _loop(on, writer, Failing())
    session = await Session.create("test")
    with pytest.raises(CheckpointError) as exc:
        async for _ in loop.run_turn(session, "go"):
            pass

    assert isinstance(exc.value.__context__, RuntimeError)
    assert "the model host vanished" in str(exc.value.__context__)
    # And the turn is on disk however the boundary went: `agent_finished` is journaled
    # before the checkpoint is attempted, which is what `covers_seq` would have covered.
    last = writer.store.read(writer.store.runs()[0].run_id)[-1]
    assert last.type == "agent_finished" and last.payload["status"] == "failed"
    writer.close()


def test_a_detached_tool_call_gets_no_checkpoint(on, tmp_path) -> None:
    """A queued approval replayed long after its turn ended has no run to snapshot. The
    boundary declines rather than opening a run of its own."""
    writer = _writer(tmp_path)
    assert checkpoint_at("pre_effect", run_id=None, writer=writer) is None
    assert writer.store.query("SELECT * FROM checkpoint") == []
    writer.close()


# --- what the snapshot says about effects ------------------------------------


def test_an_unresolved_effect_is_named_in_the_snapshot(on, tmp_path) -> None:
    """The one thing a resume needs from the ledger: which calls were still open when the
    lights went out. An `unsafe_write` in this list may have happened."""
    writer = _writer(tmp_path)
    _started(RunJournal(writer, "run-1"))
    ledger = EffectLedger(writer)
    effect = ledger.intend(
        run_id="run-1", step_id="s1", tool="sends_mail", effect_class="unsafe_write",
        args={"to": "dyd2008@nyu.edu"},
    )
    effect.dispatched()  # and then nothing: the call never resolved
    cp = Checkpointer(writer).write("run-1", trigger="turn_end")

    assert cp.effects_cursor.effects == 1
    assert cp.effects_cursor.open_keys == (effect.key,)
    assert cp.effects_cursor.last_effect_seq > 0
    assert cp.effects_cursor.last_effect_seq <= cp.covers_seq

    effect.committed(result_ref="action:1", result="sent")
    later = Checkpointer(writer).write("run-1", trigger="manual")
    assert later.effects_cursor.open_keys == ()
    assert later.covers_seq > cp.covers_seq
    writer.close()


def test_the_latest_checkpoint_is_the_one_that_covers_most_of_the_run(on, tmp_path) -> None:
    """Where 4b's resume starts. Ordered by the position of the announcement, which is the
    only order the journal guarantees."""
    writer = _writer(tmp_path)
    rj = RunJournal(writer, "run-1")
    _started(rj)
    checkpointer = Checkpointer(writer)
    first = checkpointer.write("run-1", trigger="turn_end")
    rj.emit(
        "message_appended",
        {"role": "user", "actor": "user", "chars": 2, "preview": "go"},
    )
    second = checkpointer.write("run-1", trigger="manual")

    assert second.covers_seq > first.covers_seq
    assert checkpointer.latest("run-1") == second
    assert [c.checkpoint_id for c in checkpointer.entries("run-1")] == [
        first.checkpoint_id, second.checkpoint_id,
    ]
    assert checkpointer.latest("some-other-run") is None
    writer.close()


def test_a_snapshot_covers_the_tail_that_was_still_in_the_buffer(on, tmp_path) -> None:
    """The chatty event types are buffered and the age check runs on append, so without a
    flush first the snapshot would sit *behind* events it had already accounted for - and a
    resume would replay them. Costs one commit; it is the reason `write` flushes."""
    writer = _writer(tmp_path)
    rj = RunJournal(writer, "run-1")
    _started(rj)
    for _ in range(3):
        rj.emit(
            "message_appended",
            {"role": "user", "actor": "user", "chars": 2, "preview": "go"},
        )
    assert writer.pending > 0
    cp = Checkpointer(writer).write("run-1", trigger="turn_end")
    assert cp.covers_seq == 4
    assert cp.messages_ref.message_events == 3
    assert cp.event_seq == 5
    writer.close()


# --- the file ----------------------------------------------------------------


def test_the_checkpoint_table_arrives_by_migration_on_a_file_that_predates_it(
    tmp_path,
) -> None:
    """Migrations are additive and are applied one step at a time. A journal written before
    this session opens with its events intact and a new empty table beside them."""
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    for step in (1, 2):
        conn.executescript(MIGRATIONS[step])
        conn.execute(f"PRAGMA user_version={step}")
    conn.execute(
        "INSERT INTO journal (run_id, seq, ts, type, payload) VALUES ('run-1',1,'t','x','{}')"
    )
    conn.commit()
    conn.close()

    with JournalStore(path) as store:
        assert int(store.query("PRAGMA user_version")[0][0]) == SCHEMA_VERSION == 3
        assert store.count("run-1") == 1
        assert store.query("SELECT * FROM checkpoint") == []


def test_pruning_a_run_takes_its_checkpoints_with_it(on, tmp_path) -> None:
    """A run is entirely present or entirely gone. A checkpoint whose journal was pruned is
    an acceleration structure over nothing, and it is the one thing a resume would trust."""
    writer = _writer(tmp_path)
    _started(RunJournal(writer, "run-1"))
    Checkpointer(writer).write("run-1", trigger="turn_end")
    writer.store.prune(Retention(mode="max_age_days", max_age_days=0))

    assert writer.store.count("run-1") == 0
    assert writer.store.query("SELECT * FROM checkpoint") == []
    writer.close()


def test_the_overhead_of_each_boundary_is_counted(on, tmp_path) -> None:
    """Passes 2a, 2b and 2c each closed with "the fsync cost is unmeasured" and Pass 10 owes
    a number. A checkpoint counts its own cost rather than needing a second instrumentation
    path bolted on later."""
    writer = _writer(tmp_path)
    _started(RunJournal(writer, "run-1"))
    cps.OVERHEAD.clear()
    Checkpointer(writer).write("run-1", trigger="turn_end")
    Checkpointer(writer).write("run-1", trigger="pre_effect")
    Checkpointer(writer).write("run-1", trigger="pre_effect")

    assert cps.OVERHEAD["turn_end"]["n"] == 1
    assert cps.OVERHEAD["pre_effect"]["n"] == 2
    assert cps.OVERHEAD["pre_effect"]["total_ms"] > 0
    assert cps.OVERHEAD["pre_effect"]["bytes"] > 0
    cps.OVERHEAD.clear()
    writer.close()
