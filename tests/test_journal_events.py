"""Pass 2b: the event vocabulary, and the runtime writing it.

The exit criterion for this session is one sentence - "a complete run produces a journal
from which the sequence of what happened is readable without reference to any other source"
- and it has two halves that fail in different ways.

The first half is coverage: if a turn ends without saying how it ended, or a worker's events
land somewhere other than the run that created them, the journal is a log rather than a
history and `state = fold(reduce, journal, initial)` folds to the wrong answer. The turn
tests here therefore assert the *shape of the whole sequence*, not that individual events
exist.

The second half is the payload, and it is where this codebase's characteristic bug lives: a
field that degrades into a plausible NULL still folds, still passes `agent doctor`, and is
wrong. So the schema refuses a missing field, an unannounced `None`, a misspelled key and a
bool standing in for a count - and the eight types no pass writes yet are each constructed
here, so that Pass 3, 4 and 5 inherit a shape something has actually built rather than a
table in a document.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from agentd.agent.delegation import TaskSpec
from agentd.agent.loop import AgentLoop, Session
from agentd.agent.results import WorkerReport
from agentd.agent.stream import Answer
from agentd.agent.subagents import SubagentSpec, run_subagent
from agentd.journal import events as jevents
from agentd.journal.runtime import RunJournal
from agentd.journal.store import JournalStore
from agentd.journal.writer import JournalWriter
from agentd.llm.base import CallParams, Finish, LLMError, LLMEvent, TextDelta
from agentd.llm.fake import FakeProvider
from agentd.policy.approvals import AutoApprover
from agentd.policy.engine import engine_from_config
from agentd.tools.registry import Registry, build_registry

# The vocabulary exactly as docs/plans/pass-02-journal.md lists it, plus every later
# addition, each named with the pass that added it. Written out rather than derived, because
# the point of the assertion is that the code has not drifted from the passes: a type added
# without a line here is a type added without anybody deciding to.
PASS_VOCABULARY = {
    "agent_started", "agent_finished",
    "tool_requested", "tool_started", "tool_progress",
    "tool_finished", "tool_failed",
    "worker_created", "worker_finished",
    "handoff_started", "handoff_finished",
    "message_appended",
    "checkpoint_written",
    "effect_intended", "effect_committed",
    "run_resumed", "run_forked",
    # Session 6c, the run-scoped result cache. The journal is where it lives, because
    # `[checkpoints] enabled` is false in the shipped config and a cache kept only in a
    # checkpoint would be a cache this machine has never once written.
    "worker_result_cached", "worker_result_reused",
    # Session 7b, the working bucket. Same reasoning as the cache above, one bucket further
    # out: working memory is discarded with its run, so the journal is the only place it can
    # live and still be recoverable by re-folding.
    "working_memory_noted", "working_memory_discarded",
}


@pytest.fixture
def writer(tmp_path: Path) -> JournalWriter:
    w = JournalWriter(JournalStore(tmp_path / "events.db"))
    yield w
    w.close()


def _types(store: JournalStore, run_id: str) -> list[str]:
    return [e.type for e in store.read(run_id)]


def _registry(*names: str) -> Registry:
    from agentd.tools import builtin_fs, builtin_memory, builtin_web

    all_tools = {
        t.name: t for t in (*builtin_fs.TOOLS, *builtin_memory.TOOLS, *builtin_web.TOOLS)
    }
    reg = Registry()
    reg.add(*(all_tools[n] for n in names))
    return reg


# --- the vocabulary ---------------------------------------------------------


def test_the_vocabulary_is_exactly_the_one_the_pass_specified() -> None:
    assert jevents.EVENT_TYPES == PASS_VOCABULARY


def test_the_types_nothing_writes_yet_are_named_rather_than_left_implicit() -> None:
    """Which types are reachable today is data, so the gap cannot drift into prose only."""
    assert jevents.EMITTED_TYPES < jevents.EVENT_TYPES
    assert jevents.EVENT_TYPES - jevents.EMITTED_TYPES == {
        # `checkpoint_written` left this set in session 4a: `journal/checkpoints.py` writes
        # it at the five boundaries. `run_resumed` left it in 4b: `journal/resume.py` writes
        # one into the run it is picking up. `run_forked` left it in 4d: `journal/fork.py`
        # writes one as the first event of the run a rewind opens. The `handoff_*` pair left
        # it in 5b, written around one generation. What is left is a progress channel the
        # tool surface does not have.
        "tool_progress",
    }


def test_an_event_type_outside_the_vocabulary_is_refused_at_the_only_door(
    writer: JournalWriter,
) -> None:
    """`reduce` has no case for a type nobody declared, and a journal it cannot fold folds
    to the wrong state rather than to an error."""
    with pytest.raises(jevents.UnknownEventType):
        writer.append("run-1", "toool_started", {})
    assert writer.store.count("run-1") == 0


def test_a_missing_required_field_is_refused_rather_than_stored_as_absent(
    writer: JournalWriter,
) -> None:
    rj = RunJournal(writer, "run-1")
    with pytest.raises(jevents.EventSchemaError) as exc:
        rj.emit("tool_started", {"call_id": "c1"})
    assert "name" in str(exc.value)
    assert writer.store.count("run-1") == 0


def test_a_null_in_a_field_that_was_not_declared_nullable_is_refused(
    writer: JournalWriter,
) -> None:
    """The house bug: a real value degrading to a plausible NULL that still folds."""
    rj = RunJournal(writer, "run-1")
    with pytest.raises(jevents.EventSchemaError) as exc:
        rj.emit(
            "tool_finished",
            {
                "call_id": "c1", "name": "fs_read", "duration_ms": 4,
                "result_chars": None, "trust": "trusted",
            },
        )
    assert "not declared nullable" in str(exc.value)


def test_a_field_declared_nullable_accepts_a_stated_null(writer: JournalWriter) -> None:
    """Resuming with no checkpoint behind it is a real resume, and has to be sayable."""
    rj = RunJournal(writer, "run-1")
    rj.emit(
        "run_resumed",
        {
            "from_seq": 0, "checkpoint_id": None, "reason": "cold_fold",
            "replayed_events": 0, "uncertain_effects": [],
        },
        sync=True,
    )
    assert writer.store.read("run-1")[0].payload["checkpoint_id"] is None


def test_a_misspelled_field_does_not_ride_along_unvalidated(writer: JournalWriter) -> None:
    rj = RunJournal(writer, "run-1")
    with pytest.raises(jevents.EventSchemaError) as exc:
        rj.emit(
            "worker_finished",
            {
                "worker_id": "w1", "name": "researcher", "status": "completed",
                "answer_chars": 3, "report_valid": True, "tainted": False, "evidence": 0,
                "actions_taken": 0, "followups": 0,
                "candidate": 0, "candidates": 0, "tokens": 0, "duration_ms": 1,
            },
        )
    assert "'candidate' is not a field" in str(exc.value)


def test_a_bool_is_not_accepted_where_a_count_is_declared(writer: JournalWriter) -> None:
    """`True` is an `int` in Python and would fold as 1."""
    rj = RunJournal(writer, "run-1")
    with pytest.raises(jevents.EventSchemaError) as exc:
        rj.emit(
            "agent_finished",
            {
                "turn_id": "t", "status": "completed", "steps": True, "duration_ms": 1,
                "answer_chars": 0, "usage": {}, "usage_reported": False,
                "context_tokens": 0, "context_ceiling_tokens": 24000,
                "context_threshold_tokens": 8000, "context_crossed": False,
                "context_basis": "unmeasured",
            },
        )
    assert "expected int" in str(exc.value)


def test_a_status_outside_its_enum_is_refused(writer: JournalWriter) -> None:
    rj = RunJournal(writer, "run-1")
    with pytest.raises(jevents.EventSchemaError):
        rj.emit(
            "effect_committed",
            {
                "effect_id": "e1", "step_id": "s1", "idempotency_key": "k",
                "status": "probably_fine", "duration_ms": 2,
            },
        )


def test_the_types_later_passes_write_already_have_a_shape_that_validates(
    writer: JournalWriter,
) -> None:
    """Pass 3, 4 and 5 fill a slot rather than migrate a schema - so each slot is built
    here at least once, instead of being a table in a document nobody executed."""
    rj = RunJournal(writer, "run-1")
    rj.emit("tool_progress", {"call_id": "c1", "name": "shell_exec", "message": "1/3"})
    rj.emit(
        "effect_intended",
        {
            "effect_id": "e1", "step_id": "s1", "tool_name": "fs_write",
            "effect_class": "unsafe_write", "idempotency_key": "k", "args_digest": "d",
        },
    )
    rj.emit(
        "effect_committed",
        {
            "effect_id": "e1", "step_id": "s1", "idempotency_key": "k",
            "status": "uncertain", "duration_ms": 12, "error": "killed mid-write",
        },
    )
    rj.emit(
        "checkpoint_written",
        {
            "checkpoint_id": "ck1", "trigger": "turn_end", "covers_seq": 3,
            "memory_watermark": {"episodic": 10, "semantic": 4},
        },
    )
    rj.emit(
        "handoff_started",
        {"handoff_id": "h1", "reason": "context_limit", "messages": 40},
    )
    rj.emit(
        "handoff_finished",
        {
            "handoff_id": "h1", "status": "ok", "summary_chars": 900,
            "kept_messages": 4, "dropped_messages": 36, "duration_ms": 800,
        },
    )
    rj.emit(
        "run_resumed",
        {
            "from_seq": 6, "checkpoint_id": "ck1", "reason": "crash",
            "replayed_events": 2, "uncertain_effects": ["e1"],
        },
    )
    RunJournal(writer, "run-2").emit(
        "run_forked",
        {"parent_run_id": "run-1", "forked_from_seq": 7, "reason": "handoff"},
    )
    writer.flush()
    assert _types(writer.store, "run-1") == [
        "tool_progress", "effect_intended", "effect_committed", "checkpoint_written",
        "handoff_started", "handoff_finished", "run_resumed",
    ]
    assert _types(writer.store, "run-2") == ["run_forked"]


def test_an_effect_is_on_disk_before_the_side_effect_it_announces(
    writer: JournalWriter,
) -> None:
    """The durability class survived the vocabulary landing on top of it."""
    rj = RunJournal(writer, "run-1")
    assert rj.emit("message_appended", _msg()) is None
    written = rj.emit(
        "effect_intended",
        {
            "effect_id": "e1", "step_id": "s1", "tool_name": "fs_write",
            "effect_class": "unsafe_write", "idempotency_key": "k", "args_digest": "d",
        },
    )
    assert written is not None and written.seq == 2  # the buffered tail went with it
    assert writer.pending == 0


def test_a_step_id_of_one_worker_cannot_collide_with_its_callers() -> None:
    """Pass 3 hashes (run_id, step_id, tool, args): two different steps sharing a string
    would make two different calls look like one retry of the same one."""
    assert jevents.step_id(1) == "s1"
    assert jevents.step_id(1, worker_id="w-7") == "w-7.s1"
    assert jevents.step_id(1) != jevents.step_id(1, worker_id="w-7")


def _msg() -> dict[str, Any]:
    return {"role": "user", "actor": "user", "chars": 3, "preview": "hi!"}


# --- a whole turn -----------------------------------------------------------


async def test_a_complete_turn_is_readable_from_the_journal_alone(cfg, tmp_path):
    """The pass's exit criterion, asserted as a sequence rather than as a set."""
    target = cfg.paths.roots()[0] / "note.txt"
    target.write_text("the answer is 42")
    provider = FakeProvider(
        turns=[[("fs_read", {"path": str(target)})], "The file says 42."]
    )
    store = JournalStore(tmp_path / "turn.db")
    writer = JournalWriter(store)
    loop = AgentLoop(
        cfg=cfg, registry=_registry("fs_read"), engine=engine_from_config(cfg),
        approver=AutoApprover(True), provider=provider, journal=writer,
    )
    session = await Session.create("test")
    finished = [
        e
        async for e in loop.run_turn(session, "what does the note say?")
        if isinstance(e, Answer)
    ][0]

    events = store.read(finished.turn_id)
    assert [e.type for e in events] == [
        "agent_started",
        "message_appended",   # the user's question
        "message_appended",   # the system block that was assembled for it
        "message_appended",   # step 1: the assistant's tool call
        "tool_requested",
        "tool_started",
        # No `effect_intended` / `effect_committed` pair: session 3c classified `fs_read`
        # as `read`, and a read gets no ledger row because there is nothing a crash could
        # leave half-done. 3b's version of this test carried the pair and said in place
        # that it would go when 3c ruled. A `read` tool that starts journaling effects
        # again means somebody changed its class, which is the thing worth noticing here.
        "tool_finished",
        "message_appended",   # step 2: the answer
        "agent_finished",
    ]
    assert [e.seq for e in events] == list(range(1, 10))
    started, finished_event = events[0], events[-1]
    assert started.payload["turn_id"] == finished.turn_id
    assert started.payload["parent_turn_id"] is None
    assert finished_event.payload["status"] == "completed"
    assert finished_event.payload["steps"] == 2
    assert "42" in finished_event.payload["answer_preview"]
    by_type = {e.type: e.payload for e in events}
    call = by_type["tool_requested"]
    assert call["name"] == "fs_read" and call["visible"] is True and call["known"] is True
    assert by_type["tool_finished"]["trust"] == "trusted"
    assert by_type["tool_finished"]["result_chars"] > 0
    # Every event of a step says which step it was, which is what lets an effect the
    # ledger recorded be tied back to the call that caused it.
    assert {e.payload["step_id"] for e in events[3:7]} == {"s1"}
    assert "effect_intended" not in by_type and "effect_committed" not in by_type
    writer.close()


