"""Pass 1 baseline telemetry: does one task produce one complete record.

The exit criterion for session 1a is "running any task produces a complete record", so the
first test here is a field-by-field check against the list in the pass file rather than a
spot check. The rest pin the classifications that the later passes are judged by, and the
one property that matters more than any number: a turn must survive telemetry failing.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import pytest

from agentd.agent.loop import AgentLoop, Session
from agentd.agent.stream import Answer
from agentd.llm.base import CallParams, Finish, LLMError, LLMEvent
from agentd.llm.fake import FakeProvider
from agentd.obs import telemetry
from agentd.policy.approvals import AutoApprover
from agentd.policy.engine import engine_from_config
from agentd.tools.registry import Registry


def _registry(*names: str) -> Registry:
    from agentd.tools import builtin_fs, builtin_memory, builtin_web

    all_tools = {
        t.name: t for t in (*builtin_fs.TOOLS, *builtin_memory.TOOLS, *builtin_web.TOOLS)
    }
    reg = Registry()
    reg.add(*(all_tools[n] for n in names))
    return reg


def _loop(cfg, provider, *tools: str, approve: bool = True) -> AgentLoop:
    return AgentLoop(
        cfg=cfg, registry=_registry(*tools), engine=engine_from_config(cfg),
        approver=AutoApprover(approve), provider=provider,
    )


async def _run(loop: AgentLoop, session: Session, text: str, **kwargs) -> list:
    return [event async for event in loop.run_turn(session, text, **kwargs)]


def _records(cfg) -> list[dict[str, Any]]:
    return telemetry.read_records(telemetry.default_path(cfg))


def _only(cfg) -> dict[str, Any]:
    rows = _records(cfg)
    assert len(rows) == 1, rows
    return rows[0]


# --- the exit criterion ------------------------------------------------------


async def test_a_task_produces_one_complete_record(cfg):
    """Every field session 1a names, present and populated, for an ordinary tool-using turn."""
    target = cfg.paths.roots()[0] / "note.txt"
    target.write_text("the answer is 42")
    provider = FakeProvider(turns=[[("fs_read", {"path": str(target)})], "The file says 42."])
    session = await Session.create("test")
    events = await _run(_loop(cfg, provider, "fs_read", "fs_list"), session, "what does it say?")

    row = _only(cfg)
    turn_id = [e for e in events if isinstance(e, Answer)][0].turn_id
    assert row["request_id"] == turn_id
    assert row["session_id"] == str(session.id)
    assert row["status"] == "completed"
    assert row["tools"]["called"] == ["fs_read"]
    assert row["tools"]["call_count"] == 1
    assert row["tools"]["visible_unused"] == ["fs_list"]
    assert row["tool_selection_failures"]["count"] == 0
    assert row["context_tokens"]["start"] > 0
    assert row["context_tokens"]["peak"] >= row["context_tokens"]["start"]
    assert row["latency_ms"] >= 0
    assert row["usage"] == {"input_tokens": 20, "output_tokens": 10, "reported": True}
    assert row["model"] == cfg.llm.model
    assert row["role"] == "main"
    assert row["answer_chars"] == len("The file says 42.")


async def test_each_turn_appends_one_line(cfg):
    session = await Session.create("test")
    for _ in range(3):
        await _run(_loop(cfg, FakeProvider(turns=["Hello."]), "fs_read"), session, "hi")

    path = telemetry.default_path(cfg)
    lines = [line for line in path.read_text().splitlines() if line.strip()]
    assert len(lines) == 3
    assert {json.loads(line)["request_id"] for line in lines}.__len__() == 3


# --- the numbers the later passes are judged by ------------------------------


async def test_context_tokens_grow_across_a_tool_using_turn(cfg):
    """Start is the first call's context, end is the assembled context the turn left behind.

    A tool result is appended to the same message list, so end must exceed start. If these
    two are ever equal on a turn that called a tool, the sampling is being taken from a copy.
    """
    target = cfg.paths.roots()[0] / "big.txt"
    target.write_text("y" * 4000)
    provider = FakeProvider(turns=[[("fs_read", {"path": str(target)})], "Read it."])
    session = await Session.create("test")
    await _run(_loop(cfg, provider, "fs_read"), session, "read the big file")

    tokens = _only(cfg)["context_tokens"]
    assert tokens["start"] < tokens["end"] <= tokens["peak"]


async def test_an_unused_visible_tool_is_named_not_just_counted(cfg):
    provider = FakeProvider(turns=["No tools needed."])
    session = await Session.create("test")
    await _run(_loop(cfg, provider, "fs_read", "fs_list", "web_search"), session, "just answer")

    tools = _only(cfg)["tools"]
    assert tools["called"] == []
    assert set(tools["visible_unused"]) == {"fs_read", "fs_list", "web_search"}
    assert tools["registry_size"] == 3


async def test_an_invented_tool_name_is_a_selection_failure(cfg):
    provider = FakeProvider(turns=[[("no_such_tool", {})], "That tool does not exist."])
    session = await Session.create("test")
    await _run(_loop(cfg, provider, "fs_read"), session, "do a thing")

    failures = _only(cfg)["tool_selection_failures"]
    assert failures["by_kind"]["unknown_tool"] == 1
    assert failures["events"][0]["tool"] == "no_such_tool"


async def test_arguments_rejected_before_the_handler_are_a_selection_failure(cfg):
    provider = FakeProvider(turns=[[("fs_read", {})], "I need a path."])
    session = await Session.create("test")
    await _run(_loop(cfg, provider, "fs_read"), session, "read something")

    row = _only(cfg)
    assert row["tool_selection_failures"]["by_kind"]["invalid_args"] == 1
    assert row["tools"]["calls"][0]["ok"] is False
    assert row["tools"]["calls"][0]["invalid_args"] is True


async def test_reaching_for_a_withdrawn_tool_is_recorded_as_not_visible(cfg):
    """The loop withdraws a tool that keeps rejecting the same arguments, so a later call to
    it is a call to something the model was no longer being shown. That is the one signal
    Pass 9 is trying to move, and it has to survive the loop re-adding the tool to `exposed`
    on the way past - which is why `visible` is read before that line, not after it."""
    provider = FakeProvider(
        turns=[[("fs_read", {})], [("fs_read", {})], [("fs_read", {})], "I give up."]
    )
    session = await Session.create("test")
    await _run(_loop(cfg, provider, "fs_read"), session, "read something")

    row = _only(cfg)
    visibility = [call["visible"] for call in row["tools"]["calls"]]
    assert visibility == [True, True, False]
    assert row["tool_selection_failures"]["by_kind"]["not_visible"] == 1


async def test_a_denied_call_is_recorded_as_denied_not_as_a_bad_choice(cfg):
    """Policy refusing a write says nothing about whether the right tool was picked.
    Counting it as a selection failure would make the baseline move when the policy does."""
    target = cfg.paths.workspace / "out.txt"
    provider = FakeProvider(
        turns=[[("fs_write", {"path": str(target), "content": "x", "reason": "asked"})], "Ok."]
    )
    session = await Session.create("test")
    await _run(_loop(cfg, provider, "fs_write", approve=False), session, "write it")

    row = _only(cfg)
    assert row["tools"]["calls"][0]["denied"] is True
    assert row["tool_selection_failures"]["by_kind"].get("switched_after") is None
    assert row["tool_selection_failures"]["by_kind"].get("unknown_tool") is None


def _collector(base_config) -> telemetry.TurnTelemetry:
    return telemetry.TurnTelemetry.start(
        cfg=base_config, request_id="r", session_id="s", role="main", actor="main",
        origin="test", channel="cli", autonomy="assist", model="m",
    )


def test_a_failure_followed_by_a_different_tool_counts_as_a_retry_elsewhere(base_config):
    """The pass's "retry with a different tool", classified on its own so 1c can report it
    apart from the kinds that are unambiguous."""
    tele = _collector(base_config)
    tele.tool_call(telemetry.CallRecord(step=1, name="web_search", ok=False))
    tele.tool_call(telemetry.CallRecord(step=2, name="fs_search", ok=True))
    assert tele.selection_failures()["by_kind"] == {"switched_after": 1}

    same = _collector(base_config)
    same.tool_call(telemetry.CallRecord(step=1, name="web_search", ok=False))
    same.tool_call(telemetry.CallRecord(step=2, name="web_search", ok=True))
    assert same.selection_failures()["count"] == 0  # retrying the same tool is not a switch


# --- final status ------------------------------------------------------------


async def test_running_out_of_steps_is_abandoned_not_completed(cfg):
    small = cfg.model_copy(update={"agent": cfg.agent.model_copy(update={"max_steps": 2})})
    target = cfg.paths.roots()[0] / "loop.txt"
    target.write_text("x")
    provider = FakeProvider(
        turns=[
            [("fs_read", {"path": str(target)})],
            [("fs_read", {"path": str(target)})],
            "Summary of where I got to.",
        ]
    )
    session = await Session.create("test")
    await _run(_loop(small, provider, "fs_read"), session, "keep reading")

    row = _only(cfg)
    assert row["status"] == "abandoned"
    assert row["steps"] == row["max_steps"] == 2


async def test_a_model_failure_is_recorded_as_failed_with_the_error(cfg):
    class Broken:
        name = "broken"

        async def stream(
            self, messages: list[dict[str, Any]], tools=None, *, params: CallParams
        ) -> AsyncIterator[LLMEvent]:
            raise LLMError("backend went away")
            yield  # pragma: no cover - makes this an async generator

    session = await Session.create("test")
    await _run(_loop(cfg, Broken(), "fs_read"), session, "anything")

    row = _only(cfg)
    assert row["status"] == "failed"
    assert "backend went away" in row["error"]


async def test_a_backend_that_reports_no_usage_says_so_instead_of_reporting_zero(cfg):
    """The router in front of the local model drops the streaming usage chunk, so every real
    turn records zero tokens. `reported` is what tells 1c that apart from a free turn."""

    class Silent(FakeProvider):
        async def stream(self, messages, tools=None, *, params):
            async for event in super().stream(messages, tools, params=params):
                if isinstance(event, Finish):
                    yield Finish(reason=event.reason, usage={})
                else:
                    yield event

    session = await Session.create("test")
    await _run(_loop(cfg, Silent(turns=["Hi."]), "fs_read"), session, "hi")

    usage = _only(cfg)["usage"]
    assert usage["reported"] is False
    assert usage["input_tokens"] == 0


# --- who the record belongs to ----------------------------------------------


async def test_a_subagent_turn_is_recorded_under_its_own_role(cfg):
    """Sub-agents share this loop, so they are measured too. The baseline table filters on
    role; without it, a delegating turn would double-count its worker's tokens as its own."""
    from agentd.agent.delegation import TaskSpec
    from agentd.agent.subagents import RESEARCHER, run_subagent

    provider = FakeProvider(
        turns=["Found nothing."],
        json_results=[{"status": "ok", "summary": "Nothing found."}],
    )
    session = await Session.create("test")
    await run_subagent(
        RESEARCHER, TaskSpec("researcher", "look something up"), parent_session_id=session.id,
        parent_turn_id=session.id, parent_autonomy="assist",
        approver=AutoApprover(True), registry=_registry("fs_read"), cfg=cfg,
        provider=provider,
    )

    row = _only(cfg)
    assert row["role"] == "subagent"
    assert row["actor"] == "subagent:researcher"
    assert row["origin"] == "subagent:researcher"


