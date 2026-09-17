"""Durability guarantees: append-only archive, immutable facts, bitemporal queries."""

from __future__ import annotations

from datetime import UTC, datetime

import psycopg
import pytest

from agentd.db import repo_archive, repo_memory
from agentd.db.pool import connection, fetch_one
from agentd.db.repo_archive import RawEvent
from agentd.ids import utcnow
from agentd.memory.retrieval import pack

MARCH = datetime(2026, 3, 1, tzinfo=UTC)
AUGUST = datetime(2026, 8, 1, tzinfo=UTC)
MAY = datetime(2026, 5, 1, tzinfo=UTC)


async def test_raw_events_cannot_be_updated_or_deleted(cfg):
    session_id = await repo_archive.create_session("test")
    event_id = await repo_archive.append_event(
        RawEvent(kind="user_message", actor="user", content="hello", session_id=session_id)
    )
    async with connection() as conn:
        with pytest.raises(psycopg.errors.RaiseException):
            await conn.execute("UPDATE raw_events SET content = 'x' WHERE event_id = %s", (event_id,))
        with pytest.raises(psycopg.errors.RaiseException):
            await conn.execute("DELETE FROM raw_events WHERE event_id = %s", (event_id,))
    row = await fetch_one("SELECT content FROM raw_events WHERE event_id = %s", (event_id,))
    assert row["content"] == "hello"


async def test_actions_and_evidence_are_append_only(cfg):
    from agentd.db.repo_ops import ActionRecord, write_action

    action_id = await write_action(
        ActionRecord(actor="main", kind="tool_call", name="fs_read", status="ok")
    )
    async with connection() as conn:
        with pytest.raises(psycopg.errors.RaiseException):
            await conn.execute("UPDATE actions SET status = 'error' WHERE id = %s", (action_id,))


async def test_fact_core_columns_are_immutable(cfg):
    fact_id = await repo_memory.insert_fact(
        statement="Dylan lives in Brooklyn", category="biographical", confidence=0.9,
        proposed_by="user",
    )
    async with connection() as conn:
        with pytest.raises(psycopg.errors.RaiseException):
            await conn.execute("UPDATE facts SET statement = 'changed' WHERE id = %s", (fact_id,))
        with pytest.raises(psycopg.errors.RaiseException):
            await conn.execute("DELETE FROM facts WHERE id = %s", (fact_id,))


async def test_a_retracted_fact_cannot_be_reactivated(cfg):
    fact_id = await repo_memory.insert_fact(
        statement="wrong thing", category="other", confidence=0.9, proposed_by="user"
    )
    await repo_memory.retract_fact(fact_id)
    async with connection() as conn:
        with pytest.raises(psycopg.errors.RaiseException):
            await conn.execute("UPDATE facts SET status = 'active' WHERE id = %s", (fact_id,))


async def test_supersession_closes_the_old_validity_window(cfg):
    old = await repo_memory.insert_fact(
        statement="Dylan lives in Brooklyn", category="biographical", confidence=0.9,
        proposed_by="user", valid_from=MARCH,
    )
    new = await repo_memory.insert_fact(
        statement="Dylan lives in Queens", category="biographical", confidence=0.9,
        proposed_by="user", valid_from=AUGUST, supersedes=old,
    )
    await repo_memory.supersede_fact(old, new, valid_to=AUGUST)

    old_row = await repo_memory.get_fact(old)
    assert old_row["status"] == "superseded"
    assert old_row["valid_to"] == AUGUST
    assert old_row["superseded_by"] == new
    assert old_row["superseded_at"] is not None


async def test_retraction_leaves_valid_to_alone(cfg):
    """A correction means it was never true, so the validity window is not closed."""
    fact_id = await repo_memory.insert_fact(
        statement="Dylan lives in Paris", category="biographical", confidence=0.9,
        proposed_by="user", valid_from=MARCH,
    )
    await repo_memory.retract_fact(fact_id)
    row = await repo_memory.get_fact(fact_id)
    assert row["status"] == "retracted"
    assert row["valid_to"] is None
    assert row["superseded_at"] is not None


