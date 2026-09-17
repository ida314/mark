"""Pure scoring functions: fusion, decay, diversity, conflict resolution, budget packing."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from agentd.memory.retrieval import (
    Item,
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


def _item(ref, score, embedding=None, **meta):
    return Item(ref=ref, kind="fact", id=None, text=f"text {ref}", score=score,
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
    kept, notes = resolve_conflicts([old, new])
    assert [i.ref for i in kept] == ["new"]
    assert kept[0].meta["conflicts"] == [old.text]
    assert notes and "lives_in" in notes[0]


def test_identical_objects_are_not_treated_as_a_conflict():
    a = _item("a", 0.9, subject_entity_id="s", predicate="lives_in", object="Queens")
    b = _item("b", 0.4, subject_entity_id="s", predicate="lives_in", object="Queens")
    kept, notes = resolve_conflicts([a, b])
    assert len(kept) == 1
    assert notes == []


def test_facts_without_a_key_pass_through_untouched():
    a = _item("a", 0.9)
    kept, notes = resolve_conflicts([a])
    assert kept == [a]
    assert notes == []


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
                meta={"conflicts": ["lives in Brooklyn"]})
    text, _ = _render([item], budget_tokens=2000)
    assert "conflicting: lives in Brooklyn" in text


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
