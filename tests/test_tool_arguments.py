"""Arguments a tool rejects, and the turn that used to be spent re-sending them.

Live 2026-09-20: asked to confirm which of two residences was current, the user answered
"east village" and the model proposed the fact as `memory_remember(content=...,
importance=5)`. Neither name is the schema's. The rejection said only "'statement' is a
required property" - nothing about the argument that was actually sent, nothing about the
one that was wanted - so the model sent it again, identically, until the step budget was
gone. The correction the agent had just asked for was never stored.

Separate from `test_agent_loop.py` because this covers the executor's repair advice and the
loop's withdrawal of a tool that cannot be called, and because that file is being edited
elsewhere.
"""

from __future__ import annotations

import json

from agentd.agent.loop import AgentLoop, Session
from agentd.agent.stream import Answer
from agentd.llm.fake import FakeProvider
from agentd.policy.approvals import AutoApprover
from agentd.policy.engine import engine_from_config
from agentd.tools.registry import Registry


def _registry(*names: str) -> Registry:
    from agentd.tools import builtin_memory

    all_tools = {t.name: t for t in builtin_memory.TOOLS}
    reg = Registry()
    reg.add(*(all_tools[n] for n in names))
    return reg


async def _run(loop: AgentLoop, session: Session, text: str, **kwargs) -> list:
    return [event async for event in loop.run_turn(session, text, **kwargs)]



# --- a call the model cannot repair, and the turn it used to eat ------------------------
#
# Live 2026-09-20: asked to confirm which of two residences was current, the user answered
# "east village" and the model proposed the fact as `memory_remember(content=...,
# importance=5)`. Neither name is the schema's. The rejection said only "'statement' is a
# required property" - nothing about the argument that was actually sent, nothing about the
# one that was wanted - so the model sent it again, identically, until the step budget was
# gone. The correction the user had just been asked for was never stored.


async def test_a_rejection_names_the_argument_that_was_sent_and_the_ones_that_work(
    cfg, journaled
):
    provider = FakeProvider(
        turns=[[("memory_remember", {"content": "Dylan lives in East Village.", "importance": 5})]]
    )
    loop = AgentLoop(
        cfg=cfg, registry=_registry("memory_remember"), engine=engine_from_config(cfg),
        approver=AutoApprover(True), provider=provider,
    )
    session = await Session.create("test")
    await _run(loop, session, "east village")
    tool_message = [
        m for call in provider.calls for m in call["messages"] if m.get("role") == "tool"
    ][0]
    error = json.loads(tool_message["content"])["error"]

    assert "'content' is not a parameter" in error  # what it sent
    assert "statement (required)" in error  # what it should have sent
    # Every fault at once: the out-of-range number is not held back for the next round trip.
    assert "maximum of 1" in error
    assert [e.type for e in journaled("tool_finished", "tool_failed")] == ["tool_failed"]


async def test_the_same_rejected_arguments_do_not_get_to_spend_the_whole_turn(cfg):
    bad = {"content": "Dylan lives in East Village.", "importance": 5}
    provider = FakeProvider(
        turns=[
            [("memory_remember", bad)],
            [("memory_remember", bad)],
            [("memory_remember", bad)],
            "I could not store that.",
        ]
    )
    loop = AgentLoop(
        cfg=cfg, registry=_registry("memory_remember", "memory_search"),
        engine=engine_from_config(cfg), approver=AutoApprover(True), provider=provider,
    )
    session = await Session.create("test")
    events = await _run(loop, session, "east village")

    # The second rejection says plainly that a third attempt cannot work...
    errors = [
        json.loads(m["content"])["error"]
        for call in provider.calls
        for m in call["messages"]
        if m.get("role") == "tool"
    ]
    assert "sent these exact arguments 2 times" in errors[1]
    # ...and the tool is gone from the next step, while the rest of them stay.
    offered = provider.calls[-1]["tools"]
    assert "memory_remember" not in offered
    assert "memory_search" in offered
    assert [e for e in events if isinstance(e, Answer)][0].text == "I could not store that."


async def test_a_repeat_that_is_not_a_repeat_keeps_its_tool(cfg, journaled):
    """Two failures with *different* arguments are two honest attempts, not a loop."""
    provider = FakeProvider(
        turns=[
            [("memory_remember", {"content": "Dylan lives in East Village."})],
            [("memory_remember", {"statement": "Dylan lives in East Village.", "importance": 7})],
            [("memory_remember", {"statement": "Dylan lives in East Village."})],
            "Stored.",
        ]
    )
    loop = AgentLoop(
        cfg=cfg, registry=_registry("memory_remember"), engine=engine_from_config(cfg),
        approver=AutoApprover(True), provider=provider,
    )
    session = await Session.create("test")
    await _run(loop, session, "east village")
    assert "memory_remember" in provider.calls[-1]["tools"]
    # The third call is the repaired one, and it was allowed to run.
    assert [e.type for e in journaled("tool_finished", "tool_failed")] == [
        "tool_failed", "tool_failed", "tool_finished",
    ]
