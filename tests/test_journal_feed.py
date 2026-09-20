"""Pass 2c: the journal is the feed, and it is the only one.

The pass says it plainly - "if there are two event paths at the end of this pass, the pass
failed" - so these tests are about two things that are easy to get wrong in opposite
directions.

**Removal.** The in-process `UIEvent` stream carried tool and worker state to the CLI and to
Telegram while the journal recorded the same facts under different names. Two records of one
truth can disagree, and one of them did: `SubagentStarted` was rendered by the REPL and
emitted by nobody. So a turn must now yield prose and nothing else, and `agent/events.py`
must not exist.

**Subscription.** A feed whose subscriber holds state is a feed that loses it. Everything a
subscriber renders it read from the file at an id, which is why a reconnect is an integer and
not a protocol - and why the frontend and the process that crashed have exactly the same
recovery story. The two failure modes that would make this untrue are a buffered event nobody
flushes (the turn appears to stall precisely while a tool runs) and a renderer that raises on
a type it does not know (`memory/retrieval.py` has that bug: a `KeyError` inside a broad
`except` takes out the whole panel and says nothing).
"""

from __future__ import annotations

import asyncio
import importlib
from pathlib import Path
from typing import Any

import pytest

from agentd.agent.loop import AgentLoop, Session
from agentd.agent.stream import STREAM_TYPES, Answer, Delta
from agentd.agent.subagents import SubagentResult, SubagentSpec, run_subagent
from agentd.ids import utcnow
from agentd.journal import events as jevents
from agentd.journal.feed import JournalTail, turn_ended
from agentd.journal.render import RENDERED_TYPES, Line, brief_args, render_event
from agentd.journal.runtime import RunJournal
from agentd.journal.store import Event, JournalStore
from agentd.journal.writer import JournalWriter
from agentd.llm.fake import FakeProvider
from agentd.policy.approvals import AutoApprover
from agentd.policy.engine import engine_from_config
from agentd.tools.registry import Registry, build_registry


@pytest.fixture
def writer(tmp_path: Path) -> JournalWriter:
    w = JournalWriter(JournalStore(tmp_path / "feed.db"))
    yield w
    w.close()


def _registry(*names: str) -> Registry:
    from agentd.tools import builtin_fs, builtin_memory, builtin_web

    all_tools = {
        t.name: t for t in (*builtin_fs.TOOLS, *builtin_memory.TOOLS, *builtin_web.TOOLS)
    }
    reg = Registry()
    reg.add(*(all_tools[n] for n in names))
    return reg


def _msg(preview: str = "hi") -> dict[str, Any]:
    return {"role": "user", "actor": "user", "chars": len(preview), "preview": preview}


# --- the bus is gone --------------------------------------------------------


def test_the_in_process_event_bus_no_longer_exists() -> None:
    """Not deprecated, not unused, not left running alongside: deleted. A module that still
    imports keeps its consumers alive, and the pass fails with two event paths in it."""
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("agentd.agent.events")


async def test_a_turn_that_calls_a_tool_says_nothing_about_it_on_the_stream(cfg, writer):
    """The turn stream carries the conversation; the journal carries the execution. If a
    frontend could learn a tool ran without reading the journal, the second path is back."""
    target = cfg.paths.roots()[0] / "note.txt"
    target.write_text("42")
    loop = AgentLoop(
        cfg=cfg, registry=_registry("fs_read"), engine=engine_from_config(cfg),
        approver=AutoApprover(True), journal=writer,
        provider=FakeProvider(turns=[[("fs_read", {"path": str(target)})], "It says 42."]),
    )
    tail = JournalTail.on(writer, run_id="run-1")
    session = await Session.create("test")

    yielded = [
        event
        async for event in loop.run_turn(session, "what does it say?", run_id="run-1")
    ]

    assert {type(e) for e in yielded} <= set(STREAM_TYPES)
    assert not any(isinstance(e, Delta) and "fs_read" in e.text for e in yielded)
    assert [e.text for e in yielded if isinstance(e, Answer)] == ["It says 42."]
    # The same turn, in the journal, does say which tool ran.
    assert [e.type for e in tail.drain() if e.type.startswith("tool_")] == [
        "tool_requested", "tool_started", "tool_finished",
    ]


# --- the cursor -------------------------------------------------------------