# --- the property that outranks every number above ---------------------------


async def test_a_broken_telemetry_path_does_not_break_the_turn(cfg, tmp_path):
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("")
    cfg.telemetry.path = blocker / "telemetry.jsonl"
    before = telemetry.write_failures

    provider = FakeProvider(turns=["Hello there."])
    session = await Session.create("test")
    events = await _run(_loop(cfg, provider, "fs_read"), session, "hi")

    assert [e for e in events if isinstance(e, Answer)][0].text == "Hello there."
    assert telemetry.write_failures == before + 1


async def test_telemetry_can_be_switched_off_entirely(cfg):
    cfg.telemetry.enabled = False
    session = await Session.create("test")
    await _run(_loop(cfg, FakeProvider(turns=["Hi."]), "fs_read"), session, "hi")
    assert _records(cfg) == []


# --- the reader 1c depends on ------------------------------------------------


def test_a_half_written_final_line_is_skipped_not_fatal(cfg, tmp_path):
    """A process killed mid-write is one of the things this pass exists to count. The
    reader losing the whole file to it would take the measurement down with the crash."""
    path = tmp_path / "telemetry.jsonl"
    telemetry.append(path, {"request_id": "a"})
    telemetry.append(path, {"request_id": "b"})
    with path.open("a") as handle:
        handle.write('{"request_id": "c"')

    assert [r["request_id"] for r in telemetry.read_records(path)] == ["a", "b"]


def test_context_estimate_counts_the_arguments_a_tool_heavy_turn_is_made_of():
    """Counting only `content` would make a turn whose context is mostly the model's own
    tool-call arguments look small, which is exactly the turn Pass 8 is about."""
    bare = [{"role": "assistant", "content": "hi"}]
    with_calls = [
        {
            "role": "assistant",
            "content": "hi",
            "tool_calls": [
                {"function": {"name": "fs_read", "arguments": json.dumps({"path": "x" * 400})}}
            ],
        }
    ]
    assert telemetry.messages_tokens(with_calls) > telemetry.messages_tokens(bare)


@pytest.mark.parametrize("content", [None, ""])
def test_an_assistant_message_with_no_text_does_not_break_the_estimate(content):
    assert telemetry.messages_tokens([{"role": "assistant", "content": content}]) == 0
