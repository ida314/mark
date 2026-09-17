"""Why did this happen? The audit tree, and undoing what it records."""

from __future__ import annotations

import json
from uuid import UUID

from rich.console import Console
from rich.tree import Tree

from ..config import get_config
from ..db import repo_memory, repo_ops

KIND_STYLE = {
    "turn": "bold white",
    "llm_call": "cyan",
    "retrieval": "blue",
    "tool_call": "green",
    "subagent": "magenta",
    "memory_write": "yellow",
    "watcher_fire": "blue",
}


def _label(row: dict) -> str:
    style = KIND_STYLE.get(row["kind"], "white")
    status = row["status"]
    status_style = {"ok": "green", "error": "red", "denied": "yellow"}.get(status, "white")
    parts = [f"[{style}]{row['kind']}[/{style}] {row['name']}", f"[{status_style}]{status}[/{status_style}]"]
    if row.get("duration_ms"):
        parts.append(f"[dim]{row['duration_ms']}ms[/dim]")
    policy = row.get("policy") or {}
    if policy.get("rule"):
        parts.append(f"[dim]rule={policy['rule']}[/dim]")
    if row.get("rationale"):
        parts.append(f'[italic]"{row["rationale"][:80]}"[/italic]')
    if row.get("error"):
        parts.append(f"[red]{row['error'][:120]}[/red]")
    return "  ".join(parts)


async def render_trace(turn_or_action: str, console: Console) -> None:
    cfg = get_config()
    try:
        ident = UUID(turn_or_action)
    except ValueError:
        rows = await repo_ops.actions_by_trace(turn_or_action)
        if not rows:
            console.print("[dim]no such trace[/dim]")
            return
        ident = rows[0]["turn_id"]

    rows = await repo_ops.actions_for_turn(ident)
    if not rows:
        row = await repo_ops.get_action(ident)
        if row is None:
            console.print("[dim]nothing recorded for that id[/dim]")
            return
        rows = [row]

    root_rows = [r for r in rows if r["kind"] == "turn"] or rows[:1]
    root = root_rows[0]
    tree = Tree(_label(root))

    refs = root.get("refs") or {}
    if refs.get("items"):
        tree.add(f"[blue]memory[/blue] retrieved: {', '.join(refs['items'][:12])}")

    by_parent: dict[str | None, list[dict]] = {}
    for row in rows:
        if row["id"] == root["id"]:
            continue
        by_parent.setdefault(str(row.get("parent_id")) if row.get("parent_id") else None, []).append(row)

    def attach(node: Tree, parent_key: str | None) -> None:
        for row in by_parent.get(parent_key, []):
            child = node.add(_label(row))
            output = row.get("output") or {}
            if isinstance(output, dict) and output.get("content"):
                child.add(f"[dim]{str(output['content'])[:200]}[/dim]")
            if row.get("undo"):
                child.add(f"[yellow]undo:[/yellow] agent undo {row['id']}")
            attach(child, str(row["id"]))

    # rows parented to the turn, plus any that never recorded a parent
    attach(tree, str(root["id"]))
    attach(tree, None)
    console.print(tree)
    if root.get("trace_id"):
        console.print(f"[dim]{cfg.obs.jaeger_ui}/trace/{root['trace_id']}[/dim]")


async def render_why(action_id: str, console: Console) -> None:
    """Walk up the causal chain from one action to its root cause."""
    chain: list[dict] = []
    current = await repo_ops.get_action(UUID(action_id))
    if current is None:
        console.print("[dim]no such action[/dim]")
        return
    while current is not None:
        chain.append(current)
        parent = current.get("parent_id")
        current = await repo_ops.get_action(parent) if parent else None

    for depth, row in enumerate(reversed(chain)):
        indent = "  " * depth
        why = row.get("rationale") or (row.get("policy") or {}).get("origin") or ""
        console.print(f"{indent}[bold]{row['kind']}[/bold] {row['name']} [dim]{row['status']}[/dim]")
        if why:
            console.print(f"{indent}  [italic]{why}[/italic]")
        if row.get("refs"):
            console.print(f"{indent}  [dim]refs: {json.dumps(row['refs'])[:200]}[/dim]")


async def undo_action(action_id: str, console: Console) -> bool:
    """Reverse what an action did, where that is possible, and record the reversal."""
    import shutil
    from pathlib import Path

    from ..db.repo_ops import ActionRecord

    row = await repo_ops.get_action(UUID(action_id))
    if row is None:
        console.print("[dim]no such action[/dim]")
        return False
    undo = row.get("undo")
    if not undo:
        console.print("[yellow]that action recorded no undo information[/yellow]")
        return False

    kind = undo.get("type")
    if kind == "restore_file":
        path, backup = Path(undo["path"]), Path(undo["backup"])
        if not backup.exists():
            console.print("[red]backup is gone[/red]")
            return False
        shutil.copy2(backup, path)
        console.print(f"[green]restored[/green] {path}")
    elif kind == "delete_file":
        path = Path(undo["path"])
        if path.exists():
            path.unlink()
        console.print(f"[green]removed[/green] {path}")
    elif kind == "retract_fact":
        await repo_memory.retract_fact(UUID(undo["fact_id"]))
        console.print(f"[green]retracted fact[/green] {undo['fact_id']}")
    elif kind == "git_revert":
        from ..memory.mdrepo import MarkdownRepo

        repo = MarkdownRepo(get_config().paths.memory_repo)
        console.print(repo.revert(undo["sha"]))
    else:
        console.print(f"[yellow]do not know how to undo {kind}[/yellow]")
        return False

    await repo_ops.write_action(
        ActionRecord(
            actor="user", kind="undo", name=str(kind), status="ok",
            rationale=f"undo of action {action_id}", input=undo,
        )
    )
    return True