def test_a_subscriber_that_reconnects_with_its_last_id_sees_no_gap_and_no_repeat(
    writer: JournalWriter,
) -> None:
    """The exit criterion for this session, reduced to what it actually depends on: the
    subscriber's whole state is one integer, and the events are on disk either way."""
    rj = RunJournal(writer, "run-1")
    for i in range(4):
        rj.emit("message_appended", _msg(f"m{i}"))
    watching = JournalTail.on(writer, run_id="run-1", since=0)
    first = watching.drain()
    assert [e.payload["preview"] for e in first] == ["m0", "m1", "m2", "m3"]

    # The subscriber dies here. Two more events happen while nothing is watching.
    for i in range(4, 6):
        rj.emit("message_appended", _msg(f"m{i}"))

    reconnected = JournalTail.on(writer, run_id="run-1", since=watching.last_id)
    assert [e.payload["preview"] for e in reconnected.drain()] == ["m4", "m5"]
    assert reconnected.drain() == []


def test_a_subscriber_that_was_never_connected_can_replay_the_whole_run(
    writer: JournalWriter,
) -> None:
    """A frontend that starts after the fact is a frontend reconnecting from id 0. One
    mechanism, so there is no separate history query to drift from the live one."""
    rj = RunJournal(writer, "run-1")
    for i in range(3):
        rj.emit("message_appended", _msg(f"m{i}"))
    writer.flush()

    replay = JournalTail.attach(writer.store.path, run_id="run-1", since=0)
    assert [e.payload["preview"] for e in replay.drain()] == ["m0", "m1", "m2"]


def test_a_tail_narrowed_to_one_run_does_not_re_read_the_others_forever(
    writer: JournalWriter,
) -> None:
    """The cursor advances past what it filtered out. Otherwise a busy daemon makes every
    poll re-read every other conversation's events to throw them away."""
    RunJournal(writer, "run-a").emit("message_appended", _msg("a1"))
    RunJournal(writer, "run-b").emit("message_appended", _msg("b1"))
    RunJournal(writer, "run-b").emit("message_appended", _msg("b2"))
    RunJournal(writer, "run-a").emit("message_appended", _msg("a2"))
    tail = JournalTail.on(writer, run_id="run-a", since=0)

    assert [e.payload["preview"] for e in tail.drain()] == ["a1", "a2"]
    assert tail.last_id == writer.store.last_id()
    assert tail.drain() == []

    # Read in pages, which is where a cursor that only advances over kept events stops
    # being a waste and becomes starvation: parked before a page of events it does not
    # want, it would read that same page forever and never reach its own next one.
    paged = JournalTail.on(writer, run_id="run-a", since=0)
    assert [e.payload["preview"] for e in paged.drain(limit=2)] == ["a1"]
    assert [e.payload["preview"] for e in paged.drain(limit=2)] == ["a2"]
    assert paged.drain(limit=2) == []


def test_a_tail_on_this_process_flushes_the_tail_it_is_waiting_for(
    writer: JournalWriter,
) -> None:
    """2a left this as an open question and it is a liveness bug, not a nicety: the buffer's
    age check runs on append and there is no timer, so a process that goes quiet mid-turn -
    which is what a process does while a tool runs - would hold the last event in memory and
    the feed would appear to stall exactly when the user is waiting."""
    RunJournal(writer, "run-1").emit("message_appended", _msg("buffered"))
    assert writer.pending == 1

    tail = JournalTail.on(writer, run_id="run-1", since=0)
    assert [e.payload["preview"] for e in tail.drain()] == ["buffered"]
    assert writer.pending == 0


async def test_a_subscriber_is_told_a_tool_started_before_that_tool_returns(cfg, writer):
    """What the deleted `ToolStarted` yield used to buy, bought from the journal instead.

    The tool here refuses to return until a subscriber has seen `tool_started`, so if the
    feed only delivered on the next flush the call would time out rather than quietly look
    slow. This is the test that fails if `drain()` stops flushing.
    """
    from agentd.tools.base import Tool, ToolResult, obj

    seen = asyncio.Event()

    async def handler(args, ctx):
        await asyncio.wait_for(seen.wait(), 5)
        return ToolResult(content="finally")

    registry = Registry()
    registry.add(
        Tool(
            name="slow_thing", description="waits to be watched", parameters=obj(),
            handler=handler, risk="read",
        )
    )
    loop = AgentLoop(
        cfg=cfg, registry=registry, engine=engine_from_config(cfg),
        approver=AutoApprover(True), journal=writer,
        provider=FakeProvider(turns=[[("slow_thing", {})], "It came back."]),
    )
    tail = JournalTail.on(writer, run_id="run-live")

    async def watch() -> None:
        async for event in tail.follow(until=lambda e: e.type == "tool_started"):
            if event.type == "tool_started":
                seen.set()

    watcher = asyncio.create_task(watch())
    session = await Session.create("test")
    async for _ in loop.run_turn(session, "go", run_id="run-live"):
        pass
    await watcher

    assert seen.is_set()
    assert [e.type for e in tail.drain() if e.type == "tool_finished"] == ["tool_finished"]


