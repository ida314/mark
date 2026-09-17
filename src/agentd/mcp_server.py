"""MCP surface: what other harnesses (OpenClaw, editors) can ask this agent for.

Calls arrive from outside the trust boundary, so they run through the same executor
with origin='mcp' and queue anything that needs a human.
"""

from __future__ import annotations

import asyncio
import json

from mcp.server.mcpserver import MCPServer

from .config import get_config
from .db import repo_agenda, repo_memory, repo_ops

mcp = MCPServer(
    "agent-core",
    instructions="Durable memory, goals and open loops for this user's personal agent.",
)


@mcp.tool()
async def memory_search(query: str, deep: bool = False, include_history: bool = False) -> str:
    """Search the user's long-term memory and return a compact context pack."""
    from .memory.retrieval import pack

    cfg = get_config()
    result = await pack(
        query,
        budget_tokens=cfg.retrieval.deep_budget_tokens if deep else cfg.retrieval.fast_budget_tokens,
        mode="deep" if deep else "fast",
        include_history=include_history,
    )
    return result.text or "Nothing in memory matches that."


@mcp.tool()
async def memory_remember(statement: str, confidence: float = 0.6, category: str = "other") -> str:
    """Propose something for the user's long-term memory. It is reviewed before storage."""
    candidate_id = await repo_memory.insert_candidate(
        statement=statement,
        proposed_by="mcp:client",
        confidence=min(max(confidence, 0.0), 0.9),
        structured={"category": category},
        evidence=[{"source": "mcp"}],
        source_trust="untrusted",
    )
    return json.dumps({"proposed": True, "candidate_id": str(candidate_id)})


@mcp.tool()
async def profile_read(section: str = "") -> str:
    """Read the user's durable markdown profile."""
    from .memory.mdrepo import MarkdownRepo

    repo = MarkdownRepo(get_config().paths.memory_repo)
    return (repo.read_section(section) if section else repo.read_core()) or "(empty)"


@mcp.tool()
async def goals_list(status: str = "active") -> str:
    """List the user's goals."""
    rows = await repo_agenda.list_goals(status)
    return json.dumps(
        [
            {
                "slug": r["slug"], "title": r["title"], "priority": r["priority"],
                "horizon": r["horizon"], "next_step": r.get("next_step"),
            }
            for r in rows
        ],
        indent=2,
    )


@mcp.tool()
async def open_loops_list() -> str:
    """List unfinished threads."""
    rows = await repo_agenda.list_open_loops("open")
    return json.dumps([{"id": str(r["id"]), "title": r["title"]} for r in rows], indent=2)


@mcp.tool()
async def open_loop_add(title: str, detail: str = "") -> str:
    """Track an unfinished thread."""
    loop_id = await repo_agenda.add_open_loop(title=title, detail=detail or None)
    return json.dumps({"id": str(loop_id)})


@mcp.tool()
async def notify_user(title: str, body: str = "", level: str = "info") -> str:
    """Send the user a notification."""
    await repo_agenda.notify(source="mcp", title=title, body=body or None, level=level)
    return "sent"


@mcp.tool()
async def approvals_list() -> str:
    """Actions waiting for the user's approval."""
    rows = await repo_ops.list_approvals("pending")
    return json.dumps(
        [{"id": str(r["id"]), "tool": r["tool_name"], "risk": r["risk"]} for r in rows], indent=2
    )


@mcp.resource("memory://profile/core")
async def profile_resource() -> str:
    """The user's core profile."""
    from .memory.mdrepo import MarkdownRepo

    return MarkdownRepo(get_config().paths.memory_repo).read_core()


@mcp.resource("memory://goals")
async def goals_resource() -> str:
    """The user's active goals."""
    rows = await repo_agenda.list_goals("active")
    return "\n".join(f"- {r['title']} (next: {r.get('next_step') or 'unset'})" for r in rows)


@mcp.prompt()
async def agent_context(query: str) -> str:
    """A context pack about the user, for another agent to work from."""
    from .memory.retrieval import pack

    result = await pack(query, budget_tokens=get_config().retrieval.deep_budget_tokens, mode="deep")
    return f"What the user's personal agent knows about: {query}\n\n{result.text}"


def serve(*, http: bool = False, port: int = 8770) -> None:
    if http:
        mcp.run(transport="streamable-http", host="127.0.0.1", port=port)
    else:
        mcp.run(transport="stdio")


async def load_external_servers():
    """Connect to configured MCP servers and import their tools, namespaced."""
    cfg = get_config()
    imported = []
    for name, server in cfg.mcp.servers.items():
        if not server.enabled:
            continue
        try:
            async with asyncio.timeout(10):
                imported.append(await _import_server(name, server))
        except Exception as exc:  # a broken server must not stop startup
            imported.append({"name": name, "error": str(exc)})
    return imported


async def _import_server(name: str, server) -> dict:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    params = StdioServerParameters(command=server.command, args=server.args, env=server.env)
    async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        tools = await session.list_tools()
        return {"name": name, "tools": [t.name for t in tools.tools]}
