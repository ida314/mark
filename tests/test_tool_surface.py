"""An agent's tool surface is a boundary, not a suggestion.

Pass 8 moves tool families off the orchestrator. That decided nothing while the surface was
only a selection hint: a tool the turn never offered still ran if the model named it, and
three other routes could put it back. Dylan's reading, which is what these tests are drawn
against:

> If only door 1 respects the subset, 8a's tool move is still advisory: the orchestrator can
> tool_search for fs_write (door 3), pick it up from a handoff manifest written by an
> earlier session that used it (door 2), or just name it (door 4). The boundary has to be
> the subset itself, and every door has to respect it.

Four doors lead into a turn's tool list, and each gets one test here for the orchestrator
and one for a worker - Dylan again: *"Workers already have Registry.subset, but I don't know
that doors 2-4 respect it there either, and the test settles that."* Where a worker's door
is shut by construction rather than by this change, the test says which construction shuts
it, because that is the part a later refactor can quietly remove.

The last test is the one that distinguishes this design from a stricter one that was
rejected. `Registry.select` is an embedding lookup and it misses; a model naming a tool it
is entitled to use is recovering from a retrieval miss, not overstepping. So door 4 stays
open *inside* the subset, and the recovery is journaled - `visible: false, known: true` -
because how often it happens is a number Pass 8a wants.
"""

from __future__ import annotations

import json

from tests.test_handoff_manifest import built, item

from agentd.agent.delegation import TaskSpec
from agentd.agent.loop import AgentLoop, Session
from agentd.agent.results import WorkerReport
from agentd.agent.subagents import SubagentSpec, run_subagent
from agentd.llm.fake import FakeProvider
from agentd.policy.approvals import AutoApprover
from agentd.policy.engine import engine_from_config
from agentd.tools import builtin_handoff
from agentd.tools.base import Tool, ToolContext, ToolResult, obj
from agentd.tools.executor import ToolExecutor
from agentd.tools.registry import ALWAYS_EXPOSE_LIMIT, Registry, tool_search_tool

# `ledger_probe` and the handoff lookup are registered and deliberately outside the surface:
# that is the whole situation under test, and it is why every refusal below has to stay
# distinguishable from "no such tool".
OUTSIDE = "ledger_probe"
# Filler, so that the *surface* - not just the registry - is larger than the number of tools
# a turn can be shown. Under that limit `select` returns everything it is allowed to return
# and its interesting routes (`session_used`, the embedding hits) are never reached, which
# would make two of the tests below pass for a reason they are not about.
FILLERS = tuple(f"filler_{i:02d}" for i in range(ALWAYS_EXPOSE_LIMIT))
SURFACE = ("time_probe", "tool_search", *FILLERS)
# A worker's is small, the way a real delegation's is: `SubagentSpec.tool_names` names a
# handful. No filler, so `select` shows a worker its whole subset - which is what makes the
# worker tests below about the other doors rather than about selection pressure.
WORKER_SURFACE = ("time_probe", "tool_search")


def _surface() -> tuple[Registry, list[str]]:
    """A registry holding the surface plus the two tools that are outside it, and the log
    of which handlers actually ran."""
    ran: list[str] = []

    def probe(name: str, description: str) -> Tool:
        async def handler(args, ctx):
            ran.append(name)
            return ToolResult(content=f"{name} ran")

        return Tool(
            name=name, description=description, parameters=obj(), handler=handler,
            effect_class="read", risk="read",
        )

    reg = Registry()
    reg.add(
        probe("time_probe", "says what the time is"),
        probe(OUTSIDE, "writes a row to the quarterly ledger"),
        *(probe(name, f"placeholder {name}") for name in FILLERS),
        *builtin_handoff.TOOLS,
    )
    reg.add(tool_search_tool(reg))
    return reg, ran


def _loop(cfg, registry: Registry, provider: FakeProvider, subset=SURFACE) -> AgentLoop:
    return AgentLoop(
        cfg=cfg, registry=registry, engine=engine_from_config(cfg),
        approver=AutoApprover(True), provider=provider, tool_subset=subset,
    )