async def test_the_turns_last_event_is_on_disk_when_the_turn_ends(cfg, tmp_path):
    """There is no timer thread: a buffered tail sits in memory until the next append. A
    run that has genuinely stopped must not look half-written to whoever reads it next."""
    provider = FakeProvider(turns=["done"])
    store = JournalStore(tmp_path / "turn.db")
    writer = JournalWriter(store)
    loop = AgentLoop(
        cfg=cfg, registry=Registry(), engine=engine_from_config(cfg),
        approver=AutoApprover(True), provider=provider, journal=writer,
    )
    session = await Session.create("test")
    async for _ in loop.run_turn(session, "hello"):
        pass
    assert writer.pending == 0
    assert _types(store, str(store.runs()[0].run_id))[-1] == "agent_finished"
    writer.close()


async def test_a_turn_that_the_model_kills_still_says_how_it_ended(cfg, tmp_path):
    """The budget-exhaustion crash from the Pass 1 baseline arrives here as an LLMError.
    The bug is untouched by this pass; what must not happen is a run whose journal simply
    stops, because that is the case the durability spine exists for."""

    class Failing:
        name = "failing"

        async def stream(self, messages, tools=None, *, params) -> AsyncIterator[LLMEvent]:
            raise LLMError("HTTP 400 System message must be at the beginning.")
            yield  # pragma: no cover - makes this an async generator

    store = JournalStore(tmp_path / "turn.db")
    writer = JournalWriter(store)
    loop = AgentLoop(
        cfg=cfg, registry=Registry(), engine=engine_from_config(cfg),
        approver=AutoApprover(True), provider=Failing(), journal=writer,
    )
    session = await Session.create("test")
    async for _ in loop.run_turn(session, "go"):
        pass

    last = store.read(store.runs()[0].run_id)[-1]
    assert last.type == "agent_finished"
    assert last.payload["status"] == "failed"
    assert "System message must be at the beginning" in last.payload["error"]
    assert last.payload["answer_chars"] == 0
    writer.close()


