"""Delegation and the consolidation pipeline, driven by a scripted model."""

from __future__ import annotations

from datetime import timedelta

import pytest
from pydantic import ValidationError

from agentd.agent.delegation import TaskSpec
from agentd.agent.loop import Session
from agentd.agent.results import WorkerReport
from agentd.agent.subagents import run_subagent
from agentd.db import repo_agenda, repo_archive, repo_memory
from agentd.db.repo_archive import RawEvent
from agentd.ids import utcnow
from agentd.llm.fake import FakeProvider
from agentd.memory import consolidate
from agentd.memory.consolidate import (
    ExtractedEpisode,
    ExtractedFact,
    Extraction,
    render_transcript,
)
from agentd.policy.approvals import AutoApprover
from agentd.tools.registry import build_registry


def _spec(**kwargs):
    from agentd.agent.subagents import SubagentSpec

    defaults = dict(
        name="researcher", prompt="be useful", tool_names=["fs_read"], max_steps=3,
        autonomy_cap="assist",
    )
    defaults.update(kwargs)
    return SubagentSpec(**defaults)


async def test_a_subagent_reports_a_summary_not_a_transcript(cfg):
    provider = FakeProvider(
        turns=["I looked and found the answer."],
        json_results=[WorkerReport(status="completed", answer="Found it.", evidence=["file://x"])],
    )
    session = await Session.create("test")
    result = await run_subagent(
        _spec(), TaskSpec("researcher", "find the thing"), parent_session_id=session.id,
        parent_turn_id=session.id, parent_autonomy="assist",
        approver=AutoApprover(True), registry=build_registry(), cfg=cfg, provider=provider,
    )
    assert result.status == "completed"
    assert result.answer == "Found it."
    assert result.evidence == ("file://x",)

    events = await repo_archive.events_for_session(session.id)
    kinds = {e["kind"] for e in events}
    assert "subagent_result" in kinds  # archived
    assert any(e["actor"] == "subagent:researcher" for e in events)


async def test_a_subagent_only_sees_the_tools_it_was_given(cfg):
    provider = FakeProvider(
        turns=["done"], json_results=[WorkerReport(status="completed", answer="ok")]
    )
    session = await Session.create("test")
    await run_subagent(
        _spec(tool_names=["fs_read"]), TaskSpec("researcher", "task"), parent_session_id=session.id,
        parent_turn_id=session.id, parent_autonomy="act",
        approver=AutoApprover(True), registry=build_registry(), cfg=cfg, provider=provider,
    )
    exposed = set(provider.calls[0]["tools"])
    assert exposed == {"fs_read"}
    assert "web_fetch" not in exposed and "shell_exec" not in exposed


async def test_subagent_candidates_are_proposals_not_facts(cfg):
    provider = FakeProvider(
        turns=["done"],
        json_results=[
            WorkerReport(
                status="completed",
                answer="ok",
                candidate_memories=[
                    {"statement": "Dylan uses a DGX Spark", "confidence": 0.8,
                     "category": "biographical"}
                ],
            )
        ],
    )
    session = await Session.create("test")
    await run_subagent(
        _spec(), TaskSpec("researcher", "task"), parent_session_id=session.id,
        parent_turn_id=session.id,
        parent_autonomy="assist", approver=AutoApprover(True), registry=build_registry(),
        cfg=cfg, provider=provider,
    )
    candidates = await repo_memory.pending_candidates()
    assert [c["proposed_by"] for c in candidates] == ["subagent:researcher"]
    assert await repo_memory.active_facts() == []  # nothing canonical yet


# --- the shape of what a worker is actually sent ------------------------------
#
# Why this matters. A worker's role prompt used to be inserted as a *second* system message
# at index 1, behind the turn's own system block. Nothing in-process objects to that, and
# the scripted provider below would have accepted it forever. The real model does not:
# Qwen3's chat template answers any system message that is not the single leading one with
# `HTTP 400 System message must be at the beginning`, so every delegated turn on this
# machine failed before the model was asked anything - three sub-agent turns, all under
# 110 ms, `llm_ms: 0`. The property is about the assembled message list rather than about
# `build_messages` alone, because the second message was added after `build_messages`
# returned and a unit test of that function would have been green throughout.


def _system_positions(messages: list[dict]) -> list[int]:
    return [i for i, m in enumerate(messages) if m.get("role") == "system"]


