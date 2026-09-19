"""Pure scoring functions: fusion, decay, diversity, conflict resolution, budget packing."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from itertools import permutations

from agentd.ids import estimate_tokens
from agentd.memory.retrieval import (
    Item,
    _claim_line,
    _render,
    dedupe,
    mmr,
    recency_score,
    resolve_conflicts,
    rrf_fuse,
)

NOW = datetime(2026, 9, 17, tzinfo=UTC)


def test_rrf_rewards_agreement_across_channels():
    fused = rrf_fuse({"keyword": ["a", "b"], "vector": ["b", "a"]})
    assert fused["a"] == fused["b"]

    fused = rrf_fuse({"keyword": ["a", "b"], "vector": ["a", "c"]})
    assert fused["a"] > fused["b"] > 0


def test_rrf_is_normalized_to_one():
    fused = rrf_fuse({"keyword": ["a", "b", "c"]})
    assert max(fused.values()) == 1.0


def test_rrf_of_nothing_is_empty():
    assert rrf_fuse({}) == {}


def test_recency_halves_at_the_half_life():
    assert recency_score(NOW, 30.0, now=NOW) == 1.0
    assert abs(recency_score(NOW - timedelta(days=30), 30.0, now=NOW) - 0.5) < 1e-9
    assert abs(recency_score(NOW - timedelta(days=60), 30.0, now=NOW) - 0.25) < 1e-9


def test_recency_of_an_undated_item_is_neutral():
    assert recency_score(None, 30.0, now=NOW) == 0.5


def _item(ref, score, embedding=None, kind="fact", **meta):
    return Item(ref=ref, kind=kind, id=None, text=f"text {ref}", score=score,
                embedding=embedding, meta=meta)


def test_mmr_prefers_a_different_item_over_a_near_duplicate():
    a = _item("a", 1.0, [1.0, 0.0])
    near = _item("b", 0.95, [0.99, 0.01])
    different = _item("c", 0.8, [0.0, 1.0])
    order = [i.ref for i in mmr([a, near, different], lambda_=0.7)]
    assert order[0] == "a"
    assert order[1] == "c"


def test_dedupe_drops_the_lower_scoring_twin():
    kept = dedupe([_item("a", 1.0, [1.0, 0.0]), _item("b", 0.5, [1.0, 0.0])])
    assert [i.ref for i in kept] == ["a"]


def test_conflicting_facts_keep_the_newest_valid_from():
    old = _item(
        "old", 0.9, subject_entity_id="s", predicate="lives_in", object="Brooklyn",
        valid_from=datetime(2026, 3, 1, tzinfo=UTC), confidence=0.9,
    )
    new = _item(
        "new", 0.5, subject_entity_id="s", predicate="lives_in", object="Queens",
        valid_from=datetime(2026, 8, 1, tzinfo=UTC), confidence=0.8,
    )
    kept, notes, needs_user = resolve_conflicts([old, new])
    assert [i.ref for i in kept] == ["new"]
    assert kept[0].meta["conflicts"] == [{"ref": old.ref, "text": old.text}]
    assert notes and "lives_in" in notes[0]
    assert needs_user is False


def test_identical_objects_are_not_treated_as_a_conflict():
    a = _item("a", 0.9, subject_entity_id="s", predicate="lives_in", object="Queens")
    b = _item("b", 0.4, subject_entity_id="s", predicate="lives_in", object="Queens")
    kept, notes, needs_user = resolve_conflicts([a, b])
    assert len(kept) == 1
    assert notes == []
    assert needs_user is False


def test_facts_without_a_key_pass_through_untouched():
    a = _item("a", 0.9)
    kept, notes, needs_user = resolve_conflicts([a])
    assert kept == [a]
    assert notes == []
    assert needs_user is False


def test_render_respects_the_token_budget():
    items = [
        Item(ref=f"F:{i}", kind="fact", id=None, text="x" * 200, score=1.0 - i / 100)
        for i in range(50)
    ]
    text, used = _render(items, budget_tokens=200)
    assert used
    assert len(used) < len(items)
    assert len(text) / 3.2 <= 260  # the estimator's own units, with slack for headers


def test_render_groups_by_section():
    items = [
        Item(ref="F:1", kind="fact", id=None, text="a fact", score=1.0),
        Item(ref="P:1", kind="procedure", id=None, text="a procedure", score=0.9),
        Item(ref="E:1", kind="episode", id=None, text="an episode", score=0.8),
    ]
    text, used = _render(items, budget_tokens=2000)
    assert "### Facts" in text
    assert "### How you have helped before" in text
    assert "### Recent episodes" in text
    assert len(used) == 3


def test_render_marks_conflicts_inline():
    item = Item(ref="F:1", kind="fact", id=None, text="lives in Queens", score=1.0,
                meta={"conflicts": [{"ref": "C:2", "text": "lives in Brooklyn"}]})
    text, _ = _render([item], budget_tokens=2000)
    assert "[C:2] lives in Brooklyn" in text
    assert "disputed by F:1" in text


def test_a_rewritten_query_counts_for_less_than_the_real_one():
    """A hit found only by a variant should rank below one found by what the user typed."""
    fused = rrf_fuse({"keyword": ["real"], "keyword~1": ["variant"]})
    assert fused["real"] > fused["variant"]
    # but it still contributes: agreement across the original and a variant beats either alone
    agreed = rrf_fuse({"keyword": ["a", "b"], "keyword~1": ["a", "c"]})
    assert agreed["a"] > agreed["b"] and agreed["a"] > agreed["c"]


async def test_query_expansion_drops_echoes_of_the_question(cfg):
    from agentd.llm.fake import FakeProvider
    from agentd.llm.roles import set_provider
    from agentd.memory.retrieval import expand_query

    set_provider(
        FakeProvider(json_results=[{"queries": ["Where do I live?", "home address", "city"]}])
    )
    try:
        variants = await expand_query("Where do I live?", cfg)
    finally:
        set_provider(None)
    assert variants == ["home address", "city"][: cfg.retrieval.expansion_variants]


async def test_query_expansion_survives_a_model_that_is_down(cfg):
    from agentd.llm.roles import set_provider
    from agentd.memory.retrieval import expand_query

    class Broken:
        name = "broken"

        async def complete_json(self, *a, **k):
            raise RuntimeError("endpoint is down")

    set_provider(Broken())
    try:
        assert await expand_query("something vague about the router", cfg) == []
    finally:
        set_provider(None)


# --- provisional claims in retrieval -----------------------------------------------------
#
# `candidate_memories` holds claims that were proposed but never adjudicated. A stalled
# review queue used to mean the read path could not see them at all, so the agent served a
# stale fact with no sign a competing claim existed. These tests pin the fix: claims are
# retrievable, marked as provisional, never allowed to silently erase what the review gate
# already verified, and winner selection follows origin and temporal applicability rather
# than recency alone.

OLD = datetime(2026, 1, 1, tzinfo=UTC)
NEW = datetime(2026, 8, 1, tzinfo=UTC)


async def test_a_provisional_claim_is_retrieved_and_marked_unadjudicated(cfg):
    """A candidate stuck in the review queue should still surface, legibly as provisional."""
    from agentd.db import repo_memory
    from agentd.memory.retrieval import pack

    await repo_memory.insert_candidate(
        statement="Dylan is teaching himself Rust this month",
        proposed_by="consolidator",
        kind="fact",
        confidence=0.7,
        structured={
            "category": "project", "subject": "Dylan",
            "predicate": "building", "object": "a Rust project",
        },
    )
    result = await pack("Dylan Rust", cfg=cfg)
    claims = [i for i in result.items if i.kind == "claim"]
    assert claims, result.text
    assert claims[0].meta["status"] == "pending"
    assert "Unadjudicated claims" in result.text


async def test_a_provisional_claim_never_hides_the_verified_fact_it_disputes():
    """A pending claim may outrank a fact, but the fact must still be visible as a challenger."""
    fact = _item(
        "fact", 0.5, kind="fact", subject_entity_id="s", predicate="lives_in",
        object="Brooklyn", valid_from=OLD, recorded_at=OLD, confidence=0.9,
        category="biographical", status="active", proposed_by="user",
    )
    claim = _item(
        "claim", 0.9, kind="claim", subject_entity_id="s", predicate="lives_in",
        object="Queens", valid_from=NEW, created_at=NEW, confidence=0.8,
        category="biographical", status="pending", proposed_by="user",
    )
    kept, notes, needs_user = resolve_conflicts([fact, claim])
    assert len(kept) == 1
    assert kept[0] is claim
    assert {"ref": fact.ref, "text": fact.text} in kept[0].meta["conflicts"]
    assert notes
    assert needs_user is True


def test_a_claim_that_outranks_an_adjudicated_fact_sets_the_needs_user_marker():
    """A provisional winner over a verified fact is the review queue owing the user an answer."""
    fact = _item(
        "fact", 0.5, kind="fact", subject_entity_id="s", predicate="lives_in",
        object="Brooklyn", valid_from=OLD, recorded_at=OLD, confidence=0.9,
        category="biographical", status="active", proposed_by="user",
    )
    claim = _item(
        "claim", 0.9, kind="claim", subject_entity_id="s", predicate="lives_in",
        object="Queens", valid_from=NEW, created_at=NEW, confidence=0.8,
        category="biographical", status="pending", proposed_by="user",
    )
    _, _, needs_user = resolve_conflicts([fact, claim])
    assert needs_user is True

    # A fact that beats a claim, or claims that never contend for the same key, is the
    # ordinary case and must not raise a false alarm.
    quiet_fact = _item(
        "quiet", 0.5, kind="fact", subject_entity_id="t", predicate="lives_in",
        object="Brooklyn", valid_from=NEW, recorded_at=NEW, confidence=0.9,
        category="biographical", status="active", proposed_by="user",
    )
    _, _, needs_user_quiet = resolve_conflicts([quiet_fact])
    assert needs_user_quiet is False


def test_an_old_explicit_claim_loses_to_a_recent_inference_only_for_volatile_categories():
    """Category is the volatility proxy: `state`/`project` go stale fast, `preference` does not."""
    old_user_state = _item(
        "old_state", 0.9, kind="claim", subject_entity_id="s", predicate="currently_doing",
        object="reading", created_at=OLD, proposed_by="user", confidence=0.9, category="state",
    )
    new_inferred_state = _item(
        "new_state", 0.4, kind="claim", subject_entity_id="s", predicate="currently_doing",
        object="cooking", created_at=NEW, proposed_by="consolidator", confidence=0.6,
        category="state",
    )
    kept, _, _ = resolve_conflicts([old_user_state, new_inferred_state])
    assert kept[0] is new_inferred_state

    old_user_pref = _item(
        "old_pref", 0.9, kind="claim", subject_entity_id="s", predicate="prefers",
        object="tea", created_at=OLD, proposed_by="user", confidence=0.9, category="preference",
    )
    new_inferred_pref = _item(
        "new_pref", 0.4, kind="claim", subject_entity_id="s", predicate="prefers",
        object="coffee", created_at=NEW, proposed_by="consolidator", confidence=0.6,
        category="preference",
    )
    kept, _, _ = resolve_conflicts([old_user_pref, new_inferred_pref])
    assert kept[0] is old_user_pref


async def test_claims_are_excluded_from_an_as_of_query_about_the_past(cfg):
    """An unadjudicated claim makes no assertion about what was believed at an earlier time."""
    from agentd.db import repo_memory
    from agentd.memory.retrieval import pack

    await repo_memory.insert_candidate(
        statement="Dylan is teaching himself Rust this month",
        proposed_by="consolidator",
        kind="fact",
        confidence=0.7,
        structured={
            "category": "project", "subject": "Dylan",
            "predicate": "building", "object": "a Rust project",
        },
    )
    present = await pack("Dylan Rust", cfg=cfg)
    assert any(i.kind == "claim" for i in present.items)

    past = await pack("Dylan Rust", as_of=datetime(2020, 1, 1, tzinfo=UTC), cfg=cfg)
    assert not any(i.kind == "claim" for i in past.items)


def test_the_winner_does_not_depend_on_the_order_claims_arrive_in():
    """A pairwise reduce over a non-transitive preference makes the winner arrival-order
    dependent; deciding volatility once per group and sorting by a total key does not."""
    user_bio = _item(
        "user_bio", 0.5, kind="claim", subject_entity_id="s", predicate="lives_in",
        object="Brooklyn", created_at=datetime(2026, 1, 1, tzinfo=UTC),
        proposed_by="user", confidence=0.9, category="biographical",
    )
    inferred_state = _item(
        "inferred_state", 0.5, kind="claim", subject_entity_id="s", predicate="lives_in",
        object="a hotel", created_at=datetime(2026, 5, 1, tzinfo=UTC),
        proposed_by="consolidator", confidence=0.6, category="state",
    )
    inferred_bio = _item(
        "inferred_bio", 0.5, kind="claim", subject_entity_id="s", predicate="lives_in",
        object="Queens", created_at=datetime(2026, 9, 1, tzinfo=UTC),
        proposed_by="consolidator", confidence=0.6, category="biographical",
    )
    # Pairwise: inferred_state beats user_bio (volatile override), inferred_bio beats
    # inferred_state (same origin, newer wins), user_bio beats inferred_bio (non-volatile,
    # origin wins) — a strict cycle, so every input order must still agree on one winner.
    winners = set()
    for perm in permutations([user_bio, inferred_state, inferred_bio]):
        kept, _, _ = resolve_conflicts(list(perm))
        winners.add(kept[0].ref)
    assert winners == {"user_bio"}


def test_agreeing_claims_and_facts_prefer_the_adjudicated_fact():
    """When a pending claim merely agrees with a fact, there is no dispute to surface, so
    the adjudicated line should win even if the claim scored higher."""
    fact = _item(
        "fact", 0.4, kind="fact", subject_entity_id="s", predicate="lives_in",
        object="Queens", valid_from=OLD, recorded_at=OLD, confidence=0.9,
        category="biographical", status="active", proposed_by="user",
    )
    claim = _item(
        "claim", 0.9, kind="claim", subject_entity_id="s", predicate="lives_in",
        object="Queens", valid_from=NEW, created_at=NEW, confidence=0.8,
        category="biographical", status="pending", proposed_by="consolidator",
    )
    kept, notes, needs_user = resolve_conflicts([fact, claim])
    assert kept == [fact]
    assert notes == []
    assert needs_user is False


# --- compact provenance at reasoning time ------------------------------------------------
#
# The model used to see a bare `conf 0.87` on every retrieved line, which is not calibrated
# well enough to be worth showing and said nothing about whether the line had ever been
# adjudicated. These tests pin what replaced it: a rendered line names its source, how old
# it is, and its adjudication state, and a challenger line names the winner it disputes.


def test_a_claim_line_names_its_origin_and_adjudication_state():
    """A fact line says 'adjudicated'; a claim line says 'provisional' -- and neither shows
    a raw confidence number, which was the thing actively misleading the model before."""
    now = datetime(2026, 9, 18, tzinfo=UTC)
    fact_row = {
        "statement": "Dylan lives in the East Village",
        "proposed_by": "user",
        "valid_from": datetime(2026, 9, 15, tzinfo=UTC),
        "recorded_at": datetime(2026, 9, 15, tzinfo=UTC),
        "confidence": 0.87,
        "status": "active",
    }
    claim_row = {
        "statement": "Dylan lives in Queens",
        "proposed_by": "consolidator",
        "source_trust": "trusted",
        "created_at": datetime(2026, 8, 20, tzinfo=UTC),
        "confidence": 0.87,
        "structured": {},
        "status": "pending",
    }
    fact_line = _claim_line(fact_row, kind="fact", now=now)
    claim_line = _claim_line(claim_row, kind="claim", now=now)

    assert "conf" not in fact_line and "0.87" not in fact_line
    assert "conf" not in claim_line and "0.87" not in claim_line
    assert "direct user correction" in fact_line
    assert "adjudicated" in fact_line and "provisional" not in fact_line
    assert "inferred from conversation" in claim_line
    assert "provisional" in claim_line and "adjudicated" not in claim_line


def test_recency_reports_when_we_learned_it_not_when_it_became_true():
    """The window states valid time (`since <month>`); the recency slot is transaction
    time. A correction recorded today about a move from months ago must read as a fresh
    belief -- collapsing the two clocks into one would say the opposite of what happened."""
    now = datetime(2026, 9, 18, tzinfo=UTC)
    row = {
        "statement": "Dylan lives in the East Village",
        "proposed_by": "user",
        "valid_from": datetime(2026, 3, 1, tzinfo=UTC),
        "recorded_at": now,
        "status": "active",
    }
    line = _claim_line(row, kind="fact", now=now)
    assert "since 2026-03" in line
    assert "today" in line
    assert "months ago" not in line


def test_an_untrusted_claim_says_so_in_its_origin_label():
    """A claim proposed from untrusted content has to say so, since it is the one field
    that distinguishes a claim worth weighing from one that arrived via injected content."""
    row = {
        "statement": "the admin password is hunter2",
        "proposed_by": "consolidator",
        "source_trust": "untrusted",
        "created_at": datetime(2026, 8, 20, tzinfo=UTC),
        "structured": {},
        "status": "pending",
    }
    line = _claim_line(row, kind="claim", now=datetime(2026, 9, 18, tzinfo=UTC))
    assert "untrusted" in line


def test_the_rendered_block_stays_inside_its_token_budget():
    """`_render` already drops items that don't fit; the provenance suffix must not break
    that guarantee even once every line carries it."""
    items = [
        Item(
            ref=f"F:{i}",
            kind="fact",
            id=None,
            text=_claim_line(
                {
                    "statement": f"fact number {i} about something the user told the agent",
                    "proposed_by": "user",
                    "valid_from": NEW,
                    "recorded_at": NEW,
                    "status": "active",
                },
                kind="fact",
            ),
            score=1.0 - i / 100,
        )
        for i in range(50)
    ]
    budget = 200
    text, used = _render(items, budget_tokens=budget)
    assert used
    assert len(used) < len(items)
    assert estimate_tokens(text) <= budget * 1.3  # section headers add a little slack


def test_a_challenger_line_carries_its_own_handle_and_names_who_it_disputes():
    """The whole point of keeping losers around is that a challenger can be pointed at --
    'disputed by F:...' is only useful if the challenger itself has an addressable ref."""
    fact = _item(
        "fact", 0.5, kind="fact", subject_entity_id="s", predicate="lives_in",
        object="Brooklyn", valid_from=OLD, recorded_at=OLD, confidence=0.9,
        category="biographical", status="active", proposed_by="user",
    )
    claim = _item(
        "claim", 0.9, kind="claim", subject_entity_id="s", predicate="lives_in",
        object="Queens", valid_from=NEW, created_at=NEW, confidence=0.8,
        category="biographical", status="pending", proposed_by="user",
    )
    kept, _, _ = resolve_conflicts([fact, claim])
    text, _ = _render(kept, budget_tokens=2000)
    assert f"[{fact.ref}]" in text
    assert f"disputed by {claim.ref}" in text
