"""The next turn sees what the previous one did.

Session 01a0e9f6 (2026-09-28, Telegram): a turn made two `delegate` calls - one rejected for
missing arguments, one that ran a coder to the approval wall - and relayed the result. On the
next message, "why did delegate fail first?", the model was handed its own reply with no call
behind it, concluded it had never called anything, confessed to a fabrication it had not
committed, and in the same reply claimed the file now existed. It did not. The archive had every
call; the history rebuilt from the archive had none of them.
"""

from __future__ import annotations

import json

from agentd.agent import context as ctxmod
from agentd.agent.loop import AgentLoop, Session
from agentd.db import repo_archive
from agentd.db.repo_archive import RawEvent
from agentd.ids import estimate_tokens, uuid7
from agentd.llm.fake import FakeProvider
from agentd.policy.approvals import AutoApprover
from agentd.policy.engine import engine_from_config
from agentd.tools.registry import Registry


def _registry(*names: str) -> Registry:
    from agentd.tools import builtin_fs

    reg = Registry()
    reg.add(*(t for t in builtin_fs.TOOLS if t.name in names))
    return reg


async def _run(loop: AgentLoop, session: Session, text: str) -> list:
    return [event async for event in loop.run_turn(session, text)]


def _tool_calls(history: list[dict]) -> list[str]:
    return [c["function"]["name"] for m in history for c in m.get("tool_calls") or []]


async def test_the_next_turn_is_shown_the_previous_turns_calls_and_results(cfg) -> None:
    target = cfg.paths.roots()[0] / "note.txt"
    target.write_text("the answer is 42")
    provider = FakeProvider(
        turns=[[("fs_read", {"path": str(target)})], "The file says 42.", "Yes, I read it."]
    )
    loop = AgentLoop(
        cfg=cfg, registry=_registry("fs_read"), tool_subset=["fs_read"],
        engine=engine_from_config(cfg), approver=AutoApprover(True), provider=provider,
    )
    session = await Session.create("test")
    await _run(loop, session, "what does the note say?")
    await _run(loop, session, "did you actually read it?")

    prompt = provider.calls[-1]["messages"]
    assert [m["role"] for m in prompt] == [
        "system", "user", "assistant", "tool", "assistant", "user",
    ]
    calls = prompt[2]
    assert calls["content"] is None
    assert calls["tool_calls"][0]["function"]["name"] == "fs_read"
    assert json.loads(calls["tool_calls"][0]["function"]["arguments"]) == {"path": str(target)}
    result = prompt[3]
    assert result["tool_call_id"] == calls["tool_calls"][0]["id"]
    assert "42" in result["content"]
    assert prompt[4]["content"].startswith("The file says 42.")


async def test_a_workers_calls_stay_out_of_its_callers_history(cfg) -> None:
    """A worker runs in its caller's session, so its `tool_result` rows sit beside the
    caller's in the same table. What tells them apart is the turn: the worker's calls belong
    to the worker's turn, and the caller's door to that work is the `delegate` result."""
    session = await Session.create("test")
    turn, worker_turn = uuid7(), uuid7()
    for event in (
        RawEvent(kind="user_message", actor="user", content="list the repo",
                 session_id=session.id, turn_id=turn),
        RawEvent(kind="tool_result", actor="tool:shell_exec", content="exit=0\nsrc tests",
                 session_id=session.id, turn_id=worker_turn,
                 payload={"args": {"command": "ls"}, "ok": True, "call_id": "w1"}),
        RawEvent(kind="assistant_message", actor="subagent:coder", content="worker prose",
                 session_id=session.id, turn_id=worker_turn),
        RawEvent(kind="tool_result", actor="tool:delegate", content='{"status": "completed"}',
                 session_id=session.id, turn_id=turn,
                 payload={"args": {"agent": "coder", "task": "ls"}, "ok": True, "call_id": "d1"}),
        RawEvent(kind="assistant_message", actor="main", content="src and tests",
                 session_id=session.id, turn_id=turn),
    ):
        await repo_archive.append_event(event)

    history = await ctxmod.history_messages(session.id, budget_tokens=10_000)

    assert _tool_calls(history) == ["delegate"]
    assert not any("worker prose" in (m.get("content") or "") for m in history)
    assert not any("src tests" in (m.get("content") or "") for m in history)