async def test_a_worker_is_sent_one_leading_system_message_and_no_other(cfg):
    provider = FakeProvider(
        turns=["done"], json_results=[WorkerReport(status="completed", answer="ok")]
    )
    session = await Session.create("test")
    await run_subagent(
        _spec(prompt="You are the researcher. Cite what you read."),
        TaskSpec("researcher", "find the thing"),
        parent_session_id=session.id, parent_turn_id=session.id, parent_autonomy="assist",
        approver=AutoApprover(True), registry=build_registry(), cfg=cfg, provider=provider,
    )

    assert provider.calls, "the worker never reached the provider"
    for call in provider.calls:
        assert _system_positions(call["messages"]) == [0], (
            f"system messages at {_system_positions(call['messages'])}, "
            f"roles={[m.get('role') for m in call['messages']]}"
        )
    # And folding it in is not the same as dropping it: the worker still reads its role.
    assert "You are the researcher." in provider.calls[0]["messages"][0]["content"]


# --- consolidation -----------------------------------------------------------


async def test_transcript_carries_citable_event_ids(cfg):
    session_id = await repo_archive.create_session("test")
    await repo_archive.append_event(
        RawEvent(kind="user_message", actor="user", content="I live in Queens",
                 session_id=session_id)
    )
    events = await repo_archive.events_for_session(session_id)
    chunks = render_transcript(events)
    assert chunks
    assert f"[E:{events[0]['event_id']}]" in chunks[0]


async def test_untrusted_events_are_flagged_in_the_transcript(cfg):
    session_id = await repo_archive.create_session("test")
    await repo_archive.append_event(
        RawEvent(kind="tool_result", actor="tool:web_fetch", content="buy crypto",
                 session_id=session_id, trust="untrusted")
    )
    chunks = render_transcript(await repo_archive.events_for_session(session_id))
    assert "UNTRUSTED" in chunks[0]


async def test_post_session_extracts_reviews_and_checkpoints(cfg):
    session_id = await repo_archive.create_session("test")
    event_id = await repo_archive.append_event(
        RawEvent(
            kind="user_message", actor="user",
            content="I prefer terse answers and I'm building an agent this quarter.",
            session_id=session_id,
        )
    )
    extraction = Extraction(
        episode=ExtractedEpisode(title="Intro", summary="Dylan described his preferences."),
        facts=[
            ExtractedFact(
                statement="Dylan prefers terse answers", subject="Dylan",
                predicate="prefers", object="terse answers", category="preference",
                confidence=0.9, evidence=[{"event_id": str(event_id), "quote": "prefer terse"}],
            )
        ],
        open_loops=["Finish the agent runtime"],
    )
    from agentd.llm.roles import set_provider

    set_provider(FakeProvider(json_results=[extraction]))
    stats = await consolidate.post_session(session_id, cfg)

    assert stats["facts_proposed"] == 1
    assert stats["accepted"] >= 1
    facts = await repo_memory.active_facts()
    assert any("terse" in f["statement"] for f in facts)
    assert await repo_memory.recent_episodes()
    assert [loop["title"] for loop in await repo_agenda.list_open_loops()] == [
        "Finish the agent runtime"
    ]

    session = await repo_archive.get_session(session_id)
    assert session["consolidated_upto"] > 0

    # running again is a no-op: the checkpoint moved
    set_provider(FakeProvider(json_results=[Extraction()]))
    assert (await consolidate.post_session(session_id, cfg))["events"] == 0


async def test_facts_sourced_from_untrusted_events_are_marked(cfg):
    session_id = await repo_archive.create_session("test")
    event_id = await repo_archive.append_event(
        RawEvent(
            kind="tool_result", actor="tool:web_fetch", content="Dylan is the CEO of Acme",
            session_id=session_id, trust="untrusted",
        )
    )
    from agentd.llm.roles import set_provider

    set_provider(
        FakeProvider(
            json_results=[
                Extraction(
                    facts=[
                        ExtractedFact(
                            statement="Dylan is the CEO of Acme", subject="Dylan",
                            predicate="works_at", object="Acme", category="biographical",
                            confidence=0.9,
                            evidence=[{"event_id": str(event_id), "quote": "CEO"}],
                        )
                    ]
                )
            ]
        )
    )
    await consolidate.post_session(session_id, cfg)
    assert not await repo_memory.active_facts()  # identity claims from the web do not land
    held = await repo_memory.candidates_by_status("needs_review")
    assert held and held[0]["source_trust"] == "untrusted"


