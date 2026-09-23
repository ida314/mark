"""The delegation contract, which is a cache key before it is anything else.

Session 6c persists a completed worker's result under a hash of its task specification and
serves it back on a resume or a redundant in-run delegation. That makes two properties of
`TaskSpec` load-bearing, and they pull in opposite directions: two delegations that mean the
same thing have to produce the same spec, or the cache never hits and a resumed run pays
again for work it already did; two delegations that mean different things have to produce
different specs, or a cache hit hands back some other piece of work's answer as if it were
this one's. The second is the expensive direction, so the tests below pin the normalization
from both sides - what it is allowed to erase, and what it must never erase.

The rest is about the seam this session closed: a worker can no longer be started from an
ad-hoc string, an unknown durable role is refused rather than given a default worker, and
what a worker was asked to do is identifiable in the journal rather than only previewable.
"""

from __future__ import annotations

import os
import subprocess
import sys

import pytest

from agentd.agent.delegation import (
    SPEC_VERSION,
    DelegationError,
    EmptyTask,
    TaskSpec,
    UnknownRole,
    delegate,
)
from agentd.agent.loop import Session
from agentd.agent.subagents import SPECS, SubagentResult, SubagentSpec, run_subagent
from agentd.db import repo_archive
from agentd.llm.fake import FakeProvider
from agentd.llm.roles import set_provider
from agentd.policy.approvals import AutoApprover
from agentd.tools.registry import build_registry

# --- what the key may erase --------------------------------------------------


def test_two_delegations_that_differ_only_in_whitespace_are_one_delegation():
    spaced = TaskSpec(
        durable_role="researcher",
        task="Compare  the   three  proposals.\r\n\r\n\r\nPrefer primary sources.  ",
        relevant_context=["https://example.com/a "],
    )
    tidy = TaskSpec(
        durable_role="researcher",
        task="Compare the three proposals.\n\nPrefer primary sources.",
        relevant_context=["https://example.com/a"],
    )
    assert spaced == tidy
    assert spaced.digest == tidy.digest


def test_two_delegations_that_differ_only_in_unicode_composition_are_one_delegation():
    """Same rendered text, different bytes: sha256 would call these two different jobs."""
    composed = TaskSpec("researcher", "Find the café menu")
    decomposed = TaskSpec("researcher", "Find the café menu")
    assert composed.digest == decomposed.digest


def test_the_order_constraints_were_written_in_is_not_part_of_the_delegation():
    one = TaskSpec("coder", "fix the parser", constraints=["no new deps", "keep the tests green"])
    other = TaskSpec("coder", "fix the parser", constraints=["keep the tests green", "no new deps"])
    assert one == other
    assert one.constraints == ("keep the tests green", "no new deps")


def test_the_same_delegation_hashes_the_same_in_a_new_process():
    """The key outlives the process that computed it, so it cannot depend on set iteration.

    Deduplicating with a set and handing the result back in iteration order looks correct
    and is stable within one interpreter; `PYTHONHASHSEED` makes it a different key in the
    process that reads the cache back. Nothing in-process can catch that, so this test is
    two interpreters with two seeds.
    """
    spec = TaskSpec("coder", "fix the parser", constraints=["review", "no new deps", "be quick"])
    code = (
        "from agentd.agent.delegation import TaskSpec;"
        "print(TaskSpec('coder', 'fix the parser',"
        " constraints=['review', 'no new deps', 'be quick']).digest)"
    )
    seen = []
    for seed in ("0", "1", "424242"):
        out = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True,
            env={**os.environ, "PYTHONHASHSEED": seed},
        )
        assert out.returncode == 0, out.stderr
        seen.append(out.stdout.strip())
    assert seen == [spec.digest] * 3


def test_a_constraint_said_twice_is_one_constraint():
    spec = TaskSpec("coder", "fix the parser", constraints=["no new deps", "no  new deps"])
    assert spec.constraints == ("no new deps",)


# --- what the key must never erase -------------------------------------------


def test_a_delegation_that_differs_by_one_word_is_a_different_delegation():
    """The expensive direction: a cache hit here would answer a question nobody asked."""
    keep = TaskSpec("coder", "set the token expiry to 24 hours")
    other = TaskSpec("coder", "set the token expiry to 48 hours")
    assert keep.digest != other.digest