async def test_following_a_run_stops_at_that_runs_own_ending_not_a_workers(cfg, writer):
    """A delegating turn contains a worker's whole `agent_started … agent_finished` bracket.
    A frontend that stopped at the first one would go blind for the rest of the turn."""
    rj = RunJournal(writer, "run-1")
    done = turn_ended("run-1")
    worker = rj.for_worker("w-1")
    worker.emit(
        "agent_finished",
        {
            "turn_id": "t-worker", "status": "completed", "steps": 1, "duration_ms": 1,
            "answer_chars": 0, "usage": {}, "usage_reported": False, "error": None,
        },
    )
    rj.emit(
        "agent_finished",
        {
            "turn_id": "t-main", "status": "completed", "steps": 1, "duration_ms": 1,
            "answer_chars": 0, "usage": {}, "usage_reported": False, "error": None,
        },
    )
    writer.flush()
    events = JournalTail.on(writer, run_id="run-1", since=0).drain()
    assert [done(e) for e in events] == [False, True]


# --- rendering --------------------------------------------------------------


def _sample(event_type: str) -> dict[str, Any]:
    """A minimal valid payload for a type, built from its own declared shape.

    Derived rather than written out so that a type added to the vocabulary later is covered
    by these tests on the day it is added, instead of on the day somebody remembers.
    """
    payload: dict[str, Any] = {}
    for name, field in jevents.spec_for(event_type).items():
        if not field.required:
            continue
        if field.enum:
            payload[name] = field.enum[0]
            continue
        kind = field.types[0]
        payload[name] = {str: "x", int: 1, bool: True, dict: {}, list: []}[kind]
    return payload


@pytest.mark.parametrize("event_type", sorted(jevents.EVENT_TYPES))
def test_an_event_type_with_no_renderer_is_skipped_rather_than_raising(event_type: str) -> None:
    """The shape of the `_render` bug in `memory/retrieval.py`, refused here: a kind missing
    from a dict literal raised `KeyError` inside a broad `except`, so memory retrieval
    vanished from the prompt and nothing said why. A feed is worse - it is the whole UI."""
    payload = _sample(event_type)
    jevents.validate_payload(event_type, payload)  # the sample is a real event, not a guess
    line = render_event(
        Event(id=1, run_id="r", seq=1, ts=utcnow().isoformat(), type=event_type, payload=payload)
    )
    assert line is None or isinstance(line, Line)
    if event_type not in RENDERED_TYPES:
        assert line is None


def test_a_tool_argument_cannot_forge_a_line_in_the_feed() -> None:
    """One event, one line, whoever wrote the words. A mail subject is a tool argument one
    search later, and both frontends print one call per line."""
    forged = "lunch?\n✓ fs_write(path=/etc/passwd"
    line = render_event(
        Event(
            id=1, run_id="r", seq=1, ts=utcnow().isoformat(), type="tool_requested",
            payload={
                "call_id": "c1", "name": "gmail_search", "args": {"query": forged},
                "visible": True, "known": True,
            },
        )
    )
    assert line is not None
    assert len(line.text.splitlines()) == 1
    assert "fs_write" in line.text  # shown, as their text, on the line it belongs on