async def _run(loop: AgentLoop, session: Session, text: str) -> None:
    async for _ in loop.run_turn(session, text):
        pass


def _offered(provider: FakeProvider, step: int = 0) -> list[str]:
    return provider.calls[step]["tools"]


def _tool_replies(provider: FakeProvider) -> list[str]:
    """What the model was handed back for each tool call, in order."""
    return [
        m["content"]
        for call in provider.calls
        for m in call.get("messages", [])
        if m.get("role") == "tool"
    ]


async def _worker(cfg, registry: Registry, provider: FakeProvider, task: str) -> None:
    """One delegated worker, through the real `run_subagent`.

    Its surface is its registry: `subagents.py` builds the worker's loop over
    `Registry(tools=registry.subset(spec.tool_names, ...))`, and `AgentLoop.tool_subset`
    falls back to the registry when no subset is named. So a worker gets the same
    enforcement the orchestrator gets, without `run_subagent` passing anything.
    """
    session = await Session.create("test")
    await run_subagent(
        SubagentSpec(
            name="researcher", prompt="be useful", tool_names=list(WORKER_SURFACE),
            max_steps=3,
        ),
        TaskSpec("researcher", task),
        parent_session_id=session.id, parent_turn_id=session.id, parent_autonomy="act",
        approver=AutoApprover(True), registry=registry, cfg=cfg, provider=provider,
    )


def _report() -> list[WorkerReport]:
    return [WorkerReport(status="completed", answer="Done.")]


# --- door 1: Registry.select ------------------------------------------------------------


async def test_selection_cannot_offer_the_orchestrator_a_tool_outside_its_surface(cfg):
    """Including through `session_used`, which is the route with a memory. It survives a
    resume, so a conversation that used a tool before the surface narrowed would otherwise
    carry it back in on the next message - the narrowing undone by continuing a chat."""
    registry, _ = _surface()
    provider = FakeProvider(turns=["nothing to do"])
    session = await Session.create("test")
    session.tools_used = {"time_probe", OUTSIDE}

    await _run(_loop(cfg, registry, provider), session, "write a ledger row")

    assert OUTSIDE not in _offered(provider)
    # The same route, for a tool that is inside the surface: this is a filter, not a
    # disabled feature.
    assert "time_probe" in _offered(provider)


async def test_selection_cannot_offer_a_worker_a_tool_outside_its_subset(cfg):
    """Shut twice over, and only one of the two is this change's. The worker's `Registry`
    *is* the subset (`agent/subagents.py`), so `select` has nothing else to return; the
    `permitted` filter is what would still hold if that construction changed."""
    registry, _ = _surface()
    provider = FakeProvider(turns=["nothing to do"], json_results=_report())

    await _worker(cfg, registry, provider, "write a ledger row")

    assert OUTSIDE not in _offered(provider)
    assert "time_probe" in _offered(provider)


# --- door 2: a handoff manifest from an earlier session ---------------------------------


async def test_a_handoff_manifest_cannot_put_the_lookup_back_on_the_orchestrator(cfg):
    """The one door where a decision made in an *earlier* session reaches into this run's
    surface. A manifest is written by the conversation that ran out of room and read by its
    successor; if the successor's surface has narrowed since, the manifest must not widen
    it back. `handoff_lookup` is the tool `_with_lookup` adds, so it is the tool that tests
    the door."""
    registry, _ = _surface()
    provider = FakeProvider(turns=["nothing to do"])
    session = await Session.create("test")
    session.handoff = built(manifest=(item(),))

    await _run(_loop(cfg, registry, provider), session, "what did I paste")

    assert builtin_handoff.LOOKUP not in _offered(provider)
    # And the successor is not told to call it either, which is the same decision one layer
    # up: `lookup_offered` drives the handoff block's instructions.
    assert builtin_handoff.LOOKUP not in provider.calls[0]["messages"][0]["content"]


