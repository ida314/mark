"""Memory tools. Note what these do *not* do: write canonical memory.

`memory_remember` proposes a candidate with evidence and confidence. The review gate
decides whether it becomes a fact, merges into one, or supersedes one.
"""

from __future__ import annotations

import json
from typing import get_args

from ..config import get_config
from ..db import repo_memory
from ..embed import get_embedder
from ..ids import parse_when, utcnow
from ..memory.predicates import UNSPECIFIED, Predicate, coerce, vocabulary_for_prompt
from .base import Tool, ToolContext, ToolResult, obj, required, tool
from .effects import READ, UNAUDITED, UNSAFE_WRITE


@tool(
    "memory_search",
    "Search everything you remember about the user: facts, past episodes, learned procedures.",
    required(
        obj(
            query={"type": "string"},
            as_of={
                "type": "string",
                "description": "ISO date: what you believed at that time",
            },
            include_history={
                "type": "boolean",
                "description": "Include superseded facts, marked [former]",
            },
            deep={"type": "boolean", "description": "Slower, more thorough retrieval"},
        ),
        "query",
    ),
    tags=("memory", "core"),
    always_on=True,
    # Still UNAUDITED after 3c looked at it, which is a deferral and not an oversight.
    # `retrieval.pack` ends in `repo_memory.touch_accessed`: `access_count = access_count
    # + 1` on every fact it returned. That is a counter, so re-execution does not converge
    # and the tool is not literally `read` - but the state it moves is retrieval
    # bookkeeping about accesses, and a replay *is* another access. Calling it `read` is a
    # judgement about what counts as state; calling it `unsafe_write` puts four fsyncs and
    # a Pass 4 confirmation prompt on the most-called tool in the runtime. A human picks.
    # See docs/records/effect-classification.md.
    effect_class=UNAUDITED,
)
async def memory_search(args: dict, ctx: ToolContext) -> ToolResult:
    from ..memory.retrieval import pack

    cfg = get_config()
    deep = bool(args.get("deep", True))
    as_of = parse_when(args["as_of"]) if args.get("as_of") else None
    result = await pack(
        args["query"],
        budget_tokens=cfg.retrieval.deep_budget_tokens if deep else cfg.retrieval.fast_budget_tokens,
        mode="deep" if deep else "fast",
        as_of=as_of,
        include_history=bool(args.get("include_history", False)),
        session_id=ctx.session_id,
        turn_id=ctx.turn_id,
    )
    if not result.text.strip():
        return ToolResult(content="Nothing in memory matches that.")
    body = result.text
    if result.conflicts:
        body += "\n\nUnresolved conflicts: " + "; ".join(result.conflicts)
    return ToolResult(content=body, data={"count": len(result.items)})


