"""The review gate: the only writer of canonical memory.

Sub-agents, tools and the consolidator all propose. This decides: reject, merge into an
existing fact, supersede one (the world changed), retract one (we were wrong), or accept.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal
from uuid import UUID

from pydantic import BaseModel

from ..config import Config, get_config
from ..db import repo_agenda, repo_memory, repo_ops
from ..db.pool import connection
from ..db.repo_ops import ActionRecord
from ..embed import cosine, get_embedder
from ..ids import parse_when, utcnow
from ..llm.roles import get_provider, params_for
from ..obs import otel
from .retrieval import to_list

MERGE_THRESHOLD = 0.93
JUDGE_FLOOR = 0.75
ACCEPT_CONFIDENCE = 0.70
USER_ACCEPT_CONFIDENCE = 0.60
MIN_CONFIDENCE = 0.50

IDENTITY_CATEGORIES = {"biographical", "preference", "relationship", "constraint"}

SECRET_PATTERNS = [
    re.compile(r"\b(?:sk|pk)-[A-Za-z0-9]{16,}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"\b(?:password|passwd|api[_ -]?key|secret|token)\b\s*[:=]\s*\S{6,}", re.I),
    re.compile(r"\bghp_[A-Za-z0-9]{20,}"),
]

JUDGE_PROMPT = """You compare a proposed memory against an existing one about the same person.

Existing: {existing}
  (recorded {recorded}, valid from {valid_from})
Proposed: {proposed}
  (evidence date {new_valid_from})

Choose exactly one relation:
- duplicate: the same claim in different words
- refines: the proposed one adds detail but does not contradict
- updates: both were true, but the world changed and the proposed one is now current
- corrects: the existing one was always wrong; the proposed one replaces it
- contradicts_uncertain: they conflict and you cannot tell which is right
- unrelated: different claims