def test_case_is_meaning_and_is_not_normalized_away():
    assert TaskSpec("coder", "read Config.py").digest != TaskSpec("coder", "read config.py").digest


def test_a_constraint_the_caller_added_changes_the_delegation():
    bare = TaskSpec("coder", "fix the parser")
    limited = TaskSpec("coder", "fix the parser", constraints=["do not touch the schema"])
    assert bare.digest != limited.digest


def test_the_same_task_given_to_two_roles_is_two_delegations():
    assert TaskSpec("coder", "read the file").digest != TaskSpec("researcher", "read the file").digest


# --- refusals ----------------------------------------------------------------


def test_an_unknown_durable_role_is_refused_rather_than_given_a_default_worker():
    with pytest.raises(UnknownRole) as exc:
        TaskSpec("analyst", "do the thing")
    assert "researcher" in str(exc.value)  # it says what does exist
    assert set(SPECS) == {"researcher", "coder"}


def test_a_task_of_only_whitespace_is_refused_rather_than_delegated_empty():
    with pytest.raises(EmptyTask):
        TaskSpec("researcher", "   \n\n  ")


def test_a_context_string_is_one_piece_of_context_and_not_a_list_of_characters():
    spec = TaskSpec("researcher", "look it up", relevant_context="the lease renews in March")
    assert spec.relevant_context == ("the lease renews in March",)


def test_a_spec_written_under_other_normalization_rules_is_refused():
    """A stored spec is only comparable to a fresh one under the rules that made it."""
    stored = TaskSpec("researcher", "look it up").as_dict()
    stored["spec_version"] = SPEC_VERSION + 1
    with pytest.raises(DelegationError):
        TaskSpec.from_dict(stored)


# --- what the worker is told -------------------------------------------------


def test_a_delegation_that_does_not_say_what_to_return_carries_the_roles_own_contract():
    """The default is written into the spec, so it is in the key and in the record.

    A default applied later - at render time, by whoever builds the message - would make two
    specs that hash the same hand two different briefs to two workers.
    """
    silent = TaskSpec("researcher", "compare the proposals")
    assert silent.expected_output == SPECS["researcher"].expected_output
    assert silent.expected_output in silent.canonical()
    spoken = TaskSpec(
        "researcher", "compare the proposals",
        expected_output=SPECS["researcher"].expected_output,
    )
    assert silent.digest == spoken.digest


def test_the_brief_carries_what_the_worker_cannot_see_for_itself():
    spec = TaskSpec(
        "coder", "fix the parser",
        relevant_context=["the failing case is in tests/test_parser.py"],
        constraints=["no new dependencies"],
        expected_output="the files you changed",
    )
    brief = spec.brief
    assert brief.startswith("fix the parser")
    assert "- the failing case is in tests/test_parser.py" in brief
    assert "- no new dependencies" in brief
    assert brief.endswith("Report back:\nthe files you changed")


def test_a_spec_survives_the_round_trip_it_will_take_through_a_checkpoint():
    spec = TaskSpec(
        "coder", "fix the parser", relevant_context=["tests/test_parser.py"],
        constraints=["no new deps"],
    )
    back = TaskSpec.from_dict(spec.as_dict())
    assert back == spec
    assert back.digest == spec.digest


# --- the executor ------------------------------------------------------------


def _worker_spec(**kwargs) -> SubagentSpec:
    defaults = dict(name="researcher", prompt="be useful", tool_names=["fs_read"], max_steps=3)
    defaults.update(kwargs)
    return SubagentSpec(**defaults)


async def test_a_worker_cannot_be_started_from_a_bare_string(cfg):
    """The ad-hoc path is closed, loudly. A coerced string would be a second way to
    normalize a delegation, and the two could disagree about the key a result is cached
    under - the one disagreement 6c has no way to notice."""
    session = await Session.create("test")
    with pytest.raises(TypeError) as exc:
        await run_subagent(
            _worker_spec(), "find the thing", parent_session_id=session.id,
            parent_turn_id=session.id, parent_autonomy="assist",
            approver=AutoApprover(True), registry=build_registry(), cfg=cfg,
            provider=FakeProvider(turns=["done"]),
        )
    assert "TaskSpec" in str(exc.value)