async def test_a_consumer_that_walks_away_mid_turn_still_closes_the_run(cfg, tmp_path):
    """Nothing writes the telemetry record or the `actions` row when a turn's event stream
    is abandoned (Pass 1, open question 4). The journal must not have the same hole, or the
    fold sees a run that started and never ended and cannot tell that from one still
    running."""
    target = cfg.paths.roots()[0] / "note.txt"
    target.write_text("x")
    provider = FakeProvider(turns=[[("fs_read", {"path": str(target)})], "done"])
    store = JournalStore(tmp_path / "turn.db")
    writer = JournalWriter(store)
    loop = AgentLoop(
        cfg=cfg, registry=_registry("fs_read"), engine=engine_from_config(cfg),
        approver=AutoApprover(True), provider=provider, journal=writer,
    )
    session = await Session.create("test")
    stream = loop.run_turn(session, "read it")
    await anext(stream)  # take one event, then walk away
    await stream.aclose()

    events = store.read(store.runs()[0].run_id)
    assert events[0].type == "agent_started"
    assert events[-1].type == "agent_finished"
    assert events[-1].payload["status"] == "cancelled"
    writer.close()


async def test_a_denied_call_records_the_rule_that_denied_it(cfg, tmp_path):
    """A denial is a failed call, not a failed tool - and "why" has to come off the result
    rather than out of a JSON body we would have to re-parse to find it."""
    target = cfg.paths.workspace / "out.txt"
    provider = FakeProvider(
        turns=[
            [("fs_write", {"path": str(target), "content": "x", "reason": "because"})],
            "Understood.",
        ]
    )
    store = JournalStore(tmp_path / "turn.db")
    writer = JournalWriter(store)
    loop = AgentLoop(
        cfg=cfg, registry=_registry("fs_write"), engine=engine_from_config(cfg),
        approver=AutoApprover(approve=False), provider=provider, journal=writer,
    )
    session = await Session.create("test")
    async for _ in loop.run_turn(session, "write it", autonomy="assist"):
        pass

    failed = [e for e in store.read(store.runs()[0].run_id) if e.type == "tool_failed"]
    assert len(failed) == 1
    assert failed[0].payload["denied"] is True
    assert failed[0].payload["invalid_args"] is False
    assert failed[0].payload["rule"]
    assert not target.exists()
    writer.close()


