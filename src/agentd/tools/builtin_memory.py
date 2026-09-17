"""Memory tools. Note what these do *not* do: write canonical memory.

`memory_remember` proposes a candidate with evidence and confidence. The review gate
decides whether it becomes a fact, merges into one, or supersedes one.
"""

from __future__ import annotations

import json

from ..config import get_config
from ..db import repo_memory
from ..ids import parse_when, utcnow
from .base import Tool, ToolContext, ToolResult, obj, required, tool


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
            confidence={"type": "number", "minimum": 0, "maximum": 1},
            valid_from={"type": "string", "description": "ISO date this became true"},
            importance={"type": "number", "minimum": 0, "maximum": 1},
        ),
        "statement",
    ),
    risk="draft",
    tags=("memory", "core"),
    always_on=True,
)
async def memory_remember(args: dict, ctx: ToolContext) -> ToolResult:
    structured = {
        "category": args.get("category", "other"),
        "importance": float(args.get("importance", 0.5)),
    }
    if args.get("valid_from"):
        structured["valid_from"] = args["valid_from"]
    evidence = []
    if ctx.turn_id:
        evidence.append({"turn_id": str(ctx.turn_id)})
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
)
async def time_now(args: dict, ctx: ToolContext) -> ToolResult:
    now = utcnow().astimezone()
    return ToolResult(content=now.strftime("%A %Y-%m-%d %H:%M %Z"))


TOOLS: list[Tool] = [memory_search, memory_remember, memory_history, profile_read, time_now]