async def test_as_of_returns_what_was_true_then(cfg):
    subject = await repo_memory.upsert_entity("person", "Dylan")
    old = await repo_memory.insert_fact(
        statement="Dylan lives in Brooklyn", category="biographical", confidence=0.9,
        proposed_by="user", valid_from=MARCH, subject_entity_id=subject, predicate="lives_in",
        object_text="Brooklyn",
    )
    new = await repo_memory.insert_fact(
        statement="Dylan lives in Queens", category="biographical", confidence=0.9,
        proposed_by="user", valid_from=AUGUST, subject_entity_id=subject, predicate="lives_in",
        object_text="Queens",
    )
    await repo_memory.supersede_fact(old, new, valid_to=AUGUST)

    past = await pack("where does Dylan live", as_of=MAY, cfg=cfg)
    assert "Brooklyn" in past.text
    assert "Queens" not in past.text

    present = await pack("where does Dylan live", as_of=utcnow(), cfg=cfg)
    assert "Queens" in present.text
    assert "Brooklyn" not in present.text


async def test_include_history_shows_former_beliefs(cfg):
    subject = await repo_memory.upsert_entity("person", "Dylan")
    old = await repo_memory.insert_fact(
        statement="Dylan lives in Brooklyn", category="biographical", confidence=0.9,
        proposed_by="user", valid_from=MARCH, subject_entity_id=subject, predicate="lives_in",
        object_text="Brooklyn",
    )
    new = await repo_memory.insert_fact(
        statement="Dylan lives in Queens", category="biographical", confidence=0.9,
        proposed_by="user", valid_from=AUGUST, subject_entity_id=subject, predicate="lives_in",
        object_text="Queens",
    )
    await repo_memory.supersede_fact(old, new, valid_to=AUGUST)
    result = await pack("Dylan lives", include_history=True, cfg=cfg)
    assert "[former]" in result.text


async def test_evidence_accumulates_and_cannot_be_rewritten(cfg):
    fact_id = await repo_memory.insert_fact(
        statement="Dylan prefers terse answers", category="preference", confidence=0.8,
        proposed_by="user",
    )
    await repo_memory.add_evidence(fact_id, quote="said so on Monday")
    await repo_memory.add_evidence(fact_id, quote="said so again")
    row = await fetch_one(
        "SELECT count(*) AS c FROM fact_evidence WHERE fact_id = %s", (fact_id,)
    )
    assert row["c"] == 2
    async with connection() as conn:
        with pytest.raises(psycopg.errors.RaiseException):
            await conn.execute("DELETE FROM fact_evidence WHERE fact_id = %s", (fact_id,))


async def test_a_rewritten_query_finds_what_the_users_words_missed(cfg):
    """The point of expansion: the user's words now need not match the words we stored."""
    from agentd.llm.fake import FakeProvider
    from agentd.llm.roles import set_provider
    from agentd.memory.retrieval import pack

    await repo_memory.insert_fact(
        statement="Dylan runs a DGX Spark homelab", category="project",
        confidence=0.9, proposed_by="user",
    )
    await repo_memory.insert_fact(
        statement="Dylan's favourite pasta shape is rigatoni", category="preference",
        confidence=0.9, proposed_by="user",
    )
    query = "the machine in the corner"  # matches neither statement lexically

    fast = await pack(query, mode="fast", cfg=cfg)
    assert not any("homelab" in i.text for i in fast.items)

    set_provider(FakeProvider(json_results=[{"queries": ["DGX Spark homelab"]}]))
    try:
        deep = await pack(query, mode="deep", cfg=cfg)
    finally:
        set_provider(None)

    assert deep.stats["variants"] == ["DGX Spark homelab"]
    homelab = next(i for i in deep.items if "homelab" in i.text)
    assert any(c.startswith("keyword~") for c in homelab.channels)
    # and it stays selective: the unrelated fact is not dragged in with it
    assert not any("rigatoni" in i.text for i in deep.items)
