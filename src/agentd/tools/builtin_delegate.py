"""Delegation: hand a task to an ephemeral sub-agent and get back a summary."""

from __future__ import annotations

import json

from .base import Tool, ToolContext, ToolResult, obj, required, tool


@tool(
    "delegate",
    (
        "Hand a self-contained task to a sub-agent: 'researcher' for web or document research, "
        "'coder' for multi-file code work, 'memory' for a deep search of what you know. "
        "You get a summary back, not their whole transcript."
    ),
    required(
        obj(
            agent={"type": "string", "enum": ["researcher", "coder", "memory"]},
            task={
                "type": "string",
                "description": "A complete brief: they cannot see this conversation.",
            },
            context={"type": "string", "description": "Extra background they need"},
        ),
        "agent",
        "task",
    ),
    tags=("core",),
    always_on=True,
)
async def delegate(args: dict, ctx: ToolContext) -> ToolResult:
    agent_name = args["agent"]
    task = args["task"]
    if args.get("context"):
        task = f"{task}\n\nBackground from the user's agent:\n{args['context']}"

    if agent_name == "memory":
        from ..config import get_config
        from ..memory.retrieval import pack

        result = await pack(
            task, budget_tokens=get_config().retrieval.deep_budget_tokens, mode="deep",
            session_id=ctx.session_id, turn_id=ctx.turn_id,
        )
        body = result.text or "Nothing relevant in memory."
        if result.conflicts:
            body += "\n\nConflicts: " + "; ".join(result.conflicts)
        return ToolResult(content=body, data={"items": len(result.items)})

    from ..agent.subagents import SPECS, run_subagent

    spec = SPECS.get(agent_name)
    if spec is None:
        return ToolResult(content=f"No such sub-agent: {agent_name}", ok=False)

    approver = ctx.extra.get("approver")
    if approver is None:
        from ..policy.approvals import QueueApprover

        approver = QueueApprover(origin=ctx.origin)

    result = await run_subagent(
        spec,
        task,
        parent_session_id=ctx.session_id,
        parent_turn_id=ctx.turn_id,
        parent_autonomy=ctx.autonomy,
        approver=approver,
        parent_action_id=ctx.action_id,
    )
    payload = {
        "status": result.status,
        "summary": result.summary,
        "artifacts": result.artifacts,
        "citations": result.citations,
    }
    return ToolResult(
        content=json.dumps(payload, indent=2),
        ok=result.status in ("ok", "partial"),
        trust="untrusted" if result.tainted else "trusted",
        data={"status": result.status},
    )


TOOLS: list[Tool] = [delegate]