async def test_the_budget_cut_keeps_an_answer_with_its_calls_or_drops_both(cfg) -> None:
    """The failure this guards against is the one this file is named for, recreated by the
    budget: an old answer kept in the window with the calls behind it cut away."""
    session = await Session.create("test")
    first, second = uuid7(), uuid7()
    for event in (
        RawEvent(kind="user_message", actor="user", content="a" * 100,
                 session_id=session.id, turn_id=first),
        RawEvent(kind="tool_result", actor="tool:fs_read", content="r" * 400,
                 session_id=session.id, turn_id=first,
                 payload={"args": {"path": "x"}, "ok": True, "call_id": "c1"}),
        RawEvent(kind="assistant_message", actor="main", content="x" * 1000,
                 session_id=session.id, turn_id=first),
        RawEvent(kind="user_message", actor="user", content="b" * 100,
                 session_id=session.id, turn_id=second),
        RawEvent(kind="assistant_message", actor="main", content="y" * 100,
                 session_id=session.id, turn_id=second),
    ):
        await repo_archive.append_event(event)

    # Room for the second turn and the first turn's answer on its own, not for the answer
    # together with the call behind it.
    budget = estimate_tokens("b" * 100) + estimate_tokens("y" * 100) + estimate_tokens("x" * 1000) + 10
    history = await ctxmod.history_messages(session.id, budget_tokens=budget)

    assert [m["role"] for m in history] == ["user", "assistant"]
    assert history[0]["content"] == "b" * 100
    assert not any("x" * 1000 == m.get("content") for m in history)

    roomy = await ctxmod.history_messages(session.id, budget_tokens=10_000)
    assert [m["role"] for m in roomy] == ["user", "assistant", "tool", "assistant", "user", "assistant"]


async def test_rows_archived_before_call_ids_still_pair_up_and_long_results_are_clipped(cfg) -> None:
    session = await Session.create("test")
    turn = uuid7()
    for event in (
        RawEvent(kind="user_message", actor="user", content="read it",
                 session_id=session.id, turn_id=turn),
        RawEvent(kind="tool_result", actor="tool:fs_read", content="z" * 5000,
                 session_id=session.id, turn_id=turn,
                 payload={"args": {"path": "big.txt"}, "ok": True}),
        RawEvent(kind="assistant_message", actor="main", content="it is long",
                 session_id=session.id, turn_id=turn),
    ):
        await repo_archive.append_event(event)

    history = await ctxmod.history_messages(session.id, budget_tokens=10_000)

    calls, result = history[1], history[2]
    assert calls["tool_calls"][0]["id"] == result["tool_call_id"]
    assert result["content"].startswith("z" * ctxmod.HISTORY_TOOL_RESULT_CHARS)
    assert "z" * (ctxmod.HISTORY_TOOL_RESULT_CHARS + 1) not in result["content"]
    assert "truncated" in result["content"] and "5000" in result["content"]


async def test_a_turn_that_called_tools_and_never_answered_keeps_its_calls(cfg) -> None:
    """A turn killed after its calls has no `assistant_message`; the calls go after the user
    message that caused them rather than nowhere."""
    session = await Session.create("test")
    turn = uuid7()
    for event in (
        RawEvent(kind="user_message", actor="user", content="read it",
                 session_id=session.id, turn_id=turn),
        RawEvent(kind="tool_result", actor="tool:fs_read", content="No such file: x",
                 session_id=session.id, turn_id=turn,
                 payload={"args": {"path": "x"}, "ok": False, "call_id": "c1"}),
    ):
        await repo_archive.append_event(event)

    history = await ctxmod.history_messages(session.id, budget_tokens=10_000)

    assert [m["role"] for m in history] == ["user", "assistant", "tool"]
    assert history[2]["content"] == "No such file: x"


def test_replaying_nothing_is_nothing() -> None:
    assert ctxmod.replay_tool_calls([]) == []