async def test_a_worker_is_told_the_constraints_the_delegation_put_on_it(cfg):
    provider = FakeProvider(
        turns=["I made the change."],
        json_results=[SubagentResult(status="ok", summary="Done.")],
    )
    session = await Session.create("test")
    spec = TaskSpec(
        "coder", "fix the parser",
        relevant_context=["the failing case is in tests/test_parser.py"],
        constraints=["no new dependencies"],
    )
    await run_subagent(
        _worker_spec(name="coder"), spec, parent_session_id=session.id,
        parent_turn_id=session.id, parent_autonomy="act", approver=AutoApprover(True),
        registry=build_registry(), cfg=cfg, provider=provider,
    )
    sent = "\n".join(
        m["content"] for m in provider.calls[0]["messages"] if isinstance(m.get("content"), str)
    )
    assert "no new dependencies" in sent
    assert "tests/test_parser.py" in sent

    archived = [
        e for e in await repo_archive.events_for_session(session.id)
        if e["kind"] == "subagent_message"
    ]
    # The full brief is the archive's, not the journal's 200 characters: a re-delegation
    # has to be buildable from what was really sent.
    assert archived and "no new dependencies" in archived[0]["content"]


async def test_the_journal_says_which_specification_was_delegated(cfg, journaled):
    provider = FakeProvider(turns=["done"], json_results=[SubagentResult(summary="ok")])
    session = await Session.create("test")
    spec = TaskSpec("researcher", "compare the proposals", constraints=["primary sources only"])
    await run_subagent(
        _worker_spec(), spec, parent_session_id=session.id, parent_turn_id=session.id,
        parent_autonomy="assist", approver=AutoApprover(True), registry=build_registry(),
        cfg=cfg, provider=provider,
    )
    created = journaled("worker_created")
    assert [e.payload["task_digest"] for e in created] == [spec.digest]
    # `task_preview` is a truncation and cannot identify a delegation; the digest can.
    assert created[0].payload["task_preview"] != spec.brief


async def test_the_delegate_tool_and_a_direct_delegation_produce_the_same_spec(cfg, journaled):
    """One interface: the model's door and the runtime's door build the same key."""
    from agentd.tools import builtin_delegate
    from agentd.tools.base import ToolContext

    provider = FakeProvider(
        turns=["done", "done"],
        json_results=[SubagentResult(summary="ok"), SubagentResult(summary="ok")],
    )
    set_provider(provider)
    session = await Session.create("test")
    ctx = ToolContext(
        session_id=session.id, turn_id=session.id, autonomy="assist", run_id="run-tool",
        step_id="s1", extra={"approver": AutoApprover(True)},
    )
    result = await builtin_delegate.delegate.handler(
        {
            "agent": "researcher",
            "task": "Compare  the three proposals.",
            "context": "the lease renews in March",
            "constraints": ["primary sources only"],
        },
        ctx,
    )
    assert result.ok

    direct = await delegate(
        "researcher",
        "Compare the three proposals.",
        relevant_context=["the lease renews in March"],
        constraints=["primary sources only"],
        parent_session_id=session.id, parent_turn_id=session.id, parent_autonomy="assist",
        approver=AutoApprover(True), registry=build_registry(), cfg=cfg, provider=provider,
        parent_run_id="run-direct",
    )
    assert direct.status == "ok"

    digests = [e.payload["task_digest"] for e in journaled("worker_created")]
    assert len(digests) == 2
    assert digests[0] == digests[1]


async def test_the_delegate_tool_refuses_an_unknown_role_instead_of_starting_a_worker(cfg):
    from agentd.tools import builtin_delegate
    from agentd.tools.base import ToolContext

    # A provider that would notice: if the refusal ever stops happening, this test fails on
    # a scripted model rather than reaching for the real one over the network.
    set_provider(FakeProvider(turns=["done"], json_results=[SubagentResult(summary="ok")]))
    session = await Session.create("test")
    ctx = ToolContext(
        session_id=session.id, turn_id=session.id, autonomy="assist",
        extra={"approver": AutoApprover(True)},
    )
    result = await builtin_delegate.delegate.handler({"agent": "analyst", "task": "dig"}, ctx)
    assert not result.ok
    assert "analyst" in result.content
