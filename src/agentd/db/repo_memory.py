"""Canonical memory access. Only the review gate should call the write functions here."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any
from uuid import UUID

from ..ids import utcnow, uuid7
from .pool import connection, fetch_all, fetch_one

SENSITIVITY_ORDER = {"normal": 0, "private": 1, "secret": 2}


# --- entities ----------------------------------------------------------------

async def upsert_entity(
    kind: str, canonical_name: str, *, aliases: list[str] | None = None,
    summary: str | None = None, embedding: list[float] | None = None,
    embedding_model: str | None = None,
) -> UUID:
    async with connection() as conn:
        cur = await conn.execute(
            """
            INSERT INTO entities (id, kind, canonical_name, aliases, summary, embedding,
                                  embedding_model)
            VALUES (%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (kind, lower(canonical_name)) DO UPDATE SET
              -- array_agg over an empty set returns NULL, so keep a floor of '{}'
              aliases = COALESCE(
                (SELECT array_agg(DISTINCT a)
                 FROM unnest(entities.aliases || EXCLUDED.aliases) a),
                '{}'),
              summary = COALESCE(EXCLUDED.summary, entities.summary),
              embedding = COALESCE(EXCLUDED.embedding, entities.embedding),
              embedding_model = COALESCE(EXCLUDED.embedding_model, entities.embedding_model),
              updated_at = now()
            RETURNING id
            """,
            (uuid7(), kind, canonical_name, aliases or [], summary, embedding, embedding_model),
        )
        row = await cur.fetchone()
        return row["id"]


async def find_entities(term: str, limit: int = 5) -> list[dict]:
    """Trigram similarity on the canonical name, or an exact alias hit."""
    return await fetch_all(
        """
        SELECT *, similarity(canonical_name, %s) AS sim
        FROM entities
        WHERE similarity(canonical_name, %s) > 0.45
           OR lower(%s) = ANY (SELECT lower(a) FROM unnest(aliases) a)
        ORDER BY sim DESC NULLS LAST
        LIMIT %s
        """,
        (term, term, term, limit),
    )


async def get_entity(entity_id: UUID) -> dict | None:
    return await fetch_one("SELECT * FROM entities WHERE id = %s", (entity_id,))


async def entity_by_name(name: str) -> dict | None:
    return await fetch_one(
        "SELECT * FROM entities WHERE lower(canonical_name) = lower(%s) LIMIT 1", (name,)
    )


# --- facts -------------------------------------------------------------------

async def insert_fact(
    *,
    statement: str,
    category: str,
    confidence: float,
    proposed_by: str,
    subject_entity_id: UUID | None = None,
    predicate: str | None = None,
    object_text: str | None = None,
    object_entity_id: UUID | None = None,
    importance: float = 0.5,
    sensitivity: str = "normal",
    valid_from: datetime | None = None,
    valid_to: datetime | None = None,
    recorded_at: datetime | None = None,
    supersedes: UUID | None = None,
    source_candidate_id: UUID | None = None,
    embedding: list[float] | None = None,
    embedding_model: str | None = None,
    conn=None,
) -> UUID:
    fact_id = uuid7()
    sql = """
        INSERT INTO facts (id, statement, subject_entity_id, predicate, object_text,
                           object_entity_id, category, confidence, importance, sensitivity,
                           valid_from, valid_to, recorded_at, supersedes, source_candidate_id,
                           proposed_by, embedding, embedding_model)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,COALESCE(%s, now()),%s,%s,%s,%s,%s)
    """
    params = (
        fact_id, statement, subject_entity_id, predicate, object_text, object_entity_id,
        category, confidence, importance, sensitivity, valid_from, valid_to, recorded_at,
        supersedes, source_candidate_id, proposed_by, embedding, embedding_model,
    )
    if conn is not None:
        await conn.execute(sql, params)
    else:
        async with connection() as c:
            await c.execute(sql, params)
    return fact_id


async def supersede_fact(
    old_id: UUID, new_id: UUID, *, valid_to: datetime | None = None, conn=None
) -> None:
    """The world changed: the old fact stops being true, but stays in the record."""
    sql = """
        UPDATE facts
        SET status = 'superseded', superseded_at = now(), superseded_by = %s,
            valid_to = COALESCE(valid_to, %s)
        WHERE id = %s
    """
    params = (new_id, valid_to or utcnow(), old_id)
    if conn is not None:
        await conn.execute(sql, params)
    else:
        async with connection() as c:
            await c.execute(sql, params)


async def retract_fact(
    fact_id: UUID, *, superseded_by: UUID | None = None, conn=None
) -> None:
    """We were wrong: valid_to is untouched because it was never true."""
    sql = """
        UPDATE facts
        SET status = 'retracted', superseded_at = now(),
            superseded_by = COALESCE(%s, superseded_by)
        WHERE id = %s
    """
    if conn is not None:
        await conn.execute(sql, (superseded_by, fact_id))
    else:
        async with connection() as c:
            await c.execute(sql, (superseded_by, fact_id))


async def link_fact_entity(fact_id: UUID, entity_id: UUID, role: str = "mention", conn=None) -> None:
    sql = (
        "INSERT INTO fact_entities (fact_id, entity_id, role) VALUES (%s,%s,%s) "
        "ON CONFLICT DO NOTHING"
    )
    if conn is not None:
        await conn.execute(sql, (fact_id, entity_id, role))
    else:
        async with connection() as c:
            await c.execute(sql, (fact_id, entity_id, role))


async def add_evidence(
    fact_id: UUID, *, event_id: UUID | None = None, candidate_id: UUID | None = None,
    quote: str | None = None, conn=None,
) -> None:
    sql = (
        "INSERT INTO fact_evidence (fact_id, event_id, candidate_id, quote) VALUES (%s,%s,%s,%s)"
    )
    if conn is not None:
        await conn.execute(sql, (fact_id, event_id, candidate_id, quote))
    else:
        async with connection() as c:
            await c.execute(sql, (fact_id, event_id, candidate_id, quote))


async def bump_confidence(fact_id: UUID, confidence: float, conn=None) -> None:
    sql = "UPDATE facts SET confidence = %s WHERE id = %s"
    if conn is not None:
        await conn.execute(sql, (confidence, fact_id))
    else:
        async with connection() as c:
            await c.execute(sql, (confidence, fact_id))


async def get_fact(fact_id: UUID) -> dict | None:
    return await fetch_one("SELECT * FROM facts WHERE id = %s", (fact_id,))


async def fact_by_short_id(short: str) -> dict | None:
    return await fetch_one(
        "SELECT * FROM facts WHERE replace(id::text, '-', '') LIKE %s LIMIT 1", (f"{short}%",)
    )


async def active_facts(
    *, category: str | None = None, limit: int = 100, as_of: datetime | None = None
) -> list[dict]:
    as_of = as_of or utcnow()
    return await fetch_all(
        f"""
        SELECT * FROM facts
        WHERE status = 'active'
          AND recorded_at <= %s AND (superseded_at IS NULL OR superseded_at > %s)
          AND (valid_from IS NULL OR valid_from <= %s)
          AND (valid_to IS NULL OR valid_to > %s)
          {"AND category = %s" if category else ""}
        ORDER BY importance DESC, recorded_at DESC
        LIMIT %s
        """,
        (as_of, as_of, as_of, as_of, *( (category,) if category else () ), limit),
    )


async def facts_for_subject(subject: str, *, include_history: bool = True) -> list[dict]:
    """Everything ever believed about a subject, in bitemporal order."""
    return await fetch_all(
        """
        SELECT f.*, e.canonical_name AS subject_name
        FROM facts f
        LEFT JOIN entities e ON e.id = f.subject_entity_id
        WHERE (e.canonical_name ILIKE %s OR f.statement ILIKE %s)
        ORDER BY COALESCE(f.valid_from, f.recorded_at), f.recorded_at
        """,
        (f"%{subject}%", f"%{subject}%"),
    )


async def same_key_facts(subject_entity_id: UUID | None, predicate: str | None) -> list[dict]:
    if not subject_entity_id or not predicate:
        return []
    return await fetch_all(
        """
        SELECT * FROM facts
        WHERE status = 'active' AND subject_entity_id = %s AND predicate = %s
        ORDER BY COALESCE(valid_from, recorded_at) DESC
        """,
        (subject_entity_id, predicate),
    )


async def nearest_facts(embedding: list[float], limit: int = 5) -> list[dict]:
    if embedding is None:
        return []
    return await fetch_all(
        """
        SELECT *, 1 - (embedding <=> %s::vector) AS similarity
        FROM facts
        WHERE status = 'active' AND embedding IS NOT NULL
        ORDER BY embedding <=> %s::vector
        LIMIT %s
        """,
        (embedding, embedding, limit),
    )


async def conflicting_groups() -> list[dict]:
    """Active facts sharing a (subject, predicate) key with differing objects."""
    return await fetch_all(
        """
        SELECT subject_entity_id, predicate, array_agg(id) AS fact_ids,
               count(DISTINCT COALESCE(object_text, object_entity_id::text)) AS variants
        FROM facts
        WHERE status = 'active' AND subject_entity_id IS NOT NULL AND predicate IS NOT NULL
        GROUP BY subject_entity_id, predicate
        HAVING count(DISTINCT COALESCE(object_text, object_entity_id::text)) > 1
        """
    )


async def mark_promoted(fact_ids: list[UUID]) -> None:
    if not fact_ids:
        return
    async with connection() as conn:
        await conn.execute(
            "UPDATE facts SET promoted_to_md = true WHERE id = ANY(%s)", (fact_ids,)
        )


async def touch_accessed(fact_ids: list[UUID]) -> None:
    if not fact_ids:
        return
    async with connection() as conn:
        await conn.execute(
            """
            UPDATE facts SET last_accessed_at = now(), access_count = access_count + 1
            WHERE id = ANY(%s)
            """,
            (fact_ids,),
        )


async def promotable_facts(min_confidence: float = 0.85) -> list[dict]:
    return await fetch_all(
        """
        SELECT f.*, e.canonical_name AS subject_name,
               (SELECT count(*) FROM fact_evidence ev WHERE ev.fact_id = f.id) AS evidence_count
        FROM facts f
        LEFT JOIN entities e ON e.id = f.subject_entity_id
        WHERE f.status = 'active'
          AND f.category IN ('biographical','preference','relationship','constraint','project')
          AND f.confidence >= %s
          AND f.sensitivity = 'normal'
          AND f.recorded_at < now() - interval '24 hours'
        ORDER BY f.category, f.importance DESC
        """,
        (min_confidence,),
    )


async def stale_state_facts(days: int = 60, limit: int = 5) -> list[dict]:
    return await fetch_all(
        """
        SELECT * FROM facts
        WHERE status = 'active' AND category IN ('state','project')
          AND recorded_at < now() - make_interval(days => %s)
          AND NOT EXISTS (
            SELECT 1 FROM fact_evidence ev
            WHERE ev.fact_id = facts.id AND ev.added_at > now() - make_interval(days => %s))
        ORDER BY recorded_at
        LIMIT %s
        """,
        (days, days, limit),
    )


# --- candidates --------------------------------------------------------------

async def insert_candidate(
    *,
    statement: str,
    proposed_by: str,
    kind: str = "fact",
    confidence: float = 0.6,
    structured: dict[str, Any] | None = None,
    evidence: list[dict] | None = None,
    source_trust: str = "trusted",
    session_id: UUID | None = None,
    turn_id: UUID | None = None,
    embedding: list[float] | None = None,
) -> UUID:
    cid = uuid7()
    async with connection() as conn:
        await conn.execute(
            """
            INSERT INTO candidate_memories
              (id, proposed_by, session_id, turn_id, kind, statement, structured, confidence,
               evidence, source_trust, embedding)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """,
            (
                cid, proposed_by, session_id, turn_id, kind, statement,
                json.dumps(structured or {}, default=str), confidence,
                json.dumps(evidence or [], default=str), source_trust, embedding,
            ),
        )
    return cid


async def insert_candidate_once(
    *,
    promotion_key: str,
    statement: str,
    proposed_by: str,
    kind: str = "fact",
    confidence: float = 0.6,
    structured: dict[str, Any] | None = None,
    evidence: list[dict] | None = None,
    source_trust: str = "trusted",
    session_id: UUID | None = None,
    turn_id: UUID | None = None,
    embedding: list[float] | None = None,
) -> tuple[UUID, bool]:
    """Propose a candidate, unless this promotion already proposed one.

    Returns `(candidate_id, inserted)`. `inserted=False` means the row was already there
    under this promotion key, which is the answer a resume needs: the write it could not
    prove had happened did happen, so there is nothing to do and nothing to duplicate.

    The same shape as `repo_archive.append_event_once` and for the same reason - one
    statement rather than a SELECT and then an INSERT, so that two attempts racing (a resume
    overlapping the process it is resuming) cannot both find it missing. `DO NOTHING` rather
    than `DO UPDATE`: a promotion that already landed is not re-stated, because the review
    gate may already have decided about it and walking a decided row back to `pending` would
    make the gate's answer a thing a crash can undo.

    `promotion_key` is written into `structured`, where `candidate_promotion_key_idx`
    (migration 0010) constrains it. It is put there by this function rather than trusted
    from the caller's `structured` dict, so the constrained value and the argument cannot
    be two different strings.
    """
    cid = uuid7()
    body = dict(structured or {})
    body["promotion_key"] = promotion_key
    async with connection() as conn:
        cur = await conn.execute(
            """
            INSERT INTO candidate_memories
              (id, proposed_by, session_id, turn_id, kind, statement, structured, confidence,
               evidence, source_trust, embedding)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT ((structured->>'promotion_key'))
              WHERE (structured->>'promotion_key') IS NOT NULL
            DO NOTHING
            RETURNING id
            """,
            (
                cid, proposed_by, session_id, turn_id, kind, statement,
                json.dumps(body, default=str), confidence,
                json.dumps(evidence or [], default=str), source_trust, embedding,
            ),
        )
        row = await cur.fetchone()
        if row is not None:
            return cid, True
        found = await conn.execute(
            "SELECT id FROM candidate_memories WHERE structured->>'promotion_key' = %s",
            (promotion_key,),
        )
        existing = await found.fetchone()
    if existing is None:  # pragma: no cover - only reachable if the index is missing
        raise RuntimeError(
            f"candidate for promotion {promotion_key[:12]} was neither inserted nor found; "
            "migration 0010 (candidate_promotion_key_idx) is probably not applied"
        )
    return existing["id"], False


async def pending_candidates(limit: int = 200) -> list[dict]:
    return await fetch_all(
        "SELECT * FROM candidate_memories WHERE status = 'pending' ORDER BY created_at LIMIT %s",
        (limit,),
    )


async def candidate_by_id(candidate_id: UUID) -> dict | None:
    return await fetch_one("SELECT * FROM candidate_memories WHERE id = %s", (candidate_id,))


async def queue_health() -> dict:
    """How deep the review queue is and how long the oldest thing has waited.

    The 2026-09-18 incident was invisible because nothing reported this: five candidates sat
    `pending` indefinitely while postgres, ntfy and the model endpoint were all healthy. A
    user-origin candidate is broken out separately because a correction someone typed has a
    very different expected latency from an inference the consolidator volunteered.
    """
    return await fetch_one(
        """
        SELECT
          count(*) FILTER (WHERE status = 'pending')                          AS pending,
          count(*) FILTER (WHERE status = 'needs_review')                     AS needs_review,
          min(created_at) FILTER (WHERE status = 'pending')                   AS oldest_pending_at,
          min(created_at) FILTER (WHERE status = 'pending'
                                    AND proposed_by = 'user')                 AS oldest_user_at,
          count(*) FILTER (WHERE status = 'rejected'
                             AND decided_at > now() - interval '7 days')      AS rejected_7d
        FROM candidate_memories
        """
    )


async def candidates_by_status(status: str, limit: int = 100) -> list[dict]:
    return await fetch_all(
        "SELECT * FROM candidate_memories WHERE status = %s ORDER BY created_at LIMIT %s",
        (status, limit),
    )


async def decide_candidate(
    candidate_id: UUID, status: str, reason: str, *, decided_by: str = "review",
    result_fact_id: UUID | None = None, conn=None,
) -> None:
    sql = """
        UPDATE candidate_memories
        SET status = %s, decision_reason = %s, decided_by = %s, decided_at = now(),
            result_fact_id = COALESCE(%s, result_fact_id)
        WHERE id = %s
    """
    params = (status, reason, decided_by, result_fact_id, candidate_id)
    if conn is not None:
        await conn.execute(sql, params)
    else:
        async with connection() as c:
            await c.execute(sql, params)


async def set_candidate_embedding(candidate_id: UUID, embedding: list[float]) -> None:
    async with connection() as conn:
        await conn.execute(
            "UPDATE candidate_memories SET embedding = %s WHERE id = %s", (embedding, candidate_id)
        )


# --- episodes and procedures -------------------------------------------------

async def insert_episode(
    *,
    session_id: UUID | None,
    started_at: datetime,
    ended_at: datetime,
    first_event_id: int,
    last_event_id: int,
    title: str,
    summary: str,
    outcome: str | None = None,
    entity_ids: list[UUID] | None = None,
    importance: float = 0.5,
    embedding: list[float] | None = None,
    embedding_model: str | None = None,
) -> UUID:
    eid = uuid7()
    async with connection() as conn:
        await conn.execute(
            """
            INSERT INTO episodes (id, session_id, started_at, ended_at, first_event_id,
                                  last_event_id, title, summary, outcome, entity_ids,
                                  importance, embedding, embedding_model)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """,
            (
                eid, session_id, started_at, ended_at, first_event_id, last_event_id, title,
                summary, outcome, entity_ids or [], importance, embedding, embedding_model,
            ),
        )
    return eid


async def recent_episodes(limit: int = 10) -> list[dict]:
    return await fetch_all("SELECT * FROM episodes ORDER BY ended_at DESC LIMIT %s", (limit,))


async def upsert_procedure(
    *, name: str, description: str, when_to_use: str, steps_md: str,
    embedding: list[float] | None = None, embedding_model: str | None = None,
) -> UUID:
    async with connection() as conn:
        cur = await conn.execute(
            """
            INSERT INTO procedures (id, name, description, when_to_use, steps_md,
                                    embedding, embedding_model)
            VALUES (%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (name) DO UPDATE SET
              description = EXCLUDED.description, when_to_use = EXCLUDED.when_to_use,
              steps_md = EXCLUDED.steps_md, version = procedures.version + 1,
              embedding = COALESCE(EXCLUDED.embedding, procedures.embedding),
              updated_at = now()
            RETURNING id
            """,
            (uuid7(), name, description, when_to_use, steps_md, embedding, embedding_model),
        )
        row = await cur.fetchone()
        return row["id"]


async def active_procedures() -> list[dict]:
    return await fetch_all("SELECT * FROM procedures WHERE status = 'active' ORDER BY name")


__all__ = [name for name in dir() if not name.startswith("_")]
