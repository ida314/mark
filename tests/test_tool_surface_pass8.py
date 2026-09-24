"""Pass 8: the capability families that have left the orchestrator, and who owns them now.

Session 8a moved the filesystem and shell family to `coder`; session 8b moved `web_search`
and `web_fetch` to `researcher`; session 8c moved the memory pair to `memory` and the Gmail
pair to `mail`. One file for all three, because the claim is the same claim with a different
family in it, and a second file would be a second place for the next session to forget to
update.

8c is the first session whose move needed code outside the surface map. The memory tools had
no role to move *to* - `delegate(agent="memory")` packed retrieval in this process and built
no worker - and the mail tools moved behind a delegation that the private-data interlock and
the unattended-mailbox rule could not see through. Both are tested here, at the bottom.

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
from agentd.agent.results import WorkerReport, WorkerResult
from agentd.agent.subagents import (
    CODER,
    CODER_EXPLORE,
    MAIL,
    MEMORY,
    RESEARCHER,
    SPECS,
    run_subagent,
)
from agentd.llm.fake import FakeProvider
from agentd.policy.approvals import AutoApprover
from agentd.policy.engine import PolicyContext, ToolCallInfo, engine_from_config
from agentd.tools.base import Tool, ToolResult, obj
from agentd.tools.registry import ALWAYS_EXPOSE_LIMIT, Registry, build_registry
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

# Session 8c's two families. The mail pair is the one driven through the loop wherever the
# claim is about a refusal, because it is the one whose leak would be the user's own data.
MEMORY_SEARCH = "memory_search"
MEMORY_HISTORY = "memory_history"
GMAIL_SEARCH = "gmail_search"
GMAIL_MESSAGE = "gmail_message"


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


# --- session 8c: memory and the mailbox, and the two roles they moved to ------------------


async def test_the_shipped_orchestrator_cannot_search_memory_or_open_the_mailbox(cfg):
    """8c's claim, named rather than derived, for the same reason 8b's version is.

    These four are the first moved tools that were `always_on` - three of them were - so
    this is also the first session in which the orchestrator's *permanent* set shrinks
    rather than only its ceiling.
    """
    loop = _orchestrator(cfg, FakeProvider())

    assert MEMORY_SEARCH not in loop.tool_subset and MEMORY_HISTORY not in loop.tool_subset
    assert GMAIL_SEARCH not in loop.tool_subset and GMAIL_MESSAGE not in loop.tool_subset
    # Registered and enabled and `always_on` and still not the orchestrator's: the flag says
    # "always offered by the registry that holds it", and the registry that holds these is
    # the role's.
    registry = loop.registry
    assert {MEMORY_SEARCH, GMAIL_SEARCH, GMAIL_MESSAGE} <= set(registry.tools)
    assert all(registry.tools[n].always_on for n in (MEMORY_SEARCH, GMAIL_SEARCH, GMAIL_MESSAGE))


async def test_the_orchestrator_is_now_offered_every_tool_it_is_allowed_to_run(cfg):
    """The number the whole pass has been waiting for, as a property of one real turn.

    `Registry.select` returns early when `len(enabled) <= ALWAYS_EXPOSE_LIMIT`, and until
    this session the permitted pool was above it - 24 after 8a, 22 after 8b - so the
    similarity route kept running and simply backfilled whatever slots a moved family
    vacated. Both sessions reported a flat offered count for that reason. At 18 the
    short-circuit fires and the offered set *is* the permitted set.

    Asserted through the turn as well as through `select`, and checked for tools that are
    not `always_on` - `reminder_set`, `watcher_add`, `open_loops_list` - because an offered
    set that happened to equal the *permanent* set would pass a weaker version of this test.

    The turn offers one fewer than `select` returns, and it is not the short-circuit
    failing: `AgentLoop._with_lookup` withholds `handoff_lookup` unless this session has a
    handoff manifest to resolve refs against, because a tool that can only answer "nothing
    to look up" is a schema spent teaching the model a call that cannot work. So the
    steady-state offered count is 17, and 18 in a session that has handed off.
    """
    provider = FakeProvider(turns=["nothing to do"])
    loop = _orchestrator(cfg, provider)
    session = await Session.create("test")

    selected = {t.name for t in await loop.registry.select("what is on", permitted=loop.tool_subset)}

    await _run(loop, session, "what is on for today")

    # The short-circuit itself: below the limit, `select` hands back the whole permitted pool
    # rather than the `always_on` set plus whatever the embedding route scored.
    assert len(loop.tool_subset) <= ALWAYS_EXPOSE_LIMIT
    assert selected == loop.tool_subset
    offered = set(provider.calls[0]["tools"])
    assert offered == loop.tool_subset - {"handoff_lookup"}
    assert {"reminder_set", "watcher_add", "open_loops_list"} <= offered


async def test_naming_gmail_search_gets_the_orchestrator_the_mail_role(cfg, journaled):
    """Door 4 for 8c's riskier family, and the owner looked up rather than assumed.

    `mail` is the third distinct owner in `MOVED_TO_ROLE`, so a refusal that named a
    hardcoded role would now be wrong in two different ways; the assertion that neither
    `coder` nor `researcher` appears is what says the lookup is real.
    """
    provider = FakeProvider(
        turns=[[(GMAIL_SEARCH, {"query": "from:registrar@nyu.edu"})], "I cannot read your mail."]
    )
    session = await Session.create("test")

    await _run(_orchestrator(cfg, provider), session, "what did the registrar say?")

    error = json.loads(_tool_replies(provider)[0])["error"]
    assert "belongs to the mail sub-agent" in error
    assert "delegate(agent='mail'" in error
    assert "researcher" not in error and "coder" not in error
    failed = [e for e in journaled("tool_failed") if e.payload["name"] == GMAIL_SEARCH]
    assert "belongs to the mail sub-agent" in failed[0].payload["error"]
    requested = [e for e in journaled("tool_requested") if e.payload["name"] == GMAIL_SEARCH]
    assert [e.payload["visible"] for e in requested] == [False]
    assert [e.payload["known"] for e in requested] == [True]
    # It never ran, which for this family is the whole claim: nothing left this machine and
    # no mail entered the context.
    assert [e.payload["name"] for e in journaled("tool_finished")] == []


async def test_a_conversation_that_read_mail_before_the_move_is_not_offered_it_after(cfg):
    """Door 1 through the route with a memory, for all four of 8c's names at once.

    `session.tools_used` is persisted and survives a resume, so a conversation that searched
    the mailbox last week is the one route by which a moved tool comes back without anyone
    deciding it should - and for this family that route would hand the model the user's mail
    on the strength of a chat it once had.
    """
    provider = FakeProvider(turns=["nothing to do"])
    session = await Session.create("test")
    session.tools_used = {GMAIL_SEARCH, GMAIL_MESSAGE, MEMORY_SEARCH, MEMORY_HISTORY, "time_now"}

    await _run(_orchestrator(cfg, provider), session, "what did that email say again")

    offered = provider.calls[0]["tools"]
    assert not {GMAIL_SEARCH, GMAIL_MESSAGE, MEMORY_SEARCH, MEMORY_HISTORY} & set(offered)
    assert "time_now" in offered


async def test_tool_search_does_not_offer_the_orchestrator_the_mailbox(cfg):
    """Door 3. `tool_search` answers "These tools are now available", and a moved name in
    that list is a promise the executor breaks one step later - here, a promise about the
    user's mail."""
    provider = FakeProvider(
        turns=[[("tool_search", {"query": "search my email inbox"})], "I cannot read mail."]
    )
    session = await Session.create("test")

    await _run(_orchestrator(cfg, provider), session, "search my inbox for the lease")

    assert GMAIL_SEARCH not in _tool_replies(provider)[0]
    assert GMAIL_SEARCH not in provider.calls[1]["tools"]


