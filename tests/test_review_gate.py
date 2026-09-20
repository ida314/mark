"""The review gate is the only writer of canonical memory, so its decisions are tested."""

from __future__ import annotations

from agentd.db import repo_memory
from agentd.db.pool import fetch_one
from agentd.llm.fake import FakeProvider
from agentd.llm.roles import set_provider
from agentd.memory import review
from agentd.memory.cues import detect_correction
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


async def test_an_evidence_citation_keeps_its_link_to_the_raw_event(cfg):
    """The transcript labels each line `[E:<uuid>]` and the model copies the marker whole.
    Parsing that as a uuid raises, and the failure used to be swallowed into NULL — which is
    how every stored fact ended up with evidence pointing at nothing."""
    from agentd.db import repo_archive

    session_id = await repo_archive.create_session("test")
    event_id = await repo_archive.append_event(
        repo_archive.RawEvent(
            kind="user_message", actor="user", content="I live in the East Village",
            session_id=session_id,
        )
    )
    await review.process_candidate(
        await _candidate(evidence=[{"event_id": f"E:{event_id}", "quote": "East Village"}]), cfg
    )
    row = await fetch_one("SELECT event_id FROM fact_evidence ORDER BY id DESC LIMIT 1")
    assert row["event_id"] is not None
    assert str(row["event_id"]) == str(event_id)


async def test_an_evidence_citation_written_bare_still_links(cfg):
    from agentd.db import repo_archive

    session_id = await repo_archive.create_session("test")
    event_id = await repo_archive.append_event(
        repo_archive.RawEvent(
            kind="user_message", actor="user", content="I live in the East Village",
            session_id=session_id,
        )
    )
    await review.process_candidate(
        await _candidate(evidence=[{"event_id": str(event_id), "quote": "East Village"}]), cfg
    )
    row = await fetch_one("SELECT event_id FROM fact_evidence ORDER BY id DESC LIMIT 1")
    assert str(row["event_id"]) == str(event_id)


async def test_a_citation_that_is_not_an_event_id_does_not_invent_one(cfg):
    await review.process_candidate(
        await _candidate(evidence=[{"event_id": "not-an-id", "quote": "x"}]), cfg
    )
    row = await fetch_one("SELECT event_id FROM fact_evidence ORDER BY id DESC LIMIT 1")
    assert row["event_id"] is None


async def test_the_ways_a_transcript_names_the_user_resolve_to_one_entity(cfg):
    """A predicate vocabulary buys nothing if the subject half of the key fragments: "Dylan",
    "the user" and "I" have to land on the same entity row or nothing ever groups."""
    ids = set()
    for subject in ("the user", "I", "me"):
        candidate = await _candidate(
            statement=f"Dylan prefers terse answers ({subject})",
            structured={"category": "preference", "subject": subject, "predicate": "prefers",
                        "object": "terse answers"},
        )
        subject_id, _, _ = await review._resolve_subject(candidate)
        ids.add(subject_id)
    assert len(ids) == 1, f"the user fragmented across {len(ids)} entity rows"


async def test_a_predicate_outside_its_family_is_stored_as_null_on_the_fact(cfg):
    candidate = await _candidate(
        structured={"category": "preference", "subject": "Dylan",
                    "predicate": "lives_in", "object": "terse answers"},
    )
    # the gate reads whatever the consolidator already reconciled, so simulate that step
    from agentd.memory.predicates import coerce

    stored, rejected = coerce("lives_in", "preference")
    assert (stored, rejected) == (None, "lives_in")
    candidate["structured"]["predicate"] = stored
    status, _ = await review.process_candidate(candidate, cfg)
    assert status == "accepted"
    facts = await repo_memory.active_facts()
    assert facts[0]["predicate"] is None


async def test_queue_health_reports_the_age_of_the_oldest_pending_candidate(cfg):
    await _candidate()
    health = await repo_memory.queue_health()
    assert health["pending"] == 1
    assert health["oldest_pending_at"] is not None
    assert health["oldest_user_at"] is None  # a consolidator proposal, not a correction


async def test_a_correction_waiting_for_review_is_reported_separately(cfg):
    """A backlog of inferences is housekeeping. A correction the user typed is a belief they
    think the agent already holds, and it is the case the queue must never sit on."""
    await _candidate(proposed_by="consolidator")
    await _candidate(statement="Dylan lives in the East Village", proposed_by="user")
    health = await repo_memory.queue_health()
    assert health["pending"] == 2
    assert health["oldest_user_at"] is not None


async def test_the_heartbeat_calls_a_waiting_correction_actionable(cfg):
    from agentd.daemon.heartbeat import situation_report

    await _candidate(statement="Dylan lives in the East Village", proposed_by="user")
    report, actionable = await situation_report()
    assert actionable
    assert "corrected" in report


