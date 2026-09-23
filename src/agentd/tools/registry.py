"""Tool registry and per-turn tool selection.

The registry may hold far more tools than any single turn should see. Selection keeps
the exposed set small: core tools, tools already used this session, and the closest
matches to what the user just asked for.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..config import Config, get_config
from ..db import repo_ops
from .base import Tool, ToolContext, ToolResult, obj, required, tool
from .effects import READ, check_effect_class

ALWAYS_EXPOSE_LIMIT = 20
SIMILARITY_FLOOR = 0.30
TOP_K = 8


@dataclass
class Registry:
    tools: dict[str, Tool] = field(default_factory=dict)

    def add(self, *tools: Tool) -> None:
        """Register tools, refusing any that has not said whether re-running it is safe.

        The check is here as well as in `Tool.__post_init__` on purpose, and the second one
        is not redundant: `effect_class` is a mutable field on a plain dataclass, an MCP
        server hands over tools this process did not write, and a duck-typed stand-in never
        runs `__post_init__` at all. The constructor is the early loud failure; this is the
        one that binds everything that reaches the registry by another road.
        """
        for t in tools:
            check_effect_class(t.name, getattr(t, "effect_class", None))
            self.tools[t.name] = t

    def get(self, name: str) -> Tool | None:
        return self.tools.get(name)

    def enabled(self) -> list[Tool]:
        return [t for t in self.tools.values() if t.enabled]

    def names(self) -> list[str]:
        return sorted(self.tools)

    def with_tags(self, tags: set[str]) -> list[Tool]:
        return [t for t in self.enabled() if set(t.tags) & tags]

    def subset(self, names: list[str] | None, tags: list[str] | None = None) -> dict[str, Tool]:
        """A restricted view for a sub-agent."""
        chosen: dict[str, Tool] = {}
        for t in self.enabled():
            if names is not None and t.name in names:
                chosen[t.name] = t
            elif tags and set(t.tags) & set(tags):
                chosen[t.name] = t
        return chosen

    async def sync_embeddings(self, embedder=None) -> int:
        """Persist tool rows, recomputing embeddings only for changed descriptions."""
        from ..embed import get_embedder

        embedder = embedder or get_embedder()
        existing = {r["name"]: r for r in await repo_ops.tool_rows()}
        changed = 0
        for t in self.tools.values():
            sha = t.desc_sha256()
            row = existing.get(t.name)
            vector = None
            model = None
            if embedder is not None and (row is None or row["desc_sha256"] != sha):
                vector = (await embedder.embed([t.embed_text()]))[0]
                model = embedder.model_name
                changed += 1
            await repo_ops.upsert_tool_row(
                name=t.name, source=t.source, description=t.description,
                input_schema=t.parameters, tags=list(t.tags), risk=t.risk,
                always_on=t.always_on, desc_sha256=sha, embedding=vector, embedding_model=model,
            )
        return changed

    async def select(
        self, query: str, *, session_used: set[str] | None = None, cfg: Config | None = None
    ) -> list[Tool]:
        """Pick the tools this turn gets to see."""
        cfg = cfg or get_config()
        enabled = self.enabled()
        if len(enabled) <= ALWAYS_EXPOSE_LIMIT:
            return enabled

        chosen = {t.name: t for t in enabled if t.always_on}
        for name in session_used or set():
            if name in self.tools and self.tools[name].enabled:
                chosen[name] = self.tools[name]

        from ..embed import get_embedder

        embedder = get_embedder()
        if embedder is not None and query.strip():
            vec = (await embedder.embed([query]))[0]
            added = 0
            for row in await repo_ops.tools_by_similarity(vec, limit=TOP_K * 2):
                if added >= TOP_K or row["score"] < SIMILARITY_FLOOR:
                    break
                t = self.tools.get(row["name"])
                if t and t.enabled and t.name not in chosen:
                    chosen[t.name] = t
                    added += 1
        return list(chosen.values())


_registry: Registry | None = None


def get_registry() -> Registry:
    global _registry
    if _registry is None:
        _registry = build_registry()
    return _registry


def set_registry(registry: Registry | None) -> None:
    global _registry
    _registry = registry


def build_registry() -> Registry:
    """Assemble every builtin tool. Imports are local to keep startup cheap."""
    from . import (
        builtin_agenda,
        builtin_calendar,
        builtin_coursework,
        builtin_delegate,
        builtin_fs,
        builtin_handoff,
        builtin_mail,
        builtin_memory,
        builtin_shell,
        builtin_web,
        builtin_working,
    )

    reg = Registry()
    reg.add(*builtin_memory.TOOLS)
    reg.add(*builtin_mail.TOOLS)
    reg.add(*builtin_calendar.TOOLS)
    reg.add(*builtin_coursework.TOOLS)
    reg.add(*builtin_fs.TOOLS)
    reg.add(*builtin_shell.TOOLS)
    reg.add(*builtin_web.TOOLS)
    reg.add(*builtin_agenda.TOOLS)
    reg.add(*builtin_delegate.TOOLS)
    # Session 7b. The working bucket's only model-facing door. Registered like anything
    # else and not `always_on`: the scratchpad is offered to a turn that sounds like it
    # needs one, and a worker's spec names it when that role's work is worth keeping notes
    # on. Making it always_on would spend a line of every prompt on a tool most turns
    # never call.
    reg.add(*builtin_working.TOOLS)
    # Registered like anything else, and offered on a turn only when a handoff is actually
    # in force - `agent/loop.py` takes it back out otherwise. `always_on` is what makes
    # "offered whenever there is a manifest" reliable rather than dependent on whether the
    # user's wording happened to embed near it; the loop's filter is what makes it the only
    # condition. A tool that can never work, offered on every turn, is a line of the prompt
    # spent teaching the model a call that returns an error.
    reg.add(*builtin_handoff.TOOLS)
    reg.add(tool_search_tool(reg))
    return reg


def tool_search_tool(registry: Registry) -> Tool:
    """Meta-tool: lets the model pull in tools that were not pre-selected."""

    @tool(
        "tool_search",
        "Find tools available beyond the ones already listed, by describing what you need.",
        required(obj(query={"type": "string"}), "query"),
        # read: a SELECT over `tools` plus an embedding call that stores nothing. It does
        # append to `ctx.extra["added_tools"]`, which is turn-scoped context and dies with
        # the turn - execution state, not something a crash could leave half-done.
        effect_class=READ,
        tags=("core",),
        always_on=True,
    )
    async def tool_search(args: dict, ctx: ToolContext) -> ToolResult:
        query = args["query"]
        from ..embed import get_embedder

        embedder = get_embedder()
        found: list[dict[str, Any]] = []
        if embedder is not None:
            vec = (await embedder.embed([query]))[0]
            rows = await repo_ops.tools_by_similarity(vec, limit=TOP_K)
            found = [r for r in rows if r["score"] >= SIMILARITY_FLOOR]
        if not found:
            words = {w for w in query.lower().split() if len(w) > 3}
            found = [
                {"name": t.name, "score": 1.0}
                for t in registry.enabled()
                if words & set(f"{t.name} {t.description}".lower().split())
            ][:TOP_K]
        if not found:
            return ToolResult(content="No matching tools.")
        ctx.extra.setdefault("added_tools", []).extend(r["name"] for r in found)
        lines = [
            f"- {r['name']}: {registry.tools[r['name']].description}"
            for r in found
            if r["name"] in registry.tools
        ]
        return ToolResult(
            content="These tools are now available for the rest of this turn:\n" + "\n".join(lines)
        )

    return tool_search