# --- the two new roles ------------------------------------------------------------------


def test_the_memory_role_is_a_real_worker_and_not_a_branch_of_the_delegate_tool():
    """What made 8c harder than 8b: there was nothing for the memory tools to move *to*.

    `delegate(agent="memory")` used to call `memory.retrieval.pack` in this process and
    build no worker, so `memory` was a string the delegate tool special-cased rather than a
    durable role. 8a's exit criterion has a test behind it, so moving a family to a name
    with no spec fails the suite rather than passing quietly. This is the positive half for
    the name itself; `test_every_moved_tool_is_granted_by_the_role_it_moved_to` is the
    positive half for the two tools.
    """
    assert SPECS["memory"] is MEMORY
    assert MEMORY.tool_names is not None
    assert {MEMORY_SEARCH, MEMORY_HISTORY} <= set(MEMORY.tool_names)
    # Read-only, and that is the role's defining property rather than an accident of what it
    # happened to need: a worker proposes memories through `candidate_memories` in its
    # report, which is the path the review gate already watches, and `memory_remember` here
    # would be a second `proposed_by` for the same inference.
    assert "memory_remember" not in MEMORY.tool_names


async def test_the_memory_worker_holds_the_memory_tools_the_orchestrator_gave_up(cfg):
    """The positive half through the real `run_subagent`, because the orchestrator's side of
    this is a property of a default and the worker's side is a property of the subset it is
    handed - two mechanisms, and the family moved between them."""
    provider = FakeProvider(
        turns=[[(MEMORY_SEARCH, {"query": "where does the user live"})], "Nothing recorded."],
        json_results=[WorkerReport(status="completed", answer="Nothing is recorded about that.")],
    )
    session = await Session.create("test")

    await run_subagent(
        MEMORY, TaskSpec("memory", "what do you know about where they live"),
        parent_session_id=session.id, parent_turn_id=session.id, parent_autonomy="act",
        approver=AutoApprover(True), registry=build_registry(), cfg=cfg, provider=provider,
    )

    offered = provider.calls[0]["tools"]
    assert MEMORY_SEARCH in offered and MEMORY_HISTORY in offered
    assert "belongs to the memory sub-agent" not in "".join(_tool_replies(provider))