# --- synchronous handling of direct user corrections --------------------------


async def test_propose_and_review_returns_the_gates_real_verdict_and_fact_id(cfg):
    status, reason, fact_id = await review.propose_and_review(
        statement="Dylan prefers terse answers", proposed_by="user", confidence=0.95,
        evidence=[{"source": "user"}], cfg=cfg,
    )
    assert status == "accepted"
    assert reason
    assert fact_id is not None
    assert (await repo_memory.get_fact(fact_id))["statement"] == "Dylan prefers terse answers"


async def test_a_correction_hint_does_not_launder_a_models_proposal_into_user_trust_or_bypass_the_gate(
    cfg,
):
    """A relation hint and a supersedes reference may only change what the judge compares
    against, never who proposed the claim or how much the gate trusts them. If a hint could
    do either, an untrusted source could dress up an identity-level claim as a "correction"
    and walk straight past the untrusted-identity block."""
    status, reason, fact_id = await review.propose_and_review(
        statement="Dylan is the CEO of Acme",
        proposed_by="main",
        category="biographical",
        confidence=0.9,
        evidence=[{"quote": "he said so"}],
        source_trust="untrusted",
        relation_hint="corrects",
        cfg=cfg,
    )
    assert status == "needs_review"
    assert "untrusted" in reason
    assert fact_id is None


async def test_a_supersedes_hint_puts_the_named_fact_in_front_of_the_judge(cfg):
    """Without the hint, an unrelated-looking statement would never surface the fact it is
    meant to replace; the hint's only job is to make sure the judge sees it."""
    subject = await repo_memory.upsert_entity("person", "Dylan")
    old = await repo_memory.insert_fact(
        statement="Dylan lives in Brooklyn", category="biographical", confidence=0.9,
        proposed_by="user", subject_entity_id=subject, predicate="lives_in",
        object_text="Brooklyn",
    )
    set_provider(
        FakeProvider(json_results=[JudgeVerdict(relation="updates", valid_from="2026-08-01")])
    )
    status, _reason, new_fact_id = await review.propose_and_review(
        statement="somewhere else now",
        proposed_by="user",
        category="biographical",
        confidence=0.9,
        evidence=[{"source": "user"}],
        supersedes_hint=old,
        cfg=cfg,
    )
    assert status == "superseding"
    old_row = await repo_memory.get_fact(old)
    assert old_row["status"] == "superseded"
    assert old_row["superseded_by"] == new_fact_id


async def test_a_supersedes_hint_cannot_steer_the_judge_away_from_a_real_conflict(cfg):
    """A mis-aimed hint — a stale handle, or just the wrong one — must never displace a
    same-key conflict the similarity loop already found. The hint fills a gap; it does not
    get to relocate the judge's attention away from a real contradiction."""
    subject = await repo_memory.upsert_entity("person", "Dylan")
    conflicting = await repo_memory.insert_fact(
        statement="Dylan lives in Brooklyn", category="biographical", confidence=0.9,
        proposed_by="user", subject_entity_id=subject, predicate="lives_in",
        object_text="Brooklyn",
    )
    unrelated = await repo_memory.insert_fact(
        statement="Dylan works at NYU", category="biographical", confidence=0.9,
        proposed_by="user", subject_entity_id=subject, predicate="works_at",
        object_text="NYU",
    )
    provider = FakeProvider(json_results=[JudgeVerdict(relation="corrects")])
    set_provider(provider)

    status, _reason = await review.process_candidate(
        await _candidate(
            statement="Dylan lives in the East Village",
            structured={
                "category": "biographical", "subject": "Dylan", "predicate": "lives_in",
                "object": "East Village", "supersedes_hint": str(unrelated),
            },
            confidence=0.9,
        ),
        cfg,
    )

    judge_calls = [c for c in provider.calls if c.get("json_schema") == "JudgeVerdict"]
    assert judge_calls, "the judge should have been called"
    judge_prompt = judge_calls[-1]["messages"][-1]["content"]
    assert "Dylan lives in Brooklyn" in judge_prompt
    assert "Dylan works at NYU" not in judge_prompt

    assert status == "superseding"
    assert (await repo_memory.get_fact(conflicting))["status"] == "retracted"
    assert (await repo_memory.get_fact(unrelated))["status"] == "active"


# --- cues.py: deterministic discourse cues -------------------------------------


def test_a_leading_actually_is_read_as_a_correction():
    cue = detect_correction("Actually, I live in the East Village")
    assert cue is not None
    assert cue.relation == "corrects"


