"""Pass 8: the capability families that have left the orchestrator, and who owns them now.

Session 8a moved the filesystem and shell family to `coder`; session 8b moved `web_search`
and `web_fetch` to `researcher`. One file for both, because the claim is the same claim with
a different family in it, and a second file would be a second place for the next session to
forget to update.

`tests/test_tool_surface.py` establishes that a surface is a boundary - four doors, each shut
against a probe tool. This file asserts the thing that file's mechanism was built for, and it
differs in two ways that are the point:

* **the real registry and the real default.** Every test here builds the orchestrator the way
  `cli/chat.py` builds it - `AgentLoop(cfg=..., approver=...)`, no `tool_subset`, the whole
  registry - because the pass's claim is about the shipped orchestrator and not about a loop
  handed a hand-written surface. If the default ever stops narrowing, these fail and the
  other file does not.
* **both halves of the exit criterion.** "Every moved tool reachable through a durable role"
  has a negative half that is easy to leave untested: *and not reachable any other way.* The
  refusals below are the negative half; `test_every_moved_tool_is_granted_by_the_role_it_moved
  _to` and the worker test are the positive one.

The pass file's warning, which is why this file exists at all: *every session of this pass
must treat "the orchestrator cannot call X" as a claim needing a test, not a description of
what was removed from a list.*
"""

from __future__ import annotations

import json

from agentd.agent.delegation import TaskSpec
from agentd.agent.loop import AgentLoop, Session
from agentd.agent.results import WorkerReport
from agentd.agent.subagents import CODER, CODER_EXPLORE, RESEARCHER, SPECS, run_subagent
from agentd.llm.fake import FakeProvider
from agentd.policy.approvals import AutoApprover
from agentd.policy.engine import PolicyContext, ToolCallInfo, engine_from_config
from agentd.tools.base import Tool, ToolResult, obj
from agentd.tools.registry import Registry, build_registry
from agentd.tools.surface import MOVED_TO_ROLE, orchestrator_surface

# The two ends of the moved family: the read that costs nothing and the write that needs an
# approval. Both have to be refused, because "it was only a read" is the argument that would
# put the family back one tool at a time.
READ_SIDE = "fs_read"
WRITE_SIDE = "shell_exec"

# Session 8b's family. `web_fetch` is the one that carries an argument and an SSRF guard, so
# it is the one driven through the loop; `web_search` is asserted alongside it wherever the
# claim is about the family rather than about one call.
WEB_FETCH = "web_fetch"
WEB_SEARCH = "web_search"


def _orchestrator(cfg, provider: FakeProvider, registry: Registry | None = None) -> AgentLoop:
    """The orchestrator exactly as the five production call sites build it: no `tool_subset`.

    `engine_from_config` and the approver are the only things passed that they do not pass,
    and neither touches the surface.
    """
    return AgentLoop(
        cfg=cfg, registry=registry or build_registry(), engine=engine_from_config(cfg),
        approver=AutoApprover(True), provider=provider,
    )


async def _run(loop: AgentLoop, session: Session, text: str) -> None:
    async for _ in loop.run_turn(session, text):
        pass


def _tool_replies(provider: FakeProvider) -> list[str]:
    return [
        m["content"]
        for call in provider.calls
        for m in call.get("messages", [])
        if m.get("role") == "tool"
    ]


# --- the orchestrator cannot reach the family, by any door ------------------------------


async def test_the_shipped_orchestrator_has_no_filesystem_or_shell_on_its_surface(cfg):
    """The claim itself, against the registry the process actually builds.

    Stated as a set difference rather than as five assertions so that adding a tool to the
    family in 8b or 8c cannot pass by being forgotten here.
    """
    loop = _orchestrator(cfg, FakeProvider())

    assert set(MOVED_TO_ROLE) & loop.tool_subset == set()
    assert MOVED_TO_ROLE.keys() <= set(loop.registry.tools)  # registered, and still refused
    assert len(loop.tool_subset) == len(loop.registry.tools) - len(MOVED_TO_ROLE)
    # Named outright as well, because the three lines above read `MOVED_TO_ROLE` on both
    # sides and would all hold of an empty one - a mutation that moves nothing at all would
    # pass them. These two are what the session actually claims to have moved.
    assert READ_SIDE not in loop.tool_subset and WRITE_SIDE not in loop.tool_subset


