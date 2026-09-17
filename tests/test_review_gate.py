"""The review gate is the only writer of canonical memory, so its decisions are tested."""

from __future__ import annotations

from agentd.db import repo_memory
from agentd.db.pool import fetch_one
from agentd.llm.fake import FakeProvider
from agentd.llm.roles import set_provider
from agentd.memory import review
from agentd.memory.review import JudgeVerdict, contains_secret


async def _candidate(**kwargs) -> dict:
    defaults = dict(
        statement="Dylan prefers terse answers",
        proposed_by="consolidator",
        confidence=0.8,
        structured={"category": "preference"},
        evidence=[{"quote": "keep it short"}],
    )
    defaults.update(kwargs)
    cid = await repo_memory.insert_candidate(**defaults)
    rows = [c for c in await repo_memory.pending_candidates() if c["id"] == cid]
    return rows[0]


async def test_a_confident_candidate_becomes_a_fact(cfg):
    status, _ = await review.process_candidate(await _candidate(), cfg)
    assert status == "accepted"
    facts = await repo_memory.active_facts()
    assert len(facts) == 1
    assert facts[0]["statement"] == "Dylan prefers terse answers"


async def test_a_candidate_with_no_evidence_is_rejected(cfg):
    status, reason = await review.process_candidate(await _candidate(evidence=[]), cfg)
    assert status == "rejected"
    assert "evidence" in reason


async def test_the_user_may_propose_without_evidence(cfg):
    status, _ = await review.process_candidate(
        await _candidate(proposed_by="user", evidence=[], confidence=0.95), cfg
    )
    assert status == "accepted"


async def test_low_confidence_is_rejected_outright(cfg):
    status, _ = await review.process_candidate(await _candidate(confidence=0.2), cfg)
    assert status == "rejected"


async def test_middling_confidence_waits_for_the_user(cfg):
    status, _ = await review.process_candidate(await _candidate(confidence=0.65), cfg)
    assert status == "needs_review"


async def test_credentials_are_never_stored(cfg):
    status, reason = await review.process_candidate(
        await _candidate(statement="Dylan's api_key = sk-abcdef0123456789abcdef"), cfg
    )
    assert status == "rejected"
    assert "credential" in reason


def test_secret_detection_covers_common_shapes():
    assert contains_secret("api_key: abcdef123456")
    assert contains_secret("-----BEGIN OPENSSH PRIVATE KEY-----")
    assert contains_secret("ghp_abcdefghijklmnopqrstuvwxyz")
    assert not contains_secret("Dylan prefers terse answers")


async def test_untrusted_sources_cannot_change_who_the_user_is(cfg):
    status, reason = await review.process_candidate(
        await _candidate(
            statement="Dylan is the CEO of Acme",
            structured={"category": "biographical"},
            source_trust="untrusted",
        ),
        cfg,
    )
    assert status == "needs_review"
    assert "untrusted" in reason


async def test_a_repeated_claim_merges_and_strengthens(cfg):
    await review.process_candidate(await _candidate(), cfg)
    before = (await repo_memory.active_facts())[0]

    status, _ = await review.process_candidate(await _candidate(), cfg)
    after = (await repo_memory.active_facts())[0]

    assert status == "merged"
    assert len(await repo_memory.active_facts()) == 1
    assert after["confidence"] > before["confidence"]
    row = await fetch_one(
        "SELECT count(*) AS c FROM fact_evidence WHERE fact_id = %s", (after["id"],)
    )
    assert row["c"] == 2


async def test_when_the_world_changes_the_old_fact_is_superseded(cfg):
    subject = await repo_memory.upsert_entity("person", "Dylan")
    old = await repo_memory.insert_fact(
        statement="Dylan lives in Brooklyn", category="biographical", confidence=0.9,
        proposed_by="user", subject_entity_id=subject, predicate="lives_in",
        object_text="Brooklyn",
    )
    set_provider(
        FakeProvider(json_results=[JudgeVerdict(relation="updates", valid_from="2026-08-01")])
    )
    status, _ = await review.process_candidate(
        await _candidate(
            statement="Dylan lives in Queens",
            structured={
                "category": "biographical", "subject": "Dylan", "predicate": "lives_in",
                "object": "Queens",
            },
            confidence=0.9,
        ),
        cfg,
    )
    assert status == "superseding"
    old_row = await repo_memory.get_fact(old)
    assert old_row["status"] == "superseded"
    assert old_row["valid_to"] is not None
    assert old_row["superseded_by"] is not None


async def test_a_correction_retracts_without_closing_the_validity_window(cfg):
    subject = await repo_memory.upsert_entity("person", "Dylan")
    wrong = await repo_memory.insert_fact(
        statement="Dylan lives in Paris", category="biographical", confidence=0.9,
        proposed_by="user", subject_entity_id=subject, predicate="lives_in",
        object_text="Paris",
    )
    set_provider(FakeProvider(json_results=[JudgeVerdict(relation="corrects")]))
    status, _ = await review.process_candidate(
        await _candidate(
            statement="Dylan lives in Queens",
            structured={
                "category": "biographical", "subject": "Dylan", "predicate": "lives_in",
                "object": "Queens",
            },
            confidence=0.9,
        ),
        cfg,
    )
    assert status == "superseding"
    row = await repo_memory.get_fact(wrong)
    assert row["status"] == "retracted"
    assert row["valid_to"] is None  # it was never true, so nothing to close


async def test_an_uncertain_conflict_goes_to_the_user(cfg):
    subject = await repo_memory.upsert_entity("person", "Dylan")
    await repo_memory.insert_fact(
        statement="Dylan lives in Brooklyn", category="biographical", confidence=0.9,
        proposed_by="user", subject_entity_id=subject, predicate="lives_in",
        object_text="Brooklyn",
    )
    set_provider(FakeProvider(json_results=[JudgeVerdict(relation="contradicts_uncertain")]))
    status, _ = await review.process_candidate(
        await _candidate(
            statement="Dylan lives in Queens",
            structured={
                "category": "biographical", "subject": "Dylan", "predicate": "lives_in",
                "object": "Queens",
            },
            confidence=0.9,
        ),
        cfg,
    )
    assert status == "needs_review"


async def test_open_loop_candidates_reach_the_agenda(cfg):
    from agentd.db import repo_agenda

    status, _ = await review.process_candidate(
        await _candidate(statement="Send the lease back", kind="open_loop", confidence=0.9), cfg
    )
    assert status == "accepted"
    loops = await repo_agenda.list_open_loops()
    assert [loop["title"] for loop in loops] == ["Send the lease back"]


async def test_process_pending_reports_what_it_did(cfg):
    await _candidate()
    await _candidate(statement="Dylan works on a personal agent", confidence=0.9)
    stats = await review.process_pending(cfg)
    assert stats.accepted == 2
    assert not await repo_memory.pending_candidates()
