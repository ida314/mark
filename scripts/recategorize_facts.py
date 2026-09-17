"""One-off: re-judge the category of facts that were stored before the taxonomy was enforced.

Every fact extracted before `ExtractedFact.category` became a Literal came back as "other",
because the field was a defaulted free string the model never had to think about. That quietly
disabled two things: the per-category recency half-lives, and the review-gate rule that
untrusted content may not touch identity-level facts.

`category` is immutable by trigger (migrations/0003_memory.sql:61), and rightly so, so this
does not edit anything. It retracts the miscategorised row — *retract*, not supersede, because
the world did not change, we were simply wrong about the record — and inserts a replacement
carrying the same statement, validity and evidence.

    .venv/bin/python scripts/recategorize_facts.py [--apply]
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from typing import Literal

from pydantic import BaseModel

sys.path.insert(0, "src")

from agentd.config import get_config  # noqa: E402
from agentd.db import repo_memory  # noqa: E402
from agentd.db.pool import close_pool, connection  # noqa: E402
from agentd.embed import get_embedder  # noqa: E402
from agentd.llm.roles import get_provider, params_for  # noqa: E402

Category = Literal[
    "biographical", "preference", "relationship", "project",
    "state", "belief", "constraint", "other",
]

SYSTEM = """You assign one category to a stored fact about a user.

biographical  who they are, where they live, unchanging or slow-changing identity
preference    how they like things done
relationship  a person and who they are to the user
project       something they are building or working on
state         true for now and expected to change
belief        an opinion they hold
constraint    a rule or limit they operate under
other         none of the above genuinely fit

Answer with the category alone. Prefer "other" over a category that only loosely fits."""


class Verdict(BaseModel):
    category: Category
    reason: str = ""


async def judge(statement: str, cfg) -> Verdict:
    # Deliberately not the "judge" role: that one thinks, and with a 2048-token budget the
    # model spends the whole allowance reasoning and returns empty content. Picking one word
    # from a list of eight does not need deliberation.
    return await get_provider(cfg).complete_json(
        [{"role": "system", "content": SYSTEM}, {"role": "user", "content": statement}],
        Verdict,
        params=params_for("rewrite", cfg),
    )


async def main(apply: bool) -> None:
    cfg = get_config()
    async with connection() as conn:
        cur = await conn.execute(
            """
            SELECT id, statement, category, confidence, importance, sensitivity,
                   subject_entity_id, predicate, object_text, valid_from, valid_to, proposed_by
            FROM facts WHERE status = 'active' AND category = 'other' ORDER BY recorded_at
            """
        )
        rows = await cur.fetchall()

    if not rows:
        print("nothing to do: no active facts are categorised 'other'")
        return

    embedder = get_embedder(cfg)
    changed = 0
    for row in rows:
        verdict = await judge(row["statement"], cfg)
        mark = " (unchanged)" if verdict.category == "other" else ""
        print(f"{verdict.category:14} {row['statement'][:64]}{mark}")
        if verdict.category == "other" or not apply:
            continue

        embedding = None
        if embedder is not None:
            embedding = (await embedder.embed([row["statement"]]))[0]
        new_id = await repo_memory.insert_fact(
            statement=row["statement"],
            category=verdict.category,
            confidence=float(row["confidence"]),
            proposed_by="recategorize",
            subject_entity_id=row["subject_entity_id"],
            predicate=row["predicate"],
            object_text=row["object_text"],
            importance=float(row["importance"]),
            sensitivity=row["sensitivity"],
            valid_from=row["valid_from"],
            valid_to=row["valid_to"],
            embedding=embedding,
            embedding_model=embedder.model_name if embedder else None,
        )
        async with connection() as conn:
            # Carry the evidence over: the new row is the same claim, so it has the same support.
            await conn.execute(
                "INSERT INTO fact_evidence (fact_id, event_id, candidate_id, quote) "
                "SELECT %s, event_id, candidate_id, quote FROM fact_evidence WHERE fact_id = %s",
                (new_id, row["id"]),
            )
            await conn.execute(
                "INSERT INTO fact_entities (fact_id, entity_id, role) "
                "SELECT %s, entity_id, role FROM fact_entities WHERE fact_id = %s "
                "ON CONFLICT DO NOTHING",
                (new_id, row["id"]),
            )
        await repo_memory.retract_fact(row["id"], superseded_by=new_id)
        changed += 1

    print(f"\n{changed} recategorised" if apply else "\ndry run; pass --apply to write")
    await close_pool()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    asyncio.run(main(parser.parse_args().apply))