async def test_naming_the_shell_gets_the_orchestrator_the_coder_and_not_just_a_refusal(
    cfg, journaled
):
    """Door 4, and the sentence it answers with.

    A refusal that only says no costs a step and teaches nothing; the model's next move is
    to look for another route, and there is not one. Dylan's ruling of 2026-09-24 put this
    reason in `tool_failed`'s error text rather than in a new field on `tool_requested`, so
    this string is the entire record that the tool has an owner - which is why it is asserted
    here in both places it lands.
    """
    provider = FakeProvider(
        turns=[[(WRITE_SIDE, {"command": "pytest"})], "I cannot run the suite myself."]
    )
    session = await Session.create("test")

    await _run(_orchestrator(cfg, provider), session, "run the test suite")

    error = json.loads(_tool_replies(provider)[0])["error"]
    assert "belongs to the coder sub-agent" in error
    assert "delegate(agent='coder'" in error
    failed = [e for e in journaled("tool_failed") if e.payload["name"] == WRITE_SIDE]
    assert "belongs to the coder sub-agent" in failed[0].payload["error"]
    # And it never became part of the turn: `visible` stays false, which is what keeps the
    # door-4 rate readable.
    requested = [e for e in journaled("tool_requested") if e.payload["name"] == WRITE_SIDE]
    assert [e.payload["visible"] for e in requested] == [False]
    assert [e.payload["known"] for e in requested] == [True]
    assert [e.payload["name"] for e in journaled("tool_finished")] == []


async def test_a_conversation_that_read_files_before_the_move_is_not_offered_them_after(cfg):
    """Door 1, through the route with a memory.

    `session.tools_used` survives a resume, so a conversation that used `fs_read` while it was
    still on the surface would otherwise carry it back in on the next message - the move
    undone by continuing a chat rather than by any decision.
    """
    provider = FakeProvider(turns=["nothing to do"])
    session = await Session.create("test")
    session.tools_used = {READ_SIDE, WRITE_SIDE, "time_now"}

    await _run(_orchestrator(cfg, provider), session, "what did that file say")

    offered = provider.calls[0]["tools"]
    assert READ_SIDE not in offered and WRITE_SIDE not in offered
    assert "time_now" in offered  # the same route, for a tool that did not move


async def test_tool_search_does_not_offer_the_orchestrator_a_tool_that_has_moved(cfg):
    """Door 3. The failure this prevents is a lie, not an extra call: `tool_search` answers
    "These tools are now available", and a moved name in that list is a promise the executor
    then breaks one step later."""
    provider = FakeProvider(
        turns=[[("tool_search", {"query": "read a file from disk"})], "I cannot read files."]
    )
    session = await Session.create("test")

    await _run(_orchestrator(cfg, provider), session, "read src/agentd/config.py")

    assert READ_SIDE not in _tool_replies(provider)[0]
    assert READ_SIDE not in provider.calls[1]["tools"]


async def test_a_tool_registered_after_the_loop_was_built_is_still_on_its_surface(cfg):
    """The regression guard for the *shape* of the narrowing, not for its contents.

    8a's surface is a subtraction from whatever the registry holds, never a fixed list, and
    this is the difference. A list would lock out every tool registered after the loop was
    constructed - an MCP server's, a test's probe - and the failure would look like "that
    tool does not work here" rather than like a surface decision. The enforcement commit
    already made this mistake once by snapshotting the subset in `__init__`; it is the same
    mistake with a different cause.
    """
    ran: list[str] = []

    async def handler(args, ctx):
        ran.append("late_probe")
        return ToolResult(content="ok")

    provider = FakeProvider(turns=[[("late_probe", {})], "Done."])
    loop = _orchestrator(cfg, provider)
    loop.registry.add(
        Tool(
            name="late_probe", description="registered after the loop existed",
            parameters=obj(), handler=handler, effect_class="read", risk="read",
        )
    )
    session = await Session.create("test")

    await _run(loop, session, "use the probe")

    assert ran == ["late_probe"]
    assert "late_probe" in loop.tool_subset


# --- and the role it moved to really has it ---------------------------------------------


def test_every_moved_tool_is_granted_by_the_role_it_moved_to():
    """The exit criterion "every moved tool reachable through a durable role", as a check
    rather than as a sentence in a record.

    Read against `SPECS` rather than against a copy of it, so a role whose `tool_names` is
    edited later - to tidy it, to narrow it - cannot quietly strand a family that was moved
    on the strength of holding it.
    """
    for name, role in MOVED_TO_ROLE.items():
        spec = SPECS.get(role)
        assert spec is not None, f"{name} was moved to {role!r}, which is not a durable role"
        assert spec.tool_names is not None and name in spec.tool_names, (
            f"{name} was moved to the {role} role, which does not grant it"
        )