If the relation is updates, give valid_from as the date the new state began (ISO), if known.
Explain in one sentence.
"""


class JudgeVerdict(BaseModel):
    relation: Literal[
        "duplicate", "refines", "updates", "corrects", "contradicts_uncertain", "unrelated"
    ] = "unrelated"
    valid_from: str | None = None
    rationale: str = ""


@dataclass
class ReviewStats:
    accepted: int = 0
    merged: int = 0
    superseded: int = 0
    retracted: int = 0
    rejected: int = 0
    needs_review: int = 0

    def as_dict(self) -> dict[str, int]:
        return self.__dict__.copy()


def contains_secret(text: str) -> bool:
    return any(p.search(text) for p in SECRET_PATTERNS)


async def _judge(existing: dict, candidate: dict, cfg: Config) -> JudgeVerdict:
    structured = candidate.get("structured") or {}
    messages = [
        {
            "role": "system",
            "content": "You are a careful memory judge. Answer only with the JSON schema.",
        },
        {
            "role": "user",
            "content": JUDGE_PROMPT.format(
                existing=existing["statement"],
                recorded=existing.get("recorded_at"),
                valid_from=existing.get("valid_from"),
                proposed=candidate["statement"],
                new_valid_from=structured.get("valid_from", "unknown"),
            ),
        },
    ]
    try:
        return await get_provider(cfg).complete_json(
            messages, JudgeVerdict, params=params_for("judge", cfg)
        )
    except Exception as exc:
        return JudgeVerdict(relation="contradicts_uncertain", rationale=f"judge unavailable: {exc}")


async def _resolve_subject(candidate: dict) -> tuple[UUID | None, str | None, str | None]:
    structured = candidate.get("structured") or {}
    subject = structured.get("subject")
    predicate = structured.get("predicate")
    obj = structured.get("object")
    subject_id = None
    if subject:
        rows = await repo_memory.find_entities(subject, limit=1)
        if rows:
            subject_id = rows[0]["id"]
        else:
            subject_id = await repo_memory.upsert_entity(
                structured.get("subject_kind", "person"), subject
            )
    return subject_id, predicate, obj


async def process_candidate(candidate: dict, cfg: Config | None = None) -> tuple[str, str]:
    """Decide one candidate. Returns (status, reason)."""
    cfg = cfg or get_config()
    cid = candidate["id"]
    statement = candidate["statement"].strip()
    structured = candidate.get("structured") or {}
    proposed_by = candidate["proposed_by"]
    confidence = float(candidate["confidence"])
    trust = candidate.get("source_trust", "trusted")
    kind = candidate.get("kind", "fact")

    with otel.span("review.candidate", {"candidate.kind": kind}):
        # Non-fact candidates are the agent's own bookkeeping.
        if kind in ("goal_update", "open_loop"):
            if trust == "untrusted" or confidence < ACCEPT_CONFIDENCE:
                return await _decide(cid, "needs_review", "low trust or confidence for agenda item")
            if kind == "open_loop" and not await repo_agenda.loop_exists(statement):
                await repo_agenda.add_open_loop(
                    title=statement, detail=structured.get("detail"),
                    due_at=parse_when(structured["due_at"]) if structured.get("due_at") else None,
                )
            elif kind == "goal_update":
                await repo_agenda.upsert_goal(
                    title=statement, description=structured.get("detail"),
                    next_step=structured.get("next_step"),
                )
            return await _decide(cid, "accepted", "agenda item applied")

        if kind == "procedure":
            await repo_memory.upsert_procedure(
                name=structured.get("name", statement[:60]),
                description=statement,
                when_to_use=structured.get("when_to_use", ""),
                steps_md=structured.get("steps_md", ""),
            )
            return await _decide(cid, "accepted", "procedure recorded")

        # 1. Validation.
        if not statement:
            return await _decide(cid, "rejected", "empty statement")
        if contains_secret(statement):
            return await _decide(cid, "rejected", "statement looked like credential material")
        if confidence < MIN_CONFIDENCE:
            return await _decide(cid, "rejected", f"confidence {confidence:.2f} below floor")
        evidence = candidate.get("evidence") or []
        if not evidence and proposed_by not in ("user", "md_watcher"):
            return await _decide(cid, "rejected", "no evidence cited")

        # 2. Untrusted content must never redefine who the user is.
        category = structured.get("category", "other")
        if trust == "untrusted" and category in IDENTITY_CATEGORIES:
            return await _decide(
                cid, "needs_review", "untrusted source proposing an identity-level fact"
            )

        # 3. Neighbours: duplicates, refinements, contradictions.
        embedder = get_embedder(cfg)
        embedding = None
        if embedder is not None:
            embedding = (await embedder.embed([statement]))[0]
            await repo_memory.set_candidate_embedding(cid, embedding)

        subject_id, predicate, obj = await _resolve_subject(candidate)
        neighbours: list[dict] = []
        if embedding is not None:
            neighbours = await repo_memory.nearest_facts(embedding, limit=5)
        same_key = await repo_memory.same_key_facts(subject_id, predicate)
        seen = {n["id"] for n in neighbours}
        neighbours.extend(f for f in same_key if f["id"] not in seen)

        best = None
        best_sim = 0.0
        for neighbour in neighbours:
            sim = float(neighbour.get("similarity") or 0.0)
            if sim == 0.0 and embedding is not None and neighbour.get("embedding") is not None:
                sim = cosine(embedding, to_list(neighbour["embedding"]))
            conflicting_key = (
                predicate
                and neighbour.get("predicate") == predicate
                and neighbour.get("subject_entity_id") == subject_id
                and (neighbour.get("object_text") or "") != (obj or "")
            )
            if sim >= MERGE_THRESHOLD and not conflicting_key:
                return await _merge(cid, neighbour, confidence, evidence)
            if sim >= JUDGE_FLOOR or conflicting_key:
                if sim > best_sim or conflicting_key:
                    best, best_sim = neighbour, max(sim, best_sim)

        valid_from = parse_when(structured["valid_from"]) if structured.get("valid_from") else None

        if best is not None:
            verdict = await _judge(best, candidate, cfg)
            if verdict.relation == "duplicate":
                return await _merge(cid, best, confidence, evidence)
            if verdict.relation == "contradicts_uncertain":
                return await _decide(
                    cid, "needs_review", f"conflicts with {best['id']}: {verdict.rationale}"
                )
            if verdict.relation in ("updates", "refines", "corrects"):
                when = parse_when(verdict.valid_from) if verdict.valid_from else valid_from
                new_id = await _insert(
                    candidate, statement, category, confidence, proposed_by, subject_id,
                    predicate, obj, when, embedding, embedder, supersedes=best["id"],
                )
                async with connection() as conn:
                    if verdict.relation == "corrects":
                        # We were wrong: transaction time records the mistake; valid_to stays.
                        await repo_memory.retract_fact(best["id"], superseded_by=new_id, conn=conn)
                    else:
                        # The world changed: the old fact stops being true when the new one starts.
                        await repo_memory.supersede_fact(
                            best["id"], new_id, valid_to=when or utcnow(), conn=conn
                        )
                await _attach_evidence(new_id, cid, evidence)
                status = "retracted" if verdict.relation == "corrects" else "superseding"
                await _audit(cid, new_id, status, verdict.rationale)
                return await _decide(
                    cid, "superseding", f"{verdict.relation}: {verdict.rationale}",
                    result_fact_id=new_id,
                )

        # 4. Accept on its own merits.
        floor = USER_ACCEPT_CONFIDENCE if proposed_by == "user" else ACCEPT_CONFIDENCE
        if confidence < floor:
            return await _decide(cid, "needs_review", f"confidence {confidence:.2f} below {floor}")
        new_id = await _insert(
            candidate, statement, category, confidence, proposed_by, subject_id, predicate,
            obj, valid_from, embedding, embedder,
        )
        await _attach_evidence(new_id, cid, evidence)
        await _audit(cid, new_id, "accepted", "new fact")
        return await _decide(cid, "accepted", "new fact", result_fact_id=new_id)


async def _insert(
    candidate, statement, category, confidence, proposed_by, subject_id, predicate, obj,
    valid_from, embedding, embedder, *, supersedes: UUID | None = None,
) -> UUID:
    structured = candidate.get("structured") or {}
    fact_id = await repo_memory.insert_fact(
        statement=statement,
        category=category if category in _CATEGORIES else "other",
        confidence=confidence,
        proposed_by=proposed_by,
        subject_entity_id=subject_id,
        predicate=predicate,
        object_text=obj,
        importance=float(structured.get("importance", 0.5)),
        valid_from=valid_from,
        supersedes=supersedes,
        source_candidate_id=candidate["id"],
        embedding=embedding,
        embedding_model=embedder.model_name if embedder else None,
    )
    if subject_id:
        await repo_memory.link_fact_entity(fact_id, subject_id, "subject")
    for name in structured.get("entities", []) or []:
        rows = await repo_memory.find_entities(name, limit=1)
        entity_id = rows[0]["id"] if rows else await repo_memory.upsert_entity("other", name)
        await repo_memory.link_fact_entity(fact_id, entity_id, "mention")
    return fact_id


_CATEGORIES = {
    "biographical", "preference", "relationship", "project", "state", "belief",
    "constraint", "other",
}


async def _attach_evidence(fact_id: UUID, candidate_id: UUID, evidence: list[dict]) -> None:
    for item in evidence or []:
        event_id = item.get("event_id")
        try:
            event_uuid = UUID(str(event_id)) if event_id else None
        except ValueError:
            event_uuid = None
        await repo_memory.add_evidence(
            fact_id, event_id=event_uuid, candidate_id=candidate_id, quote=item.get("quote")
        )
    if not evidence:
        await repo_memory.add_evidence(fact_id, candidate_id=candidate_id)


async def _merge(candidate_id: UUID, fact: dict, confidence: float, evidence: list[dict]) -> tuple[str, str]:
    """Same claim again: strengthen the existing fact rather than duplicating it."""
    await _attach_evidence(fact["id"], candidate_id, evidence)
    new_confidence = min(0.99, max(float(fact["confidence"]), confidence) + 0.05)
    await repo_memory.bump_confidence(fact["id"], new_confidence)
    await _audit(candidate_id, fact["id"], "merged", "duplicate of an existing fact")
    return await _decide(
        candidate_id, "merged", f"merged into {fact['id']}", result_fact_id=fact["id"]
    )


async def _decide(
    candidate_id: UUID, status: str, reason: str, *, result_fact_id: UUID | None = None
) -> tuple[str, str]:
    await repo_memory.decide_candidate(
        candidate_id, status, reason, result_fact_id=result_fact_id
    )
    return status, reason


async def _audit(candidate_id: UUID, fact_id: UUID, status: str, reason: str) -> None:
    await repo_ops.write_action(
        ActionRecord(
            actor="review", kind="memory_write", name=status, status="ok",
            rationale=reason, refs={"candidate": str(candidate_id), "fact": str(fact_id)},
            undo={"type": "retract_fact", "fact_id": str(fact_id)},
            **otel.current_ids(),
        )
    )


async def process_pending(cfg: Config | None = None, limit: int = 200) -> ReviewStats:
    cfg = cfg or get_config()
    stats = ReviewStats()
    for candidate in await repo_memory.pending_candidates(limit):
        status, _reason = await process_candidate(candidate, cfg)
        if status == "accepted":
            stats.accepted += 1
        elif status == "merged":
            stats.merged += 1
        elif status == "superseding":
            stats.superseded += 1
        elif status == "rejected":
            stats.rejected += 1
        elif status == "needs_review":
            stats.needs_review += 1
    return stats