def test_actually_only_fires_when_it_opens_the_statement():
    """The cue is a signal that the *sentence itself* is a correction, not that the word
    appears somewhere in it."""
    assert detect_correction("I was going to say actually yes") is None


def test_a_leading_no_comma_is_read_as_a_correction():
    cue = detect_correction("No, I live in the East Village")
    assert cue is not None
    assert cue.relation == "corrects"


def test_that_is_wrong_is_read_as_a_correction():
    cue = detect_correction("That's wrong, I live in the East Village")
    assert cue is not None
    assert cue.relation == "corrects"


def test_i_moved_is_read_as_the_world_having_changed():
    cue = detect_correction("I moved to the East Village last month")
    assert cue is not None
    assert cue.relation == "updates"


def test_i_no_longer_is_read_as_the_world_having_changed():
    cue = detect_correction("I no longer live in Brooklyn")
    assert cue is not None
    assert cue.relation == "updates"


def test_its_not_x_its_y_extracts_what_is_being_replaced():
    cue = detect_correction("it's not Brooklyn, it's the East Village")
    assert cue is not None
    assert cue.relation == "corrects"
    assert cue.replaces == "Brooklyn"
    assert cue.replacement == "the East Village"


def test_a_plain_statement_carries_no_cue():
    assert detect_correction("Dylan prefers terse answers") is None


# The vocabulary below is the one that failed live: a user typing "where i live is wrong,
# i live in the east village" got the canned "Queued for review" note, because
# `detect_correction` only knew the literal "that's wrong" contraction.


def test_saying_what_is_wrong_is_read_as_a_correction():
    cue = detect_correction(
        "where i live is wrong, i live in the east village, manhattan. "
        "I go to nyu and i graduate 05/2027"
    )
    assert cue is not None
    assert cue.relation == "corrects"


def test_a_wrong_stored_value_is_read_as_a_correction():
    for text in (
        "that address is wrong, i live in Manhattan",
        "your info is wrong, my graduation is May 2027",
        "where i live is wrong. i live in the east village",
    ):
        cue = detect_correction(text)
        assert cue is not None, text
        assert cue.relation == "corrects"


def test_calling_a_file_wrong_is_not_a_correction_of_memory():
    """This fires on every turn the user types in their own repo, where "the test is wrong"
    and "the CI config is wrong, fix it" are ordinary instructions about code. What makes a
    sentence a correction is the first-person restatement after it, not which noun is wrong —
    enumerating memory-ish nouns would never generalise."""
    assert detect_correction("the CI config is wrong, fix it") is None
    assert detect_correction("the test is wrong, fix it") is None
    assert detect_correction("the migration is wrong") is None
    assert detect_correction("what if the assumption is wrong") is None


def test_naming_an_error_without_stating_the_truth_stays_in_the_queue():
    """A bare "your info is wrong" leaves nothing for the fast path to adjudicate against,
    so queuing it is the correct outcome rather than a missed correction."""
    assert detect_correction("your info is wrong") is None


def test_reporting_someone_elses_error_is_not_a_correction_of_memory():
    """A cue means *this sentence corrects what you believe about me*. An opinion about a
    document being wrong, and a question about whether someone was wrong, are neither."""
    assert detect_correction("I think the docs are wrong about this") is None
    assert detect_correction("was I wrong?") is None
    assert detect_correction("tell me if I'm wrong") is None
    assert detect_correction("Let me know if the plan is wrong about the ordering") is None
    assert detect_correction("the test is wrong about the ordering, can you fix it") is None


def test_not_x_im_y_extracts_what_is_being_replaced():
    cue = detect_correction("I'm not in Brooklyn, I'm in the East Village")
    assert cue is not None
    assert cue.relation == "corrects"
    assert cue.replaces == "in Brooklyn"
    assert cue.replacement == "in the East Village"

    short = detect_correction("not Brooklyn, I'm in the East Village")
    assert short is not None
    assert short.replaces == "Brooklyn"


def test_hedging_is_not_a_replacement_cue():
    """"I'm not sure, I'm going to check" is the same shape as "not X, I'm Y" and corrects
    nothing; a cue that fires here would adjudicate a non-correction inline."""
    assert detect_correction("I'm not sure, I'm going to check the docs") is None
    assert detect_correction("i'm not certain, i'm leaning towards option B") is None


def test_denying_a_stored_claim_is_read_as_a_correction():
    for text in ("I don't live in Brooklyn", "I don't work at Google"):
        cue = detect_correction(text)
        assert cue is not None, text
        assert cue.relation == "corrects"


def test_ive_moved_is_read_as_the_world_having_changed():
    cue = detect_correction("I've moved to the East Village")
    assert cue is not None
    assert cue.relation == "updates"
