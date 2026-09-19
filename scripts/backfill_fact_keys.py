"""One-off: give stored facts the (subject, predicate, object) key they were never asked for.

`ExtractedFact.subject/.predicate/.object` were optional with None defaults, so the model
never filled them and every stored fact has all three NULL. That leaves `same_key_facts`
returning [], `conflicting_groups` matching nothing, and `resolve_conflicts` passing every
fact through ungrouped — contradiction detection has only ever run on embedding similarity.

Same shape as `recategorize_facts.py`, and the same reason it cannot simply UPDATE: core
columns are immutable by trigger (migrations/0003_memory.sql:61). So this retracts the keyless
row — *retract*, not supersede, because the world did not change, the record was incomplete —
and inserts a replacement.

Three columns `recategorize_facts.py` drops, which matter here:

  recorded_at            `promotable_facts` requires `recorded_at < now() - 24h`. Fresh
                         replacements would drop every promoted bullet out of the markdown
                         repo on the next nightly run and re-add it the night after.
  fact_evidence.added_at `stale_state_facts` asks whether evidence arrived in the last 60
                         days. Fresh copies would suppress staleness checks for two months,
                         which is precisely the signal the volatility model depends on.
  source_candidate_id    the link back to the candidate that proposed it, which the queue
                         health view and any later relation work both read.

    .venv/bin/python scripts/backfill_fact_keys.py [--apply]
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from pydantic import BaseModel

sys.path.insert(0, "src")

from agentd.config import get_config  # noqa: E402
from agentd.db import repo_memory  # noqa: E402
from agentd.db.pool import close_pool, connection  # noqa: E402
from agentd.embed import get_embedder  # noqa: E402
from agentd.llm.roles import get_provider, params_for  # noqa: E402
from agentd.memory import review  # noqa: E402
from agentd.memory.predicates import Predicate, coerce, vocabulary_for_prompt  # noqa: E402

SYSTEM = f"""You read one stored fact about a user and name its subject, predicate and object.

The subject is who or what the claim is about. The object is the value the predicate relates
it to. Both are short noun phrases taken from the statement itself — do not invent detail the
statement does not contain.

Pick the predicate from the list for the fact's category. If nothing fits exactly, answer
`unspecified`; that is a good answer and better than a loose match, because a wrong predicate
makes two unrelated facts look like they contradict each other.

{vocabulary_for_prompt()}
"""


class Key(BaseModel):
    subject: str
    subject_kind: str = "person"
    predicate: Predicate
    object: str


async def derive(statement: str, category: str, cfg) -> Key:
    # Not the "judge" role: that one thinks, and with a 2048-token budget it spends the whole
    # allowance reasoning and returns empty content. Naming three fields does not need it.
    return await get_provider(cfg).complete_json(
        [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": f"category: {category}\nfact: {statement}"},
        ],
        Key,
        params=params_for("rewrite", cfg),
    )


async def main(apply: bool) -> None:
    cfg = get_config()
    async with connection() as conn:
        cur = await conn.execute(
            """
            SELECT id, statement, category, confidence, importance, sensitivity,
                   subject_entity_id, predicate, object_text, object_entity_id,
                   valid_from, valid_to, recorded_at, source_candidate_id, proposed_by
            FROM facts
            WHERE status = 'active' AND (predicate IS NULL OR subject_entity_id IS NULL)
            ORDER BY recorded_at
            """
        )
        rows = await cur.fetchall()

    if not rows:
        print("nothing to do: every active fact already has a key")
        return

    embedder = get_embedder(cfg)
    changed = 0
    for row in rows:
        key = await derive(row["statement"], row["category"], cfg)
        predicate, rejected = coerce(key.predicate, row["category"])
        note = f"  (dropped {rejected}: wrong family for {row['category']})" if rejected else ""
        shown = predicate or "NULL"
        print(f"{shown:22} {key.subject} -> {key.object}{note}")
        print(f"{'':22} {row['statement'][:70]}")
        if not apply:
            continue

        subject_id, _, _ = await review._resolve_subject(
            {"structured": {"subject": key.subject, "subject_kind": key.subject_kind}}
        )
        embedding = None
        if embedder is not None:
            embedding = (await embedder.embed([row["statement"]]))[0]
        new_id = await repo_memory.insert_fact(
            statement=row["statement"],
            category=row["category"],
            confidence=float(row["confidence"]),
            proposed_by=row["proposed_by"],
            subject_entity_id=subject_id,
            predicate=predicate,
            object_text=key.object,
            object_entity_id=row["object_entity_id"],
            importance=float(row["importance"]),
            sensitivity=row["sensitivity"],
            valid_from=row["valid_from"],
            valid_to=row["valid_to"],
            # Carried, not reset: promotion ignores facts younger than a day, so a fresh
            # timestamp would un-promote everything for one nightly cycle.
            recorded_at=row["recorded_at"],
            supersedes=row["id"],
            source_candidate_id=row["source_candidate_id"],
            embedding=embedding,
            embedding_model=embedder.model_name if embedder else None,
        )
        async with connection() as conn:
            # The new row is the same claim, so it has the same support — including when that
            # support arrived, which is what the staleness sweep reads.
            await conn.execute(
                "INSERT INTO fact_evidence (fact_id, event_id, candidate_id, quote, added_at) "
                "SELECT %s, event_id, candidate_id, quote, added_at "
                "FROM fact_evidence WHERE fact_id = %s",
                (new_id, row["id"]),
            )
            await conn.execute(
                "INSERT INTO fact_entities (fact_id, entity_id, role) "
                "SELECT %s, entity_id, role FROM fact_entities WHERE fact_id = %s "
                "ON CONFLICT DO NOTHING",
                (new_id, row["id"]),
            )
        if subject_id:
            await repo_memory.link_fact_entity(new_id, subject_id, "subject")
        await repo_memory.retract_fact(row["id"], superseded_by=new_id)
        changed += 1

    print(f"\n{changed} rekeyed" if apply else f"\ndry run over {len(rows)}; pass --apply to write")
    await close_pool()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    asyncio.run(main(parser.parse_args().apply))