async def test_an_unmeasured_turn_is_not_journaled_as_a_free_one(cfg, tmp_path):
    """The router drops the usage chunk on streamed calls, so zero tokens is routinely "not
    measured" rather than "nothing spent". Recording the zeros without saying which one it
    was is how a cost comparison gets made against numbers nobody measured."""

    class Silent:
        name = "silent"

        async def stream(self, messages, tools=None, *, params: CallParams):
            yield TextDelta(text="hi")
            yield Finish(reason="stop", usage={})

    store = JournalStore(tmp_path / "turn.db")
    writer = JournalWriter(store)
    loop = AgentLoop(
        cfg=cfg, registry=Registry(), engine=engine_from_config(cfg),
        approver=AutoApprover(True), provider=Silent(), journal=writer,
    )
    session = await Session.create("test")
    async for _ in loop.run_turn(session, "hello"):
        pass

    last = store.read(store.runs()[0].run_id)[-1]
    assert last.payload["usage"] == {"input_tokens": 0, "output_tokens": 0}
    assert last.payload["usage_reported"] is False
    writer.close()


# --- workers ----------------------------------------------------------------


async def test_a_workers_events_belong_to_the_run_that_created_it(cfg, tmp_path):
    """A worker is part of the work, not a separate history. If its events opened a run of
    their own, a fold of the caller's run would show a gap where the delegation happened."""
    provider = FakeProvider(
        turns=["I looked and found it."],
        json_results=[WorkerReport(status="completed", answer="Found it.", evidence=["file://x"])],
    )
    store = JournalStore(tmp_path / "turn.db")
    writer = JournalWriter(store)
    session = await Session.create("test")
    spec = SubagentSpec(
        name="researcher", prompt="be useful", tool_names=["fs_read"], max_steps=3,
    )
    await run_subagent(
        spec, TaskSpec("researcher", "find the thing"), parent_session_id=session.id,
        parent_turn_id=session.id,
        parent_autonomy="assist", approver=AutoApprover(True), registry=build_registry(),
        cfg=cfg, provider=provider, parent_run_id="run-outer", parent_step_id="s2",
        journal=writer,
    )
    writer.flush()

    assert [r.run_id for r in store.runs()] == ["run-outer"]
    events = store.read("run-outer")
    assert [e.type for e in events] == [
        "worker_created",
        "agent_started",
        "message_appended",  # the task it was given
        "message_appended",  # its own system block
        "message_appended",  # its answer
        "agent_finished",
        "worker_finished",
        # Session 6c: the result, cached for the rest of this run, tagged with the same
        # worker so the entry can be traced back to what earned it.
        "worker_result_cached",
    ]
    worker_id = events[0].payload["worker_id"]
    assert all(e.payload["worker_id"] == worker_id for e in events)
    assert events[0].payload["parent_step_id"] == "s2"
    assert events[0].payload["tools"] == ["fs_read"]
    assert events[1].payload["parent_turn_id"] == str(session.id)
    finished = next(e for e in events if e.type == "worker_finished")
    assert finished.payload["status"] == "completed"
    assert finished.payload["evidence"] == 1
    assert finished.payload["report_valid"] is True
    # The worker's own step ids are scoped by its worker id, so step 1 of the worker and
    # step 1 of its caller are different strings in the same run.
    assert events[4].payload["step_id"] == jevents.step_id(1, worker_id=worker_id)
    writer.close()


async def test_a_worker_without_a_run_lands_in_its_callers_turn(cfg, tmp_path):
    """The fallback is the same derivation a top-level turn uses, so a caller that does not
    thread `parent_run_id` still writes into the run its turn opened - never into a new one
    nothing points at."""
    provider = FakeProvider(
        turns=["done"], json_results=[WorkerReport(status="completed", answer="ok")]
    )
    store = JournalStore(tmp_path / "turn.db")
    writer = JournalWriter(store)
    session = await Session.create("test")
    await run_subagent(
        SubagentSpec(name="researcher", prompt="p", tool_names=["fs_read"], max_steps=2),
        TaskSpec("researcher", "task"), parent_session_id=session.id, parent_turn_id=session.id,
        parent_autonomy="assist", approver=AutoApprover(True), registry=build_registry(),
        cfg=cfg, provider=provider, journal=writer,
    )
    writer.flush()
    assert [r.run_id for r in store.runs()] == [str(session.id)]
    writer.close()
