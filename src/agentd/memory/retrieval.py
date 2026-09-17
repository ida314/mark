"""Hybrid retrieval: store everything, put almost nothing in the prompt.

Channels (keyword, vector, entity) are fused with RRF, scored with priors, optionally
reranked, stripped of contradictions and duplicates, then packed into a token budget.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID

from ..config import Config, get_config
from ..db import repo_memory
from ..db.pool import connection
from ..embed import get_embedder
from ..ids import estimate_tokens, short_id, utcnow

RRF_K = 60
CHANNEL_WEIGHTS = {"keyword": 1.0, "vector": 1.0, "entity": 0.8, "raw": 0.5}
TYPE_PRIOR = {"fact": 1.0, "procedure": 0.7, "episode": 0.6, "raw": 0.3}
HALF_LIFE_DAYS = {
    "state": 30.0, "project": 30.0,
    "preference": 365.0, "biographical": 365.0, "relationship": 365.0,
    "constraint": 365.0, "belief": 180.0, "other": 90.0,
    "_episode": 21.0, "_raw": 7.0,
}
SECTION_SHARE = {"facts": 0.50, "procedures": 0.15, "episodes": 0.35}
STOPWORDS = {
    "what", "when", "where", "which", "about", "with", "that", "this", "from", "have",
    "does", "did", "the", "and", "for", "you", "your", "are", "was", "how", "who", "why",
    "tell", "know", "remember", "please", "i", "me", "my",
}


@dataclass
class Item:
    ref: str            # short handle shown to the model, e.g. F:8c1e
    kind: str           # fact | episode | procedure | raw
    id: UUID | None
    text: str
    score: float = 0.0
    channels: list[str] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)
    embedding: list[float] | None = None


@dataclass
class ContextPack:
    text: str
    items: list[Item] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)
    stats: dict[str, Any] = field(default_factory=dict)


def to_list(vector) -> list[float] | None:
    """pgvector hands back a Vector object; everything downstream wants a plain list."""
    if vector is None:
        return None
    if hasattr(vector, "to_list"):
        return vector.to_list()
    return list(vector)


def query_terms(query: str) -> list[str]:
    words = [w for w in re.findall(r"[A-Za-z0-9_']+", query.lower()) if len(w) > 2]
    return [w for w in words if w not in STOPWORDS]


def recency_score(when: datetime | None, half_life_days: float, *, now: datetime | None = None) -> float:
    if when is None:
        return 0.5
    now = now or utcnow()
    age_days = max(0.0, (now - when).total_seconds() / 86400.0)
    return math.exp(-math.log(2) * age_days / half_life_days)


def rrf_fuse(channel_ranks: dict[str, list[str]]) -> dict[str, float]:
    """Reciprocal rank fusion across channels, normalized to the best score."""
    scores: dict[str, float] = {}
    for channel, ranked in channel_ranks.items():
        weight = CHANNEL_WEIGHTS.get(channel, 1.0)
        for rank, key in enumerate(ranked):
            scores[key] = scores.get(key, 0.0) + weight / (RRF_K + rank + 1)
    if not scores:
        return {}
    best = max(scores.values())
    return {k: v / best for k, v in scores.items()}


def mmr(items: list[Item], lambda_: float = 0.7, limit: int = 40) -> list[Item]:
    """Greedy relevance/diversity trade-off over already-scored items."""
    from ..embed import cosine

    selected: list[Item] = []
    pool = list(items)
    while pool and len(selected) < limit:
        best_item, best_value = None, -1e9
        for item in pool:
            penalty = 0.0
            if item.embedding and selected:
                sims = [
                    cosine(item.embedding, s.embedding)
                    for s in selected
                    if s.embedding is not None
                ]
                penalty = max(sims) if sims else 0.0
            value = lambda_ * item.score - (1 - lambda_) * penalty
            if value > best_value:
                best_item, best_value = item, value
        selected.append(best_item)
        pool.remove(best_item)
    return selected


def resolve_conflicts(items: list[Item]) -> tuple[list[Item], list[str]]:
    """One answer per (subject, predicate): newest valid_from wins, rest are noted."""
    groups: dict[tuple[str, str], list[Item]] = {}
    passthrough: list[Item] = []
    for item in items:
        subject = item.meta.get("subject_entity_id")
        predicate = item.meta.get("predicate")
        if item.kind == "fact" and subject and predicate:
            groups.setdefault((str(subject), predicate), []).append(item)
        else:
            passthrough.append(item)

    kept: list[Item] = []
    notes: list[str] = []
    for (_subject, predicate), group in groups.items():
        if len(group) == 1:
            kept.append(group[0])
            continue
        objects = {str(g.meta.get("object") or g.text) for g in group}
        if len(objects) == 1:
            kept.append(max(group, key=lambda g: g.score))
            continue
        ordered = sorted(
            group,
            key=lambda g: (
                g.meta.get("valid_from") or g.meta.get("recorded_at") or datetime.min,
                g.meta.get("confidence", 0.0),
                g.meta.get("recorded_at") or datetime.min,
            ),
            reverse=True,
        )
        winner, losers = ordered[0], ordered[1:]
        winner.meta["conflicts"] = [loser.text for loser in losers]
        kept.append(winner)
        notes.append(
            f"{predicate}: kept '{winner.text}' over "
            + "; ".join(f"'{loser.text}'" for loser in losers)
        )
    return kept + passthrough, notes


def dedupe(items: list[Item], threshold: float = 0.92) -> list[Item]:
    from ..embed import cosine

    kept: list[Item] = []
    for item in sorted(items, key=lambda i: i.score, reverse=True):
        if item.embedding and any(
            k.embedding and cosine(item.embedding, k.embedding) >= threshold for k in kept
        ):
            continue
        kept.append(item)
    return kept


def _fact_line(row: dict) -> str:
    bits = []
    valid_from = row.get("valid_from")
    if valid_from:
        bits.append(f"since {valid_from:%Y-%m}")
    if row.get("valid_to"):
        bits.append(f"until {row['valid_to']:%Y-%m}")
    bits.append(f"conf {row.get('confidence', 0):.2f}")
    suffix = f" ({', '.join(bits)})" if bits else ""
    prefix = "[former] " if row.get("status") == "superseded" else ""
    return f"{prefix}{row['statement']}{suffix}"


async def _channel_facts(
    query: str, terms: list[str], as_of: datetime, known_as_of: datetime, *, include_history: bool,
    max_sensitivity: str, limit: int, qvec: list[float] | None,
    entity_ids: list[UUID],
) -> tuple[dict[str, dict], dict[str, list[str]]]:
    rows: dict[str, dict] = {}
    ranks: dict[str, list[str]] = {}
    def where_clause(p: str = "") -> str:
        """Validity and sensitivity filters, pushed into SQL. `p` is a table alias prefix.

        Two clocks: `as_of` is valid time (what was true in the world then) and
        `known_as_of` is transaction time (what we had recorded by then).
        """
        validity = (
            ""
            if include_history
            else f"""
              AND ({p}valid_from IS NULL OR {p}valid_from <= %(as_of)s)
              AND ({p}valid_to IS NULL OR {p}valid_to > %(as_of)s)
            """
        )
        # A superseded fact is not deleted: it stays selectable through its valid-time
        # window, which is what makes "where did I live in May" answerable. Only
        # retractions (things that were never true) and not-yet-known rows drop out.
        return f"""
            {p}recorded_at <= %(known)s
            AND ({p}status <> 'retracted' OR {p}superseded_at > %(known)s)
            AND CASE {p}sensitivity WHEN 'normal' THEN 0 WHEN 'private' THEN 1 ELSE 2 END
                <= %(max_sens)s
            {validity}
        """

    base_where = where_clause()
    params = {
        "as_of": as_of,
        "known": known_as_of,
        "max_sens": repo_memory.SENSITIVITY_ORDER.get(max_sensitivity, 1),
        "limit": limit,
        "q": " ".join(terms) or query,
        "qvec": qvec,
        "entity_ids": entity_ids,
    }

    async with connection() as conn:
        if terms:
            cur = await conn.execute(
                f"""
                SELECT *, ts_rank_cd(tsv, websearch_to_tsquery('english', %(q)s)) AS rank
                FROM facts
                WHERE {base_where} AND tsv @@ websearch_to_tsquery('english', %(q)s)
                ORDER BY rank DESC LIMIT %(limit)s
                """,
                params,
            )
            ranks["keyword"] = []
            for row in await cur.fetchall():
                key = f"fact:{row['id']}"
                rows[key] = row
                ranks["keyword"].append(key)

        if qvec is not None:
            await conn.execute("SET LOCAL hnsw.ef_search = 80")
            cur = await conn.execute(
                f"""
                SELECT * FROM facts
                WHERE {base_where} AND embedding IS NOT NULL
                ORDER BY embedding <=> %(qvec)s::vector LIMIT %(limit)s
                """,
                params,
            )
            ranks["vector"] = []
            for row in await cur.fetchall():
                key = f"fact:{row['id']}"
                rows.setdefault(key, row)
                ranks["vector"].append(key)

        if entity_ids:
            cur = await conn.execute(
                f"""
                SELECT DISTINCT f.* FROM facts f
                LEFT JOIN fact_entities fe ON fe.fact_id = f.id
                WHERE {where_clause("f.")}
                  AND (f.subject_entity_id = ANY(%(entity_ids)s)
                       OR fe.entity_id = ANY(%(entity_ids)s))
                ORDER BY f.importance DESC, f.recorded_at DESC LIMIT %(limit)s
                """,
                params,
            )
            ranks["entity"] = []
            for row in await cur.fetchall():
                key = f"fact:{row['id']}"
                rows.setdefault(key, row)
                ranks["entity"].append(key)
    return rows, ranks


async def _channel_simple(
    table: str, query: str, terms: list[str], limit: int, qvec: list[float] | None
) -> tuple[dict[str, dict], dict[str, list[str]]]:
    rows: dict[str, dict] = {}
    ranks: dict[str, list[str]] = {}
    async with connection() as conn:
        if terms:
            cur = await conn.execute(
                f"""
                SELECT *, ts_rank_cd(tsv, websearch_to_tsquery('english', %s)) AS rank
                FROM {table}
                WHERE tsv @@ websearch_to_tsquery('english', %s)
                ORDER BY rank DESC LIMIT %s
                """,
                (" ".join(terms), " ".join(terms), limit),
            )
            ranks["keyword"] = []
            for row in await cur.fetchall():
                key = f"{table}:{row['id']}"
                rows[key] = row
                ranks["keyword"].append(key)
        if qvec is not None:
            cur = await conn.execute(
                f"""
                SELECT * FROM {table}
                WHERE embedding IS NOT NULL
                ORDER BY embedding <=> %s::vector LIMIT %s
                """,
                (qvec, limit),
            )
            ranks["vector"] = []
            for row in await cur.fetchall():
                key = f"{table}:{row['id']}"
                rows.setdefault(key, row)
                ranks["vector"].append(key)
    return rows, ranks


async def pack(
    query: str,
    *,
    budget_tokens: int = 2000,
    mode: str = "fast",
    as_of: datetime | None = None,
    known_as_of: datetime | None = None,
    include_history: bool = False,
    max_sensitivity: str = "private",
    session_id: UUID | None = None,
    turn_id: UUID | None = None,
    cfg: Config | None = None,
) -> ContextPack:
    cfg = cfg or get_config()
    as_of = as_of or utcnow()
    known_as_of = known_as_of or utcnow()
    terms = query_terms(query)
    embedder = get_embedder(cfg)
    qvec = (await embedder.embed([query]))[0] if embedder and query.strip() else None

    # entities mentioned in the query
    entity_ids: list[UUID] = []
    for term in terms[:6]:
        for ent in await repo_memory.find_entities(term, limit=2):
            if ent["id"] not in entity_ids:
                entity_ids.append(ent["id"])

    limit = cfg.retrieval.channel_limit
    fact_rows, fact_ranks = await _channel_facts(
        query, terms, as_of, known_as_of, include_history=include_history,
        max_sensitivity=max_sensitivity, limit=limit, qvec=qvec, entity_ids=entity_ids,
    )
    ep_rows, ep_ranks = await _channel_simple("episodes", query, terms, limit, qvec)
    proc_rows, proc_ranks = await _channel_simple("procedures", query, terms, limit, qvec)

    all_rows = {**fact_rows, **ep_rows, **proc_rows}
    channel_ranks: dict[str, list[str]] = {}
    for source_ranks in (fact_ranks, ep_ranks, proc_ranks):
        for channel, keys in source_ranks.items():
            channel_ranks.setdefault(channel, []).extend(keys)
    fused = rrf_fuse(channel_ranks)

    items: list[Item] = []
    for key, rrf in fused.items():
        row = all_rows.get(key)
        if row is None:
            continue
        table = key.split(":")[0]
        if table == "fact":
            kind, prior = "fact", TYPE_PRIOR["fact"]
            half_life = HALF_LIFE_DAYS.get(row.get("category", "other"), 90.0)
            when = row.get("valid_from") or row.get("recorded_at")
            text = _fact_line(row)
            confidence = float(row.get("confidence", 0.7))
            importance = float(row.get("importance", 0.5))
            meta = {
                "subject_entity_id": row.get("subject_entity_id"),
                "predicate": row.get("predicate"),
                "object": row.get("object_text"),
                "valid_from": row.get("valid_from"),
                "recorded_at": row.get("recorded_at"),
                "confidence": confidence,
                "category": row.get("category"),
            }
            ref = f"F:{short_id(row['id'])}"
        elif table == "episodes":
            kind, prior = "episode", TYPE_PRIOR["episode"]
            half_life = HALF_LIFE_DAYS["_episode"]
            when = row.get("ended_at")
            text = f"{row['title']}: {row['summary']}"
            confidence, importance = 1.0, float(row.get("importance", 0.5))
            meta = {"ended_at": when}
            ref = f"E:{short_id(row['id'])}"
        else:
            kind, prior = "procedure", TYPE_PRIOR["procedure"]
            half_life = 1e9
            when = row.get("updated_at")
            text = f"{row['name']}: {row['description']} (use when {row['when_to_use']})"
            confidence, importance = 1.0, 0.6
            meta = {}
            ref = f"P:{short_id(row['id'])}"

        recency = 1.0 if kind == "procedure" else recency_score(when, half_life, now=as_of)
        score = (
            0.60 * rrf
            + 0.15 * recency
            + 0.10 * importance
            + 0.10 * confidence
            + 0.05 * prior
        )
        channels = [c for c, keys in channel_ranks.items() if key in keys]
        items.append(
            Item(
                ref=ref, kind=kind, id=row["id"], text=text, score=score,
                channels=channels, meta=meta,
                embedding=to_list(row.get("embedding")),
            )
        )

    items.sort(key=lambda i: i.score, reverse=True)

    if mode == "deep" and embedder is not None and items:
        top = items[: cfg.retrieval.rerank_top_k]
        try:
            ce_scores = await embedder.rerank(query, [i.text for i in top])
            for item, ce in zip(top, ce_scores, strict=True):
                item.score = 0.5 * item.score + 0.5 * (1 / (1 + math.exp(-ce)))
            items.sort(key=lambda i: i.score, reverse=True)
        except Exception:
            pass

    items, conflicts = resolve_conflicts(items)
    items = dedupe(items)
    items.sort(key=lambda i: i.score, reverse=True)
    items = mmr(items, limit=40)

    text, used = _render(items, budget_tokens)
    await repo_memory.touch_accessed(
        [i.id for i in used if i.kind == "fact" and i.id is not None]
    )
    return ContextPack(
        text=text,
        items=used,
        conflicts=conflicts,
        stats={
            "candidates": len(all_rows),
            "returned": len(used),
            "entities": len(entity_ids),
            "mode": mode,
        },
    )


def _render(items: list[Item], budget_tokens: int) -> tuple[str, list[Item]]:
    """Fixed section shares; unused room flows to the next section."""
    sections = {"facts": [], "procedures": [], "episodes": []}
    for item in items:
        sections[{"fact": "facts", "procedure": "procedures", "episode": "episodes"}[item.kind]].append(item)

    lines: list[str] = []
    used: list[Item] = []
    remaining = budget_tokens
    for name, share in SECTION_SHARE.items():
        allowance = int(budget_tokens * share)
        chosen: list[str] = []
        spent = 0
        for item in sections[name]:
            line = f"- [{item.ref}] {item.text}"
            for conflict in item.meta.get("conflicts", []):
                line += f"\n    (conflicting: {conflict})"
            cost = estimate_tokens(line)
            if spent + cost > allowance or cost > remaining:
                continue
            chosen.append(line)
            used.append(item)
            spent += cost
            remaining -= cost
        if chosen:
            title = {"facts": "Facts", "procedures": "How you have helped before",
                     "episodes": "Recent episodes"}[name]
            lines.append(f"### {title}\n" + "\n".join(chosen))
        remaining += allowance - spent if allowance > spent else 0
    return "\n\n".join(lines), used