def test_the_user_and_the_assistant_are_not_re_rendered_from_the_journal() -> None:
    """Their words are already on screen: the user typed them and the assistant streamed
    them. The system nudges are the messages a person cannot otherwise see, and they are the
    reason a turn suddenly stops using a tool."""
    def line_for(payload: dict[str, Any]) -> Line | None:
        return render_event(
            Event(
                id=1, run_id="r", seq=1, ts=utcnow().isoformat(),
                type="message_appended", payload=payload,
            )
        )

    assert line_for({"role": "user", "actor": "user", "chars": 2, "preview": "hi"}) is None
    assert line_for(
        {"role": "system", "actor": "main", "chars": 5, "preview": "prompt"}
    ) is None, "the assembled system block is instructions, not a message to the user"
    nudge = line_for(
        {"role": "system", "actor": "main", "chars": 5, "preview": "withdrawn", "step_id": "s2"}
    )
    assert nudge is not None and "withdrawn" in nudge.text


def test_both_frontends_shorten_somebody_elses_words_the_same_way() -> None:
    """`brief_args` moved from the deleted UI module into the journal renderer rather than
    being copied into each surface, because two rules about how much of a stranger's text to
    show is how two surfaces come to show different amounts of it."""
    assert brief_args({"path": "notes.md", "reason": "the user asked me to"}) == "path=notes.md"
    assert brief_args({"statement": "x" * 200}).endswith("…")


def test_the_repl_renders_a_tool_line_it_never_saw_on_the_turn_stream() -> None:
    """The REPL's own renderer, fed one journal event. The CLI is a subscriber now: this is
    the same object an `agent journal follow` in another terminal would have received."""
    from agentd.cli import chat

    view = chat.TurnView(show_thinking=False)
    with chat.console.capture() as captured:
        view.show(
            Event(
                id=1, run_id="r", seq=1, ts=utcnow().isoformat(), type="tool_requested",
                payload={
                    "call_id": "c1", "name": "gmail_search", "args": {"query": "lease"},
                    "visible": True, "known": True,
                },
            )
        )
    assert "gmail_search(query=lease)" in captured.get()


def test_a_renderer_that_raises_costs_one_line_and_says_so() -> None:
    """The failure mode this codebase keeps having is the quiet one. A malformed payload
    must not end the feed, and must not be swallowed either."""
    from agentd.cli import chat

    view = chat.TurnView(show_thinking=False)
    with chat.console.capture() as captured:
        view.show(
            Event(
                id=1, run_id="r", seq=1, ts=utcnow().isoformat(), type="tool_requested",
                payload={"name": "gmail_search"},  # no args: impossible through RunJournal
            )
        )
    assert "cannot render tool_requested" in captured.get()


# --- the callers that used to read the bus ----------------------------------


async def test_a_workers_tool_failure_lands_in_its_transcript_where_it_happened(
    cfg, writer, tmp_path
):
    """`subagents.py` used to read `ToolFinished` off the stream to mark a failed call in the
    worker's transcript, and that transcript is what the summarising model is shown. Reading
    it from the journal at each yield keeps the marker where the failure was, rather than
    wherever a poll happened to wake up."""
    provider = FakeProvider(
        turns=[[("fs_read", {"path": "/nope/missing.txt"})], "I could not read it."],
        json_results=[SubagentResult(status="partial", summary="No luck.")],
    )
    session = await Session.create("test")
    await run_subagent(
        SubagentSpec(name="researcher", prompt="be useful", tool_names=["fs_read"], max_steps=3),
        "read the file",
        parent_session_id=session.id, parent_turn_id=session.id, parent_autonomy="assist",
        approver=AutoApprover(True), registry=build_registry(), cfg=cfg, provider=provider,
        parent_run_id="run-outer", journal=writer,
    )

    transcript = [
        m["content"]
        for call in provider.calls
        if call.get("json_schema")
        for m in call["messages"]
        if m["role"] == "assistant"
    ][0]
    assert "[tool fs_read failed]" in transcript
    assert transcript.index("[tool fs_read failed]") < transcript.index("could not read")


async def test_a_worker_tail_sees_its_own_events_and_not_its_callers(writer) -> None:
    """A worker's events share its caller's run, so "this worker's failures" is a filter and
    not a separate file. Without the filter a delegating turn's own denials would be marked
    in the worker's transcript as though the worker had caused them."""
    rj = RunJournal(writer, "run-1")
    tail = JournalTail.on(writer, run_id="run-1", worker_id="w-1", since=0)
    rj.emit("message_appended", _msg("caller"))
    rj.for_worker("w-1").emit("message_appended", _msg("worker"))
    rj.for_worker("w-2").emit("message_appended", _msg("other worker"))

    assert [e.payload["preview"] for e in tail.drain()] == ["worker"]