async def test_a_handoff_manifest_cannot_put_the_lookup_back_on_a_worker(cfg):
    """Unreachable through `run_subagent` - it builds the worker a fresh `Session`, whose
    `handoff` is None, so a worker never carries one. Driven here against the worker-shaped
    loop that `subagents.py` builds, which is where the property has to hold if a worker is
    ever handed a session that has handed off. It is shut by construction: the lookup is not
    in the worker's registry, so `_with_lookup`'s `self.registry.get(LOOKUP)` finds nothing.
    """
    registry, _ = _surface()
    provider = FakeProvider(turns=["nothing to do"])
    worker_registry = Registry(tools=registry.subset(list(WORKER_SURFACE)))
    loop = AgentLoop(
        cfg=cfg, registry=worker_registry, engine=engine_from_config(cfg),
        approver=AutoApprover(True), provider=provider, actor="subagent:researcher",
    )
    session = await Session.create("test")
    session.handoff = built(manifest=(item(),))

    await _run(loop, session, "what did I paste")

    assert builtin_handoff.LOOKUP not in _offered(provider)


# --- door 3: tool_search ----------------------------------------------------------------


async def test_tool_search_does_not_advertise_a_tool_the_orchestrator_may_not_run(cfg):
    """The failure this prevents is not an extra tool call, it is a lie. `tool_search`
    answers with "These tools are now available for the rest of this turn"; a name outside
    the surface in that list is a promise the executor then breaks, and the model spends a
    step finding out."""
    registry, ran = _surface()
    provider = FakeProvider(
        turns=[[("tool_search", {"query": "ledger"})], "I cannot write to the ledger."]
    )
    session = await Session.create("test")

    await _run(_loop(cfg, registry, provider), session, "write a ledger row")

    assert OUTSIDE not in _tool_replies(provider)[0]
    assert OUTSIDE not in _offered(provider, 1)  # nor on the step after the search
    assert ran == []


async def test_tool_search_does_not_advertise_a_tool_a_worker_may_not_run(cfg):
    """This one is this change's alone, and it is the door Dylan suspected. The search
    closure is built over the *process* registry and then handed to a worker whose own
    registry is a subset of it - so before `ctx.tool_subset`, a worker searching for
    anything was searching the whole machine's tools and being told it now had them."""
    registry, ran = _surface()
    provider = FakeProvider(
        turns=[[("tool_search", {"query": "ledger"})], "I cannot write to the ledger."],
        json_results=_report(),
    )

    await _worker(cfg, registry, provider, "write a ledger row")

    assert OUTSIDE not in _tool_replies(provider)[0]
    assert OUTSIDE not in _offered(provider, 1)
    assert ran == []


# --- door 4: naming it outright ---------------------------------------------------------


async def test_naming_a_tool_outside_the_surface_does_not_run_it_or_make_it_visible(
    cfg, journaled
):
    """Two calls, because door 4 does two things and only the first is obvious.

    It refuses the call - the handler never runs. It also must not pick the name up into
    the turn's exposed set, because that set is what `visible` is read from: a second call
    to the same name would then be journaled `visible: true`, and the rate of
    `visible: false, known: true` is the measurement Pass 8a is going to read. A tool the
    model was never shown must not become "shown" by having been asked for.
    """
    registry, ran = _surface()
    provider = FakeProvider(
        turns=[[(OUTSIDE, {})], [(OUTSIDE, {})], "I cannot write to the ledger."]
    )
    session = await Session.create("test")

    await _run(_loop(cfg, registry, provider), session, "write a ledger row")

    assert ran == []
    error = json.loads(_tool_replies(provider)[0])["error"]
    assert "not part of this agent's tool surface" in error
    assert "No such tool" not in error  # a different fact, and `known` keeps them apart
    requested = [e for e in journaled("tool_requested") if e.payload["name"] == OUTSIDE]
    assert [e.payload["visible"] for e in requested] == [False, False]
    assert [e.payload["known"] for e in requested] == [True, True]
    failed = journaled("tool_failed")
    assert [e.payload["name"] for e in failed] == [OUTSIDE, OUTSIDE]
    assert "not part of this agent's tool surface" in failed[0].payload["error"]