async def test_the_mail_worker_holds_the_mailbox_the_orchestrator_gave_up(cfg):
    """The same, for the family whose move is the safety decision of this session.

    The search returns nothing because no Gmail credential exists under the test config -
    `resolve_account` refuses inside the handler, which is after the surface check this test
    is about. A surface refusal would never reach the handler at all, which is what
    distinguishes the two in the assertion below.
    """
    provider = FakeProvider(
        turns=[[(GMAIL_SEARCH, {"query": "from:registrar"})], "I could not reach the mailbox."],
        json_results=[WorkerReport(status="blocked", answer="No account is configured.")],
    )
    session = await Session.create("test")

    await run_subagent(
        MAIL, TaskSpec("mail", "what did the registrar say about the deadline"),
        parent_session_id=session.id, parent_turn_id=session.id, parent_autonomy="act",
        approver=AutoApprover(True), registry=build_registry(), cfg=cfg, provider=provider,
    )

    offered = provider.calls[0]["tools"]
    assert GMAIL_SEARCH in offered and GMAIL_MESSAGE in offered
    assert "belongs to the mail sub-agent" not in "".join(_tool_replies(provider))


def test_the_mailbox_and_the_open_web_are_never_inside_one_worker():
    """Why the mail tools did not go to `researcher`, as a check rather than as a paragraph.

    `private-data-no-outward-delegation` exists because a sub-agent starts with a fresh
    session, unaware its caller ever read the mailbox, holding `web_search` and `web_fetch`.
    After 8b the researcher is the *only* holder of those two. A role that held the mailbox
    as well would have both sides of that interlock inside one context where no rule can see
    them, and the rule would still read as though it were enforced.

    Stated over every spec rather than over `MAIL`, so that the day somebody adds
    `gmail_search` to the researcher - the obvious, wrong, tidy thing to do - this fails.
    """
    registry = build_registry()
    for name, spec in SPECS.items():
        granted = set(spec.tool_names or ())
        mail = {n for n in granted if "mail" in registry.tools[n].tags}
        egress = {n for n in granted if "egress" in registry.tools[n].tags}
        assert not (mail and egress), f"{name} holds both the mailbox {mail} and egress {egress}"
    assert {n for n in (MAIL.tool_names or ()) if "mail" in registry.tools[n].tags}