async def test_the_coder_worker_holds_what_the_orchestrator_gave_up(cfg):
    """Through the real `run_subagent`, because the orchestrator's half of this is a property
    of the default and the worker's half is a property of the subset it is handed - two
    different mechanisms, and only one of them changed in 8a."""
    provider = FakeProvider(
        turns=[[(WRITE_SIDE, {"command": "true"})], "Ran it."],
        json_results=[WorkerReport(status="completed", answer="Ran it.")],
    )
    session = await Session.create("test")

    await run_subagent(
        CODER, TaskSpec("coder", "run the suite"),
        parent_session_id=session.id, parent_turn_id=session.id, parent_autonomy="act",
        approver=AutoApprover(True), registry=build_registry(), cfg=cfg, provider=provider,
    )

    offered = provider.calls[0]["tools"]
    assert WRITE_SIDE in offered and READ_SIDE in offered
    # Not merely offered: the refusal that stops the orchestrator does not fire here.
    assert "belongs to the coder sub-agent" not in "".join(_tool_replies(provider))


def test_the_explore_worker_is_the_one_that_needs_nobody_at_the_terminal(cfg):
    """`coder/explore` earns its definition on exactly one property, so that property is what
    is tested: nothing it can call returns `require_approval`, which is what lets it finish on
    a path whose approver queues and denies - `agent ask`, a watcher, the heartbeat.

    Asserted against the shipped policy and against `coder` in the same test, because the
    claim is comparative. If a later edit makes one of the explorer's tools need approval, or
    makes the full coder's stop needing it, the reason to have two specs is gone and this is
    what says so.
    """
    engine = engine_from_config(cfg)
    registry = build_registry()
    ctx = PolicyContext(autonomy="assist", origin="interactive")

    def verdicts(spec) -> set[str]:
        return {
            engine.evaluate(
                ToolCallInfo(
                    name=name, risk=registry.tools[name].risk,
                    tags=set(registry.tools[name].tags), args={},
                ),
                ctx,
            ).outcome
            for name in spec.tool_names or []
        }

    assert verdicts(CODER_EXPLORE) == {"allow"}
    assert "require_approval" in verdicts(CODER)


def test_the_explorer_cannot_write_or_run_anything():
    """The half of the claim above that is about the spec rather than about the policy. A
    read-only worker that holds `fs_write` is a claim, not a property."""
    assert set(CODER_EXPLORE.tool_names or []) & {"fs_write", "shell_exec"} == set()
    assert "fs_read" in (CODER_EXPLORE.tool_names or [])


# --- the callers that are not a turn ----------------------------------------------------


def test_the_surface_is_a_subtraction_and_says_so_for_an_empty_registry():
    """`orchestrator_surface` is total: it never invents a name, and a registry that holds
    none of the moved family is returned unchanged. The MCP server and `policy/replay` pass
    no surface at all and are unaffected - that is `test_tool_surface.py`'s last test."""
    assert orchestrator_surface([]) == set()
    assert orchestrator_surface(["time_now", "delegate"]) == {"time_now", "delegate"}
    assert orchestrator_surface([READ_SIDE, "time_now"]) == {"time_now"}


# --- session 8b: the web family, and the researcher --------------------------------------


async def test_the_shipped_orchestrator_cannot_search_the_web_or_open_a_url(cfg):
    """8b's claim, named rather than derived.

    The set-difference test above holds of an empty `MOVED_TO_ROLE` and of one that lost the
    web entries, because it reads the same dict on both sides. These two names are what this
    session says it moved, so these two names are asserted.
    """
    loop = _orchestrator(cfg, FakeProvider())

    assert WEB_SEARCH not in loop.tool_subset and WEB_FETCH not in loop.tool_subset
    # Registered, enabled, and still not the orchestrator's: the move is a surface decision
    # and not a tool being switched off, which is what keeps the researcher able to call it.
    assert {WEB_SEARCH, WEB_FETCH} <= set(loop.registry.tools)


async def test_naming_web_fetch_gets_the_orchestrator_the_researcher_and_not_the_coder(
    cfg, journaled
):
    """Door 4, and the half of the refusal 8a could not test: that the owner is looked up
    rather than written into the sentence.

    8a's family all belongs to one role, so a refusal that said "coder" unconditionally would
    have passed every test it wrote. A second role is what makes `owner_of` falsifiable, and
    the assertion that the *coder* is not named is the whole point of the test.
    """
    provider = FakeProvider(
        turns=[
            [(WEB_FETCH, {"url": "https://docs.example/recurring-events"})],
            "I cannot open a page myself.",
        ]
    )
    session = await Session.create("test")

    await _run(_orchestrator(cfg, provider), session, "what do Google's docs say?")

    error = json.loads(_tool_replies(provider)[0])["error"]
    assert "belongs to the researcher sub-agent" in error
    assert "delegate(agent='researcher'" in error
    assert "coder" not in error
    failed = [e for e in journaled("tool_failed") if e.payload["name"] == WEB_FETCH]
    assert "belongs to the researcher sub-agent" in failed[0].payload["error"]
    # It never became part of the turn, and it never ran: a fetch is an `unsafe_write`
    # because the server may act on it, so "refused before the handler" is the claim.
    requested = [e for e in journaled("tool_requested") if e.payload["name"] == WEB_FETCH]
    assert [e.payload["visible"] for e in requested] == [False]
    assert [e.payload["known"] for e in requested] == [True]
    assert [e.payload["name"] for e in journaled("tool_finished")] == []