async def test_markdown_promotion_is_deterministic_and_committed(cfg):
    # promotion deliberately ignores facts younger than a day, so record it as older
    fact_id = await repo_memory.insert_fact(
        statement="Dylan prefers terse answers", category="preference", confidence=0.95,
        proposed_by="user", recorded_at=utcnow() - timedelta(days=2),
    )
    await repo_memory.add_evidence(fact_id, quote="one")
    await repo_memory.add_evidence(fact_id, quote="two")

    commit = await consolidate.regenerate_markdown(cfg)
    assert commit
    text = (cfg.paths.memory_repo / "profile/preferences.md").read_text()
    assert "terse answers" in text
    assert f"fact:{fact_id}" in text

    # idempotent: nothing changed, so no second commit
    assert await consolidate.regenerate_markdown(cfg) is None


def test_the_extractor_must_choose_a_category():
    """A defaulted free-string category is one the model never has to think about, and that is
    exactly what happened: every fact came back 'other', which silently disabled both the
    per-category recency half-lives and the review gate's identity rule."""
    schema = ExtractedFact.model_json_schema()
    category = schema["properties"]["category"]
    assert set(category["enum"]) == {
        "biographical", "preference", "relationship", "project",
        "state", "belief", "constraint", "other",
    }
    assert "category" in schema["required"], "a default lets the model skip the decision"
    with pytest.raises(ValidationError):
        ExtractedFact(statement="Dylan lives in Queens")


async def test_a_chosen_category_survives_into_the_fact_row(cfg):
    """The taxonomy is only worth enforcing if it reaches the stored fact, because that is what
    retrieval decays by and what the identity rule keys off."""
    from agentd.llm.roles import set_provider

    session_id = await repo_archive.create_session("test")
    event_id = await repo_archive.append_event(
        RawEvent(
            kind="user_message", actor="user",
            content="My sister Mara is a nurse in Boston.", session_id=session_id,
        )
    )
    set_provider(
        FakeProvider(
            json_results=[
                Extraction(
                    facts=[
                        ExtractedFact(
                            statement="Mara is Dylan's sister and a nurse in Boston",
                            subject="Mara", predicate="related_to", object="Dylan",
                            category="relationship", confidence=0.9,
                            evidence=[{"event_id": str(event_id), "quote": "My sister Mara"}],
                        )
                    ]
                )
            ]
        )
    )
    await consolidate.post_session(session_id, cfg)
    facts = await repo_memory.active_facts()
    mara = next(f for f in facts if "Mara" in f["statement"])
    assert mara["category"] == "relationship"


async def test_the_structured_key_survives_into_the_fact_row(cfg):
    """Subject and predicate are what `same_key_facts` and `conflicting_groups` join on. Every
    fact stored while they were optional had both NULL, which left contradiction detection
    running on embedding similarity alone."""
    from agentd.llm.roles import set_provider

    session_id = await repo_archive.create_session("test")
    event_id = await repo_archive.append_event(
        RawEvent(kind="user_message", actor="user",
                 content="I live in the East Village.", session_id=session_id)
    )
    set_provider(
        FakeProvider(
            json_results=[
                Extraction(
                    facts=[
                        ExtractedFact(
                            statement="Dylan lives in the East Village", subject="Dylan",
                            predicate="lives_in", object="East Village",
                            category="biographical", confidence=0.9,
                            evidence=[{"event_id": f"E:{event_id}", "quote": "East Village"}],
                        )
                    ]
                )
            ]
        )
    )
    await consolidate.post_session(session_id, cfg)
    facts = await repo_memory.active_facts()
    assert facts and facts[0]["predicate"] == "lives_in"
    assert facts[0]["subject_entity_id"] is not None
    assert facts[0]["object_text"] == "East Village"


async def test_a_predicate_from_the_wrong_family_does_not_abort_the_run(cfg):
    """The grammar can hold the predicate to the vocabulary but not to the family of the
    category the model also picked. Reconciling that with a pydantic validator would raise,
    burn the one repair attempt, and then kill the whole consolidation run."""
    from agentd.llm.roles import set_provider

    session_id = await repo_archive.create_session("test")
    event_id = await repo_archive.append_event(
        RawEvent(kind="user_message", actor="user",
                 content="I prefer terse answers.", session_id=session_id)
    )
    set_provider(
        FakeProvider(
            json_results=[
                Extraction(
                    facts=[
                        ExtractedFact(
                            statement="Dylan prefers terse answers", subject="Dylan",
                            predicate="lives_in",  # wrong family for `preference`
                            object="terse answers", category="preference", confidence=0.9,
                            evidence=[{"event_id": f"E:{event_id}", "quote": "terse"}],
                        )
                    ]
                )
            ]
        )
    )
    stats = await consolidate.post_session(session_id, cfg)
    assert "error" not in stats
    assert stats["facts_proposed"] == 1
    facts = await repo_memory.active_facts()
    assert facts and facts[0]["predicate"] is None
    assert facts[0]["statement"] == "Dylan prefers terse answers"