@tool(
    "memory_remember",
    "Propose something worth remembering long term. It is reviewed before being stored.",
    required(
        obj(
            statement={
                "type": "string",
                "description": "A self-contained third-person claim, e.g. 'Dylan prefers terse answers'",
            },
            category={
                "type": "string",
                "enum": [
                    "biographical", "preference", "relationship", "project",
                    "state", "belief", "constraint", "other",
                ],
            },
            subject={
                "type": "string",
                "description": (
                    "Who or what the claim is about ('Dylan', 'agentd'). With the predicate "
                    "and object it is the key that lets a later claim be recognised as "
                    "contradicting this one, so a vague subject makes the memory unusable."
                ),
            },
            predicate={
                "type": "string",
                "enum": list(get_args(Predicate)),
                "description": (
                    "The relation, from the list for the category you chose; "
                    f"'{UNSPECIFIED}' when nothing fits, which is a good answer.\n"
                    f"{vocabulary_for_prompt()}"
                ),
            },
            object={
                "type": "string",
                "description": "The value the predicate points at ('East Village', 'Python')",
            },
            confidence={"type": "number", "minimum": 0, "maximum": 1},
            valid_from={"type": "string", "description": "ISO date this became true"},
            importance={"type": "number", "minimum": 0, "maximum": 1},
            relation={
                "type": "string",
                "enum": ["corrects", "updates", "refines"],
                "description": (
                    "How this relates to what you already believe, if you know: 'corrects' "
                    "(the old one was always wrong), 'updates' (the world changed), "
                    "'refines' (adds detail, no contradiction)."
                ),
            },
            supersedes_ref={
                "type": "string",
                "description": "The [F:xxxx] handle of the fact this one replaces or refines, if known",
            },
        ),
        "statement",
    ),
    risk="draft",
    tags=("memory", "core"),
    always_on=True,
    # unsafe_write: `insert_candidate` has no dedup key, so a replay queues the claim
    # twice; on the correction-cue path it reaches `review.propose_and_review`, which
    # writes canonical memory. Pass 7's `idempotent_write (if keyed)` needs the key.
    effect_class=UNSAFE_WRITE,
)
async def memory_remember(args: dict, ctx: ToolContext) -> ToolResult:
    structured = {
        "category": args.get("category", "other"),
        "importance": float(args.get("importance", 0.5)),
    }
    # All three are optional: a model that names none of them still gets a reviewable
    # candidate, just an ungroupable one.
    subject = (args.get("subject") or "").strip() or None
    object_text = (args.get("object") or "").strip() or None
    raw_predicate = (args.get("predicate") or "").strip() or None
    if args.get("valid_from"):
        structured["valid_from"] = args["valid_from"]
    evidence = []
    if ctx.turn_id:
        evidence.append({"turn_id": str(ctx.turn_id)})

    relation_hint = args.get("relation")
    supersedes_hint = None
    if args.get("supersedes_ref"):
        ref = args["supersedes_ref"].strip()
        if ref.upper().startswith("F:"):
            ref = ref[2:]
        fact = await repo_memory.fact_by_short_id(ref)
        if fact is not None:
            supersedes_hint = fact["id"]

    # A correction cue only ever buys synchronicity: adjudicate inline instead of leaving
    # this to sit in the queue. Untainted only — untrusted content must never take the fast
    # path, whatever the turn's discourse looked like. `proposed_by` stays `ctx.actor` on
    # both paths; promoting it to "user" here would launder the model's own inference
    # through the user's evidence exemption and lower confidence floor.
    if ctx.extra.get("correction_cue") is not None and not ctx.tainted:
        from ..memory import review

        status, reason, fact_id = await review.propose_and_review(
            statement=args["statement"],
            proposed_by=ctx.actor,
            category=structured["category"],
            confidence=float(args.get("confidence", 0.75)),
            importance=structured["importance"],
            valid_from=args.get("valid_from"),
            evidence=evidence,
            source_trust="untrusted" if ctx.tainted else "trusted",
            session_id=ctx.session_id,
            turn_id=ctx.turn_id,
            subject=subject,
            predicate=raw_predicate,
            obj=object_text,
            relation_hint=relation_hint,
            supersedes_hint=supersedes_hint,
        )
        return ToolResult(
            content=json.dumps(
                {
                    "status": status,
                    "reason": reason,
                    "fact_id": str(fact_id) if fact_id else None,
                }
            ),
            data={"status": status, "fact_id": str(fact_id) if fact_id else None},
        )

    if subject:
        structured["subject"] = subject
    if raw_predicate is not None:
        # The JSON schema can hold the predicate to the vocabulary but not to the family of
        # the category the model also picked, so reconcile here, after decoding. An
        # off-family predicate is stored as None, never as a per-category bucket: a shared
        # predicate is half a grouping key, so a bucket would make unrelated claims collide
        # as one contradiction. The original stays on the record.
        resolved, mismatched = coerce(raw_predicate, structured["category"])
        structured["predicate"] = resolved
        structured["predicate_as_extracted"] = mismatched
    if object_text:
        structured["object"] = object_text
    if relation_hint:
        structured["relation_hint"] = relation_hint
    if supersedes_hint:
        structured["supersedes_hint"] = str(supersedes_hint)
    embedder = get_embedder(get_config())
    embedding = (await embedder.embed([args["statement"]]))[0] if embedder is not None else None
    candidate_id = await repo_memory.insert_candidate(
        statement=args["statement"],
        proposed_by=ctx.actor,
        kind="fact",
        confidence=float(args.get("confidence", 0.75)),
        structured=structured,
        evidence=evidence,
        source_trust="untrusted" if ctx.tainted else "trusted",
        session_id=ctx.session_id,
        turn_id=ctx.turn_id,
        embedding=embedding,
    )
    return ToolResult(
        content=json.dumps(
            {
                "proposed": True,
                "candidate_id": str(candidate_id),
                "note": "Queued for review; it becomes a durable memory if it passes.",
            }
        ),
        data={"candidate_id": str(candidate_id)},
    )


@tool(
    "memory_history",
    "Show how what you believe about a subject changed over time, including former beliefs.",
    required(obj(subject={"type": "string"}), "subject"),
    tags=("memory",),
    effect_class=READ,
)
async def memory_history(args: dict, ctx: ToolContext) -> ToolResult:
    rows = await repo_memory.facts_for_subject(args["subject"])
    if not rows:
        return ToolResult(content=f"Nothing recorded about '{args['subject']}'.")
    lines = []
    for row in rows[:60]:
        window = []
        if row.get("valid_from"):
            window.append(f"from {row['valid_from']:%Y-%m-%d}")
        if row.get("valid_to"):
            window.append(f"until {row['valid_to']:%Y-%m-%d}")
        status = row["status"]
        lines.append(
            f"- {row['statement']} [{status}{', ' + ' '.join(window) if window else ''}, "
            f"recorded {row['recorded_at']:%Y-%m-%d}]"
        )
    return ToolResult(content="\n".join(lines))


@tool(
    "profile_read",
    "Read the durable profile the agent keeps in markdown (core, preferences, goals, projects).",
    obj(section={"type": "string", "description": "e.g. profile/core, goals, projects"}),
    tags=("memory", "core"),
    always_on=True,
    effect_class=READ,
)
async def profile_read(args: dict, ctx: ToolContext) -> ToolResult:
    from ..memory.mdrepo import MarkdownRepo

    repo = MarkdownRepo(get_config().paths.memory_repo)
    section = args.get("section")
    text = repo.read_section(section) if section else repo.read_core()
    return ToolResult(content=text or "(nothing recorded yet)")


@tool(
    "time_now",
    "The current date and time in the user's timezone.",
    obj(),
    tags=("core",),
    always_on=True,
    effect_class=READ,
)
async def time_now(args: dict, ctx: ToolContext) -> ToolResult:
    now = utcnow().astimezone()
    return ToolResult(content=now.strftime("%A %Y-%m-%d %H:%M %Z"))


TOOLS: list[Tool] = [memory_search, memory_remember, memory_history, profile_read, time_now]