async def test_a_conversation_that_browsed_before_the_move_is_not_offered_the_web_after(cfg):
    """Door 1 through the route with a memory, for 8b's family.

    Worth repeating rather than trusting 8a's version: `session.tools_used` is persisted and
    survives a resume, so a chat that fetched a page last week is the one route by which a
    moved tool comes back without anyone deciding it should.
    """
    provider = FakeProvider(turns=["nothing to do"])
    session = await Session.create("test")
    session.tools_used = {WEB_SEARCH, WEB_FETCH, "time_now"}

    await _run(_orchestrator(cfg, provider), session, "look that up again")

    offered = provider.calls[0]["tools"]
    assert WEB_SEARCH not in offered and WEB_FETCH not in offered
    assert "time_now" in offered


async def test_the_researcher_worker_holds_the_web_the_orchestrator_gave_up(cfg):
    """The positive half, through the real `run_subagent` rather than off the spec.

    The loopback url is deliberate: `web_fetch`'s own SSRF guard refuses it inside the
    handler, which is after the surface check this test is about, so the tool is reached and
    nothing leaves the machine. A surface refusal would never get that far.
    """
    provider = FakeProvider(
        turns=[[(WEB_FETCH, {"url": "http://127.0.0.1/8b"})], "Could not read it."],
        json_results=[WorkerReport(status="blocked", answer="The page was unreachable.")],
    )
    session = await Session.create("test")

    await run_subagent(
        RESEARCHER, TaskSpec("researcher", "find out what the docs say"),
        parent_session_id=session.id, parent_turn_id=session.id, parent_autonomy="act",
        approver=AutoApprover(True), registry=build_registry(), cfg=cfg, provider=provider,
    )

    offered = provider.calls[0]["tools"]
    assert WEB_SEARCH in offered and WEB_FETCH in offered
    assert "belongs to the researcher sub-agent" not in "".join(_tool_replies(provider))


async def test_the_researcher_still_reads_files_that_the_coder_role_owns(cfg):
    """The subtraction is the orchestrator's ceiling, not a global disabling.

    `fs_read`, `fs_list` and `fs_search` moved to `coder` in 8a and the researcher keeps all
    three, because half of a real research question is in the repository - "does this file
    handle recurring events the way the vendor's docs say" is one brief. If the moved
    families were ever subtracted from a worker's own subset too, this is what would say so,
    and the symptom in the field would be a researcher that can read the web and not the
    file it was asked about.
    """
    granted = set(RESEARCHER.tool_names or [])

    assert {"fs_read", "fs_list", "fs_search"} <= granted
    assert {name for name, role in MOVED_TO_ROLE.items() if role == "coder"} & granted

    provider = FakeProvider(
        turns=["nothing to read"],
        json_results=[WorkerReport(status="completed", answer="Nothing to read.")],
    )
    session = await Session.create("test")
    await run_subagent(
        RESEARCHER, TaskSpec("researcher", "how does gcal.py handle recurring events"),
        parent_session_id=session.id, parent_turn_id=session.id, parent_autonomy="act",
        approver=AutoApprover(True), registry=build_registry(), cfg=cfg, provider=provider,
    )

    assert "fs_read" in provider.calls[0]["tools"]


def test_the_researcher_role_already_needs_nobody_at_the_terminal(cfg):
    """Why 8b defined no ephemeral workers, as a check rather than as a paragraph.

    `coder/explore` earns a spec because it holds a capability the full role does not:
    nothing it can call returns `require_approval`, so it completes where the approver queues
    and denies. Every one of the researcher's tools is `allow` at every autonomy level, so the
    role already has that property and a narrower slice of it - a source finder, a document
    analyst - would differ from the role only in the wording of its prompt.

    When this fails, the reasoning has expired rather than the code: a web tool that starts
    needing approval (an egress budget, a paid API) is exactly the change that would make a
    read-only research worker worth defining.
    """
    engine = engine_from_config(cfg)
    registry = build_registry()

    outcomes = {
        engine.evaluate(
            ToolCallInfo(
                name=name, risk=registry.tools[name].risk,
                tags=set(registry.tools[name].tags), args={},
            ),
            PolicyContext(autonomy=level, origin="interactive"),
        ).outcome
        for name in RESEARCHER.tool_names or []
        for level in ("observe", "assist", "act")
    }

    assert outcomes == {"allow"}