def test_the_two_new_roles_need_nobody_at_the_terminal(cfg):
    """Both are read-only, and that is a property worth pinning rather than a coincidence.

    It is 8a's criterion for `coder/explore`: nothing either can call returns
    `require_approval`, so both finish on a path whose approver queues and denies - `agent
    ask`, a watcher, the heartbeat. For `memory` it is what makes a delegation a usable
    replacement for the `memory_search` the orchestrator gave up on *every* path and not
    only the attended one. When this fails, a write has been added to a role that was moved
    tools on the strength of being read-only.
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
        for spec in (MEMORY, MAIL)
        for name in spec.tool_names or []
        for level in ("observe", "assist", "act")
    }

    assert outcomes == {"allow"}


# --- the interlock, which now has a moved family inside it -------------------------------


async def test_a_mail_delegation_closes_the_door_behind_it(cfg, monkeypatch):
    """The hole this session would have opened if the move had been a filing decision.

    A worker's `session.private` dies with the worker. What survives is its answer, and for
    this role the answer *is* the user's mail, rendered into the orchestrator's context - so
    moving `gmail_search` behind a delegation would have repealed the interlock by putting
    it somewhere the flag could not reach. `reads_private_data` derives the flag from the
    role's own tools and `agent/loop.py` raises it on the caller.

    8b left "no test covers the combination of a moved family and a private session" as an
    open question. This is it: the mail family is moved, and the researcher - which is the
    only holder of the web since 8b - is refused afterwards by the interlock rather than by
    the surface.
    """
    from agentd.agent import delegation

    async def fake_delegate(durable_role, task, **kwargs):
        return WorkerResult(status="completed", answer=f"{durable_role} says: the 14th.")

    monkeypatch.setattr(delegation, "delegate", fake_delegate)
    provider = FakeProvider(
        turns=[
            [("delegate", {"agent": "mail", "task": "what deadline did the registrar give"})],
            [("delegate", {"agent": "researcher", "task": "what is the university calendar"})],
            "I cannot research anything after reading your mail.",
        ]
    )
    session = await Session.create("test")

    await _run(_orchestrator(cfg, provider), session, "what deadline did the registrar give?")

    assert session.private is True
    errors = [json.loads(reply) for reply in _tool_replies(provider)]
    assert "private-data-no-outward-delegation" in json.dumps(errors[1])


async def test_a_memory_delegation_leaves_the_door_open(cfg, monkeypatch):
    """The control, and the reason `memory` is not in the interlock's list of roles.

    Only a role that can hand back the user's private data raises the flag. If the
    propagation above were written as "any delegation" - the easy way to make the mail test
    pass - then asking what you know would close the outside world for the rest of the
    conversation, and the failure would look like a model that had gone quiet rather than
    like a bug.
    """
    from agentd.agent import delegation

    async def fake_delegate(durable_role, task, **kwargs):
        return WorkerResult(status="completed", answer="They live in the East Village. [F:01a0]")

    monkeypatch.setattr(delegation, "delegate", fake_delegate)
    provider = FakeProvider(
        turns=[
            [("delegate", {"agent": "memory", "task": "where do they live"})],
            "The East Village.",
        ]
    )
    session = await Session.create("test")

    await _run(_orchestrator(cfg, provider), session, "where do I live again?")

    assert session.private is False


def test_a_private_turn_can_still_consult_memory_and_the_mailbox():
    """Why `memory` and `mail` are deliberately absent from the interlock's role list.

    Before 8c the orchestrator held `memory_search` and `memory_history` itself, and both
    are in `test_policy.py`'s `PRIVATE_SAFE` set: the interlock shuts the *egress* door, and
    a local SELECT over the user's own memory opens none. After 8c the only way to reach
    them is a delegation, so listing `memory` beside `researcher` and `coder` would mean
    that reading the mail leaves the agent unable to consult its memory at all - and "read
    the invitation, then look at what it collides with" is the workflow the interlock was
    written to preserve, not one it is meant to break.
    """
    from pathlib import Path

    from agentd.config import DEFAULT_POLICY
    from agentd.policy.engine import load_engine

    engine = load_engine(
        DEFAULT_POLICY,
        {"allowed_roots": [str(Path.home())], "workspace": ["/tmp/ws"], "memory_repo": ["/tmp/m"]},
    )

    def verdict(agent: str, **ctx) -> str:
        return engine.evaluate(
            ToolCallInfo(name="delegate", risk="read", tags=("core",), args={"agent": agent}),
            PolicyContext(autonomy="assist", **ctx),
        ).outcome

    assert verdict("memory", private=True) == "allow"
    assert verdict("mail", private=True) == "allow"
    assert verdict("researcher", private=True) == "deny"
    assert verdict("coder", private=True) == "deny"


def test_the_daemon_cannot_delegate_its_way_into_the_mailbox():
    """`mail-tools-never-unattended` stopped reaching the moment the tools moved.

    It matches `origin: [daemon]`, and `run_subagent` gives a worker an origin of
    `subagent:<role>` rather than its caller's - so a heartbeat that delegated to `mail`
    would have handed the mailbox to a turn the rule cannot see. That is the shape of
    regression a surface move can cause without touching a policy file, and
    `mail-delegation-never-unattended` is the same refusal one layer up.

    The researcher control matters: this must deny the mailbox, not delegation.
    """
    from pathlib import Path

    from agentd.config import DEFAULT_POLICY
    from agentd.policy.engine import load_engine

    engine = load_engine(
        DEFAULT_POLICY,
        {"allowed_roots": [str(Path.home())], "workspace": ["/tmp/ws"], "memory_repo": ["/tmp/m"]},
    )

    def verdict(agent: str, origin: str) -> str:
        return engine.evaluate(
            ToolCallInfo(name="delegate", risk="read", tags=("core",), args={"agent": agent}),
            PolicyContext(autonomy="observe", origin=origin),
        ).outcome

    assert verdict("mail", "daemon") == "deny"
    assert verdict("mail", "interactive") == "allow"
    assert verdict("researcher", "daemon") == "allow"
    # And a worker cannot delegate onward, so the call above is the daemon's only route:
    # `delegate` is in no role's tool_names.
    assert all("delegate" not in (spec.tool_names or ()) for spec in SPECS.values())
