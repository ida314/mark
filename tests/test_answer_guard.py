"""The no-action guard: an answer may not say "done" over a turn that called nothing.

Session 01a0e9f6 (2026-09-28): a one-step, zero-tool-call turn wrote "Let me actually do it
now. Now I'll read it back. Done. The file contains `hello world`." No file existed. The
runtime's own count of the turn's calls was zero; nothing compared that count to the prose.
"""

from __future__ import annotations

import pytest

from agentd.agent.loop import NO_ACTION_NOTE, AgentLoop, Session, claims_action
from agentd.agent.stream import Answer
from agentd.db import repo_archive
from agentd.llm.fake import FakeProvider
from agentd.policy.approvals import AutoApprover
from agentd.policy.engine import engine_from_config
from agentd.tools.base import ToolResult
from agentd.tools.registry import Registry


def _registry(*names: str) -> Registry:
    from agentd.tools import builtin_fs

    reg = Registry()
    reg.add(*(t for t in builtin_fs.TOOLS if t.name in names))
    return reg


def _loop(cfg, provider: FakeProvider) -> AgentLoop:
    return AgentLoop(
        cfg=cfg, registry=_registry("fs_read"), tool_subset=["fs_read"],
        engine=engine_from_config(cfg), approver=AutoApprover(True), provider=provider,
    )


async def _answer(loop: AgentLoop, session: Session, text: str) -> str:
    return [e async for e in loop.run_turn(session, text) if isinstance(e, Answer)][0].text


@pytest.mark.parametrize(
    "text",
    [
        "Done. The file contains `hello world`.",
        "Let me actually do it now.\nNow I'll read it back.\n**Done.** Here is what it says.",
        "I've created the file at /home/dylan/agent-test.md.",
        "I have now written the note and read it back.",
        "The file has been created with the content you asked for.",
        "The tests were run and all 19 pass.",
        "I just deleted the stale entries.",
    ],
)
def test_a_claim_of_completed_work_is_recognised(text: str) -> None:
    assert claims_action(text)


@pytest.mark.parametrize(
    "text",
    [
        "I can create the file if you like - where should it go?",
        "Shall I create it now?",
        "I will create the file and read it back.",
        "What does 'done' mean in this context?",
        "The write is sitting in your approval queue and has not run.",
        "I don't have a note-taking tool in this session.",
    ],
)
def test_an_offer_a_question_or_a_refusal_is_not_a_claim(text: str) -> None:
    assert claims_action(text) is None


async def test_a_claim_over_zero_calls_is_flagged_and_the_answer_says_so(cfg, journaled) -> None:
    provider = FakeProvider(turns=["Done. I've created the file and read it back."])
    session = await Session.create("test")
    answer = await _answer(_loop(cfg, provider), session, "create the file")

    assert answer.endswith(NO_ACTION_NOTE)
    flagged = journaled("answer_flagged")
    assert len(flagged) == 1
    assert flagged[0].payload["reason"] == "action_claimed_without_tools"
    assert flagged[0].payload["phrase"].startswith("Done.")
    # Archived with the note on it, so the next turn's history says it too.
    rows = await repo_archive.recent_messages(session.id)
    assert rows[-1]["kind"] == "assistant_message" and NO_ACTION_NOTE in rows[-1]["content"]


async def test_the_same_words_over_a_real_call_are_left_alone(cfg, journaled) -> None:
    target = cfg.paths.roots()[0] / "note.txt"
    target.write_text("hello world")
    provider = FakeProvider(turns=[[("fs_read", {"path": str(target)})], "Done. It says hello world."])
    session = await Session.create("test")
    answer = await _answer(_loop(cfg, provider), session, "read the note")

    assert NO_ACTION_NOTE not in answer
    assert journaled("answer_flagged") == []


async def test_a_failed_call_still_counts_as_having_tried(cfg, journaled) -> None:
    """The guard is about calling nothing, not about succeeding. A turn whose one call was
    refused and which then says "done" is a different lie, and one the tool result already
    exposes to the reader."""
    provider = FakeProvider(turns=[[("fs_read", {"path": "/nowhere/at/all"})], "Done."])
    session = await Session.create("test")
    answer = await _answer(_loop(cfg, provider), session, "read it")

    assert NO_ACTION_NOTE not in answer
    assert journaled("answer_flagged") == []


async def test_a_turn_with_no_tools_on_the_table_is_never_flagged(cfg, journaled) -> None:
    """Nothing to call means nothing it could have called; the note would be noise."""
    provider = FakeProvider(turns=["Done. I've created it."])
    loop = AgentLoop(
        cfg=cfg, registry=Registry(), tool_subset=[], engine=engine_from_config(cfg),
        approver=AutoApprover(True), provider=provider,
    )
    session = await Session.create("test")
    answer = await _answer(loop, session, "create it")

    assert NO_ACTION_NOTE not in answer
    assert journaled("answer_flagged") == []


# --- fs_read says which of two things is wrong --------------------------------


async def test_fs_read_tells_a_missing_file_from_a_directory(cfg) -> None:
    from agentd.tools import builtin_fs
    from agentd.tools.base import ToolContext

    root = cfg.paths.roots()[0]
    ctx = ToolContext(actor="test", origin="test", autonomy="assist")
    missing: ToolResult = await builtin_fs.fs_read.handler({"path": str(root / "nope.md")}, ctx)
    folder: ToolResult = await builtin_fs.fs_read.handler({"path": str(root)}, ctx)

    assert missing.ok is False and missing.content.startswith("No such file")
    assert folder.ok is False and "directory" in folder.content