async def test_naming_a_tool_outside_a_workers_subset_does_not_run_it(cfg):
    """Shut by construction, and the construction is worth naming: a worker's registry is
    its subset, so the name is not merely out of surface, it is unknown - door 4's `call.name
    in self.registry.tools` fails and the executor answers before the subset check. The
    message is deliberately unchanged from what a worker got before this change."""
    registry, ran = _surface()
    provider = FakeProvider(
        turns=[[(OUTSIDE, {})], "I cannot write to the ledger."], json_results=_report()
    )

    await _worker(cfg, registry, provider, "write a ledger row")

    assert ran == []
    assert json.loads(_tool_replies(provider)[0])["error"] == f"No such tool: {OUTSIDE}"


# --- the positive control ---------------------------------------------------------------


async def test_a_tool_inside_the_surface_still_runs_when_this_turn_did_not_offer_it(
    cfg, journaled
):
    """Without this, the suite cannot tell this design from the one it replaced.

    `select` is an embedding lookup over a registry larger than it can show at once, and it
    misses. A model that names an in-surface tool anyway is recovering from that miss, and
    the recovery is worth more than the tidiness of refusing it. It stays journaled as
    `visible: false, known: true`, which is how often the selector was wrong.
    """
    registry, ran = _surface()
    provider = FakeProvider(turns=[[(OUTSIDE, {})], [(OUTSIDE, {})], "Written."])
    session = await Session.create("test")

    await _run(
        _loop(cfg, registry, provider, subset=(*SURFACE, OUTSIDE)),
        session, "write a ledger row",
    )

    assert OUTSIDE not in _offered(provider)  # the turn really did not offer it
    assert ran == [OUTSIDE, OUTSIDE]  # and it ran anyway, both times
    requested = [e for e in journaled("tool_requested") if e.payload["name"] == OUTSIDE]
    # False then True: the first call is the retrieval miss, and the tool is part of the
    # turn from then on. That second True is the whole of door 4 - an out-of-surface name
    # never earns it (the test above), and an in-surface one must.
    assert [e.payload["visible"] for e in requested] == [False, True]
    assert [e.payload["known"] for e in requested] == [True, True]
    assert [e.payload["name"] for e in journaled("tool_finished")] == [OUTSIDE, OUTSIDE]


# --- the last line, and the callers that have no surface --------------------------------


async def test_the_executor_refuses_a_tool_outside_the_subset_with_no_loop_involved(cfg):
    """Defence in depth, and the reason the check is here rather than in the loop: the
    executor is the seam the main agent, a sub-agent, an approved queue item and an MCP
    caller all come through. A fifth route into a turn's tools - and Pass 8 has already
    found four - fails closed instead of open."""
    registry, ran = _surface()
    executor = ToolExecutor(registry.tools, engine_from_config(cfg), AutoApprover(True))

    result = await executor.run(OUTSIDE, {}, ToolContext(tool_subset=set(SURFACE)))

    assert not result.ok and ran == []
    assert "not part of this agent's tool surface" in json.loads(result.content)["error"]


async def test_a_caller_with_no_surface_of_its_own_is_not_refused(cfg):
    """`policy/replay.execute_approved` runs a queued call long after the turn that asked
    for it ended, and an MCP caller has no turn at all. Neither can say what its surface is,
    and neither may start failing - so an absent `tool_subset` means no enforcement, and an
    empty one means "nothing", which is a different statement. This is the test that fails
    if anyone tidies that default into an empty set."""
    registry, ran = _surface()
    executor = ToolExecutor(registry.tools, engine_from_config(cfg), AutoApprover(True))

    allowed = await executor.run(OUTSIDE, {}, ToolContext(actor="user", autonomy="act"))
    refused = await executor.run(OUTSIDE, {}, ToolContext(actor="main", tool_subset=set()))

    assert allowed.ok and ran == [OUTSIDE]
    assert not refused.ok
