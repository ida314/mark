"""`agent` — the command line entry point."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
from pathlib import Path
from uuid import UUID

import typer
from rich.console import Console
from rich.table import Table

from ..config import (
    CONFIG_DIR,
    CONFIG_FILE,
    DEFAULT_CONFIG,
    DEFAULT_POLICY,
    POLICY_FILE,
    REPO_ROOT,
    get_config,
    reset_config_cache,
)

app = typer.Typer(no_args_is_help=True, add_completion=False, help="Your persistent personal agent.")
db_app = typer.Typer(help="Database lifecycle.")
memory_app = typer.Typer(help="Inspect and correct what the agent remembers.")
md_app = typer.Typer(help="The git-backed markdown memory repo.")
goals_app = typer.Typer(help="Goals.")
loops_app = typer.Typer(help="Open loops.")
approvals_app = typer.Typer(help="Actions waiting for your decision.")
daemon_app = typer.Typer(help="The always-on background process.")
policy_app = typer.Typer(help="The rules that gate every tool call.")
tools_app = typer.Typer(help="Tool registry.")
mcp_app = typer.Typer(help="Model Context Protocol server.")
watchers_app = typer.Typer(help="Timers, intervals and file watchers.")

app.add_typer(db_app, name="db")
app.add_typer(memory_app, name="memory")
memory_app.add_typer(md_app, name="md")
app.add_typer(goals_app, name="goals")
app.add_typer(loops_app, name="loops")
app.add_typer(approvals_app, name="approvals")
app.add_typer(daemon_app, name="daemon")
app.add_typer(policy_app, name="policy")
app.add_typer(tools_app, name="tools")
app.add_typer(mcp_app, name="mcp")
app.add_typer(watchers_app, name="watchers")

console = Console()


def run(coro):
    return asyncio.run(coro)


# --- setup -------------------------------------------------------------------


@app.command()
def init() -> None:
    """Create config, data directories and the memory repository."""
    from ..memory.mdrepo import MarkdownRepo

    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    for src, dst in ((DEFAULT_CONFIG, CONFIG_FILE), (DEFAULT_POLICY, POLICY_FILE)):
        if dst.exists():
            console.print(f"[dim]kept existing {dst}[/dim]")
        else:
            shutil.copy(src, dst)
            console.print(f"[green]wrote[/green] {dst}")
    reset_config_cache()
    cfg = get_config()
    cfg.ensure_dirs()
    repo = MarkdownRepo(cfg.paths.memory_repo)
    repo.init()
    console.print(f"[green]memory repo[/green] {cfg.paths.memory_repo}")
    console.print(f"[green]workspace[/green]   {cfg.paths.workspace}")
    console.print("\nNext: [bold]agent db up && agent db migrate && agent doctor[/bold]")


@app.command()
def doctor() -> None:
    """Check every dependency, including whether tool calling actually works."""
    run(_doctor())


async def _doctor() -> None:
    cfg = get_config()
    table = Table(show_header=True, header_style="bold")
    table.add_column("check")
    table.add_column("result")
    table.add_column("detail", overflow="fold")

    def row(name: str, ok: bool | None, detail: str = "") -> None:
        mark = "[green]ok[/green]" if ok else ("[yellow]warn[/yellow]" if ok is None else "[red]fail[/red]")
        table.add_row(name, mark, detail)

    # config
    row("config", CONFIG_FILE.exists() or None, str(CONFIG_FILE if CONFIG_FILE.exists() else DEFAULT_CONFIG))

    # database
    from ..db import migrate as migrate_mod
    from ..db.pool import fetch_one, wait_ready

    db_ok = await wait_ready(cfg, timeout=5)
    row("postgres", db_ok, cfg.db.dsn)
    if db_ok:
        try:
            applied, pending = await migrate_mod.status(cfg)
            row("migrations", not pending, f"{len(applied)} applied, {len(pending)} pending")
            ext = await fetch_one("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
            row("pgvector", bool(ext), ext["extversion"] if ext else "missing")
        except Exception as exc:
            row("migrations", False, str(exc)[:200])

    # model endpoint and, crucially, tool calls
    from ..llm.openai_compat import OpenAICompatProvider

    provider = OpenAICompatProvider(cfg)
    try:
        models = await provider.client.models.list()
        names = [m.id for m in models.data]
        row("llm endpoint", True, f"{cfg.llm.base_url} -> {', '.join(names[:3])}")
        configured = cfg.llm.model in names
        row("model", configured or None, cfg.llm.model if configured else f"{cfg.llm.model} not listed")
        ok, detail = await provider.probe_tool_calls()
        row("tool calling", ok, detail[:160])
    except Exception as exc:
        row("llm endpoint", False, f"{cfg.llm.base_url}: {exc}"[:200])

    # embeddings
    if cfg.embed.enabled:
        from ..embed import get_embedder

        try:
            embedder = get_embedder(cfg)
            vec = (await embedder.embed(["hello"]))[0]
            row("embeddings", len(vec) == cfg.embed.dim, f"{cfg.embed.model} dim={len(vec)}")
        except Exception as exc:
            row("embeddings", False, str(exc)[:200])

    # sandbox
    if shutil.which("docker"):
        image = subprocess.run(
            ["docker", "images", "-q", cfg.sandbox.image], capture_output=True, text=True
        ).stdout.strip()
        row("sandbox image", bool(image) or None, cfg.sandbox.image if image else "run: agent sandbox build")
    else:
        row("docker", False, "not installed: sandboxed shell disabled")

    # policy
    try:
        from ..policy.engine import engine_from_config

        engine = engine_from_config(cfg)
        row("policy", True, f"{len(engine.policy.rules)} rules, {len(engine.policy.hard_deny)} hard denies")
    except Exception as exc:
        row("policy", False, str(exc)[:200])

    # external MCP servers (only worth reporting if any are configured)
    if cfg.mcp.servers:
        from ..mcp_client import close_external_tools, load_external_tools

        clients = await load_external_tools(cfg)
        try:
            imported = sum(len(v) for v in clients.loaded.values())
            detail = f"{len(clients.loaded)}/{len(cfg.mcp.servers)} connected, {imported} tools"
            if clients.failures:
                detail += " · " + "; ".join(f"{k}: {v}" for k, v in clients.failures.items())
            detail += "".join(f" · {w}" for w in clients.warnings)
            row("mcp servers", not clients.failures, detail[:200])
        finally:
            await close_external_tools()

    # daemon
    if db_ok:
        from ..db import repo_ops

        status = await repo_ops.daemon_status()
        beat = status.get("heartbeat_at") if status else None
        if beat:
            from ..ids import utcnow

            # the row outlives the process, so a stale beat means it is gone, not healthy
            age = (utcnow() - beat).total_seconds()
            stopped = (status.get("info") or {}).get("status") == "stopped"
            fresh = not stopped and age < 2 * cfg.daemon.scheduler_poll_s + 3600
            row(
                "daemon",
                fresh or None,
                f"pid {status['pid']}, last beat {beat:%H:%M:%S}"
                + ("" if fresh else f" ({int(age / 60)} min ago — not running?)"),
            )
        else:
            row("daemon", None, "not running (agent daemon run)")

    console.print(table)


# --- database ----------------------------------------------------------------


@db_app.command("up")
def db_up() -> None:
    """Start Postgres with docker compose."""
    subprocess.run(["docker", "compose", "up", "-d"], cwd=REPO_ROOT, check=False)


@db_app.command("down")
def db_down() -> None:
    """Stop Postgres (data is kept in the volume)."""
    subprocess.run(["docker", "compose", "down"], cwd=REPO_ROOT, check=False)


@db_app.command("wait")
def db_wait(timeout: float = 60.0) -> None:
    """Block until Postgres accepts connections."""
    from ..db.pool import wait_ready

    ok = run(wait_ready(get_config(), timeout))
    if not ok:
        console.print("[red]database did not become ready[/red]")
        raise typer.Exit(1)


@db_app.command("migrate")
def db_migrate(dry_run: bool = False) -> None:
    """Apply pending migrations."""
    from ..db import migrate as migrate_mod

    if dry_run:
        applied, pending = run(migrate_mod.status(get_config()))
        console.print(f"applied: {applied}\npending: {pending}")
        return
    versions = run(migrate_mod.migrate(get_config()))
    console.print(f"[green]applied[/green] {versions}" if versions else "[dim]already current[/dim]")


@db_app.command("status")
def db_status() -> None:
    from ..db import migrate as migrate_mod

    applied, pending = run(migrate_mod.status(get_config()))
    console.print(f"applied: {len(applied)}\npending: {pending or 'none'}")


# --- conversation ------------------------------------------------------------


@app.command()
def chat(
    autonomy: str = typer.Option("assist", help="observe | assist | act"),
    resume: str | None = typer.Option(None, help="Session id to continue"),
    show_thinking: bool = typer.Option(False, help="Show the model's reasoning stream"),
) -> None:
    """Talk to your agent."""
    from .chat import run_chat

    run(run_chat(get_config(), autonomy=autonomy, resume=resume, show_thinking=show_thinking))


@app.command()
def ask(
    prompt: str,
    autonomy: str = typer.Option("assist"),
) -> None:
    """One question, one answer, no REPL."""
    from ..agent.loop import one_shot
    from ..mcp_client import close_external_tools, load_external_tools
    from ..policy.approvals import QueueApprover

    async def _ask() -> str:
        await load_external_tools()
        try:
            return await one_shot(
                prompt, autonomy=autonomy, approver=QueueApprover(origin="interactive")
            )
        finally:
            await close_external_tools()

    console.print(run(_ask()))


@app.command()
def remember(text: str) -> None:
    """Tell the agent something worth keeping."""
    run(_remember(text))


async def _remember(text: str) -> None:
    from ..db import repo_memory
    from ..memory import review

    candidate_id = await repo_memory.insert_candidate(
        statement=text, proposed_by="user", confidence=0.95,
        structured={"category": "other"}, evidence=[{"source": "user"}],
    )
    for row in await repo_memory.pending_candidates():
        if row["id"] == candidate_id:
            status, reason = await review.process_candidate(row)
            console.print(f"[green]{status}[/green]: {reason}")


@app.command()
def consolidate(
    session: str | None = typer.Option(None, help="Session id"),
    nightly: bool = typer.Option(False, help="Run the nightly job now"),
) -> None:
    """Turn raw conversation into durable memory."""
    from ..memory import consolidate as consolidate_mod

    if nightly:
        console.print(run(consolidate_mod.nightly(get_config())))
    elif session:
        console.print(run(consolidate_mod.post_session(UUID(session), get_config())))
    else:
        console.print(run(consolidate_mod.maybe_consolidate_idle(get_config())))


# --- memory ------------------------------------------------------------------


@memory_app.command("search")
def memory_search(
    query: str,
    as_of: str | None = typer.Option(None, help="What was true on this date"),
    history: bool = typer.Option(False, help="Include superseded facts"),
    deep: bool = typer.Option(False, help="Slower, better retrieval"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Search memory the way the agent does."""
    run(_memory_search(query, as_of, history, deep, as_json))


async def _memory_search(query, as_of, history, deep, as_json) -> None:
    from ..ids import parse_when
    from ..memory.retrieval import pack

    cfg = get_config()
    result = await pack(
        query,
        budget_tokens=cfg.retrieval.deep_budget_tokens if deep else cfg.retrieval.fast_budget_tokens,
        mode="deep" if deep else "fast",
        as_of=parse_when(as_of) if as_of else None,
        include_history=history,
    )
    if as_json:
        console.print_json(
            json.dumps(
                {
                    "items": [
                        {"ref": i.ref, "kind": i.kind, "text": i.text, "score": round(i.score, 4)}
                        for i in result.items
                    ],
                    "conflicts": result.conflicts,
                    "stats": result.stats,
                }
            )
        )
        return
    console.print(result.text or "[dim]nothing found[/dim]")
    if result.conflicts:
        console.print(f"[yellow]conflicts:[/yellow] {'; '.join(result.conflicts)}")
    console.print(f"[dim]{result.stats}[/dim]")


@memory_app.command("facts")
def memory_facts(
    category: str | None = typer.Option(None),
    limit: int = typer.Option(50),
) -> None:
    """List the facts currently believed."""
    run(_memory_facts(category, limit))


async def _memory_facts(category, limit) -> None:
    from ..db import repo_memory

    rows = await repo_memory.active_facts(category=category, limit=limit)
    if not rows:
        console.print("[dim]no facts yet[/dim]")
        return
    table = Table(show_header=True, header_style="bold")
    table.add_column("id", style="dim")
    table.add_column("category")
    table.add_column("statement", overflow="fold")
    table.add_column("conf")
    table.add_column("since")
    for row in rows:
        table.add_row(
            str(row["id"])[:8], row["category"], row["statement"],
            f"{row['confidence']:.2f}",
            f"{row['valid_from']:%Y-%m-%d}" if row.get("valid_from") else "",
        )
    console.print(table)


@memory_app.command("history")
def memory_history(subject: str) -> None:
    """How belief about a subject changed over time."""
    run(_memory_history(subject))


async def _memory_history(subject: str) -> None:
    from ..db import repo_memory

    rows = await repo_memory.facts_for_subject(subject)
    if not rows:
        console.print("[dim]nothing recorded[/dim]")
        return
    for row in rows:
        window = []
        if row.get("valid_from"):
            window.append(f"from {row['valid_from']:%Y-%m-%d}")
        if row.get("valid_to"):
            window.append(f"until {row['valid_to']:%Y-%m-%d}")
        color = {"active": "green", "superseded": "yellow", "retracted": "red"}[row["status"]]
        console.print(
            f"[{color}]{row['status']:<10}[/{color}] {row['statement']} "
            f"[dim]{' '.join(window)} · recorded {row['recorded_at']:%Y-%m-%d} · {str(row['id'])[:8]}[/dim]"
        )


@memory_app.command("review")
def memory_review() -> None:
    """Decide the memories the gate was unsure about."""
    run(_memory_review())


async def _memory_review() -> None:
    from ..db import repo_memory
    from ..memory import review as review_mod

    rows = await repo_memory.candidates_by_status("needs_review")
    if not rows:
        console.print("[dim]nothing waiting for review[/dim]")
        return
    for row in rows:
        console.print(
            f"\n[bold]{row['statement']}[/bold]\n"
            f"[dim]by {row['proposed_by']} · confidence {row['confidence']:.2f} · "
            f"{row['decision_reason'] or ''}[/dim]"
        )
        answer = typer.prompt("accept / reject / skip", default="skip")
        if answer.startswith("a"):
            await repo_memory.decide_candidate(row["id"], "pending", "user accepted", decided_by="user")
            fresh = [c for c in await repo_memory.pending_candidates() if c["id"] == row["id"]]
            if fresh:
                candidate = dict(fresh[0])
                candidate["proposed_by"] = "user"
                candidate["confidence"] = max(0.95, float(candidate["confidence"]))
                status, reason = await review_mod.process_candidate(candidate)
                console.print(f"[green]{status}[/green]: {reason}")
        elif answer.startswith("r"):
            await repo_memory.decide_candidate(
                row["id"], "rejected", "user rejected", decided_by="user"
            )


@memory_app.command("retract")
def memory_retract(fact_id: str, reason: str = typer.Option("user retracted")) -> None:
    """Mark a fact as never having been true."""
    run(_memory_retract(fact_id, reason))


async def _memory_retract(fact_id: str, reason: str) -> None:
    from ..db import repo_memory
    from ..db.repo_ops import ActionRecord, write_action

    row = (
        await repo_memory.fact_by_short_id(fact_id.replace("-", ""))
        if len(fact_id) < 32
        else await repo_memory.get_fact(UUID(fact_id))
    )
    if row is None:
        console.print("[red]no such fact[/red]")
        raise typer.Exit(1)
    await repo_memory.retract_fact(row["id"])
    await write_action(
        ActionRecord(actor="user", kind="memory_write", name="retract", status="ok",
                     rationale=reason, refs={"fact": str(row["id"])})
    )
    console.print(f"[green]retracted[/green] {row['statement']}")


@memory_app.command("reembed")
def memory_reembed() -> None:
    """Recompute embeddings (after changing the embedding model)."""
    run(_memory_reembed())


async def _memory_reembed() -> None:
    from ..db.pool import connection, fetch_all
    from ..embed import get_embedder

    embedder = get_embedder()
    if embedder is None:
        console.print("[red]embeddings are disabled[/red]")
        return
    for table, column in (("facts", "statement"), ("episodes", "summary"), ("procedures", "description")):
        rows = await fetch_all(f"SELECT id, {column} AS text FROM {table}")
        if not rows:
            continue
        vectors = await embedder.embed([r["text"] for r in rows])
        async with connection() as conn:
            for row, vector in zip(rows, vectors, strict=True):
                await conn.execute(
                    f"UPDATE {table} SET embedding = %s, embedding_model = %s WHERE id = %s",
                    (vector, embedder.model_name, row["id"]),
                )
        console.print(f"[green]{table}[/green]: {len(rows)} re-embedded")


@md_app.command("log")
def md_log(limit: int = 20) -> None:
    """History of the markdown memory repo."""
    from ..memory.mdrepo import MarkdownRepo

    for entry in MarkdownRepo(get_config().paths.memory_repo).log(limit):
        console.print(f"{entry['sha']}  [dim]{entry['date']} {entry['author']}[/dim]  {entry['subject']}")


@md_app.command("revert")
def md_revert(sha: str, retract_facts: bool = typer.Option(False)) -> None:
    """Undo a commit in the markdown repo."""
    from ..memory.mdrepo import MarkdownRepo

    if not retract_facts:
        console.print(
            "[yellow]note:[/yellow] generated blocks are rebuilt from the database, so "
            "reverting alone will be undone by the next nightly run. Use --retract-facts "
            "to retract the underlying facts too."
        )
    console.print(MarkdownRepo(get_config().paths.memory_repo).revert(sha))


@app.command()
def profile(section: str | None = typer.Argument(None), edit: bool = typer.Option(False)) -> None:
    """Show or edit the markdown profile."""
    from ..memory.mdrepo import MarkdownRepo

    repo = MarkdownRepo(get_config().paths.memory_repo)
    if edit:
        target = repo.root / (section or "agent/instructions.md")
        target.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run([os.environ.get("EDITOR", "vi"), str(target)], check=False)
        run(_sync_md())
        return
    console.print(repo.read_section(section) if section else repo.read_core() or "[dim]empty[/dim]")


async def _sync_md() -> None:
    from ..memory.consolidate import sync_user_edits

    commit = await sync_user_edits(get_config())
    console.print(f"[green]committed[/green] {commit}" if commit else "[dim]no changes[/dim]")


# --- agenda ------------------------------------------------------------------


@goals_app.command("list")
def goals_list(status: str = typer.Option("active")) -> None:
    run(_goals_list(status))


async def _goals_list(status: str) -> None:
    from ..db import repo_agenda

    rows = await repo_agenda.list_goals(None if status == "all" else status)
    if not rows:
        console.print("[dim]no goals[/dim]")
        return
    for row in rows:
        due = f" due {row['due_at']:%Y-%m-%d}" if row.get("due_at") else ""
        console.print(
            f"[bold]{row['slug']}[/bold] {row['title']} "
            f"[dim]p{row['priority']} {row['horizon']}{due} {row['status']}[/dim]"
        )
        if row.get("next_step"):
            console.print(f"    next: {row['next_step']}")


@goals_app.command("add")
def goals_add(
    title: str,
    priority: int = typer.Option(3),
    horizon: str = typer.Option("quarter"),
    next_step: str | None = typer.Option(None),
) -> None:
    from ..db import repo_agenda

    run(repo_agenda.upsert_goal(title=title, priority=priority, horizon=horizon, next_step=next_step))
    console.print("[green]saved[/green]")


@goals_app.command("done")
def goals_done(slug: str) -> None:
    from ..db import repo_agenda

    run(repo_agenda.set_goal_status(slug, "done"))
    console.print("[green]done[/green]")


@goals_app.command("drop")
def goals_drop(slug: str) -> None:
    from ..db import repo_agenda

    run(repo_agenda.set_goal_status(slug, "dropped"))
    console.print("[green]dropped[/green]")


@loops_app.command("list")
def loops_list(status: str = typer.Option("open")) -> None:
    run(_loops_list(status))


async def _loops_list(status: str) -> None:
    from ..db import repo_agenda

    rows = await repo_agenda.list_open_loops(None if status == "all" else status)
    for row in rows:
        due = f" due {row['due_at']:%Y-%m-%d}" if row.get("due_at") else ""
        console.print(f"{str(row['id'])[:8]}  {row['title']} [dim]{row['status']}{due}[/dim]")
    if not rows:
        console.print("[dim]no open loops[/dim]")


@loops_app.command("add")
def loops_add(title: str, due: str | None = typer.Option(None)) -> None:
    from ..db import repo_agenda
    from ..ids import parse_when

    run(repo_agenda.add_open_loop(title=title, due_at=parse_when(due) if due else None))
    console.print("[green]tracked[/green]")


@loops_app.command("close")
def loops_close(loop_id: str) -> None:
    from ..db import repo_agenda

    run(repo_agenda.close_open_loop(UUID(loop_id)))
    console.print("[green]closed[/green]")


@app.command()
def remind(text: str, at: str = typer.Option(..., "--at", "--in", help="ISO time or '10m'")) -> None:
    """Set a reminder the daemon will deliver."""
    run(_remind(text, at))


async def _remind(text: str, at: str) -> None:
    from ..db import repo_agenda
    from ..ids import parse_when

    when = parse_when(at)
    if when is None:
        console.print(f"[red]could not parse time {at!r}[/red]")
        raise typer.Exit(1)
    await repo_agenda.add_watcher(
        name=f"reminder: {text[:40]}", kind="once", spec={"at": when.isoformat()},
        action={"type": "notify", "text": text}, created_by="user", next_fire_at=when,
    )
    console.print(f"[green]reminder set[/green] for {when.astimezone():%Y-%m-%d %H:%M %Z}")


@watchers_app.command("list")
def watchers_list() -> None:
    run(_watchers_list())


async def _watchers_list() -> None:
    from ..db import repo_agenda

    rows = await repo_agenda.list_watchers()
    for row in rows:
        state = "on" if row["enabled"] else "off"
        nxt = f" next {row['next_fire_at']:%Y-%m-%d %H:%M}" if row.get("next_fire_at") else ""
        console.print(f"{str(row['id'])[:8]}  {row['name']} [dim]{row['kind']} {state}{nxt}[/dim]")
    if not rows:
        console.print("[dim]no watchers[/dim]")


@watchers_app.command("disable")
def watchers_disable(watcher_id: str) -> None:
    from ..db import repo_agenda

    run(repo_agenda.set_watcher_enabled(UUID(watcher_id), False))
    console.print("[green]disabled[/green]")


@app.command()
def inbox(all_: bool = typer.Option(False, "--all"), mark_read: bool = typer.Option(False)) -> None:
    """Notifications."""
    run(_inbox(all_, mark_read))


async def _inbox(all_: bool, mark_read: bool) -> None:
    from ..db import repo_agenda

    rows = await repo_agenda.list_notifications(unread_only=not all_)
    for row in rows:
        color = {"info": "cyan", "warn": "yellow", "error": "red"}.get(row["level"], "white")
        console.print(f"[{color}]●[/{color}] {row['title']} [dim]{row['created_at']:%m-%d %H:%M}[/dim]")
        if row.get("body"):
            console.print(f"   {row['body'][:300]}")
    if not rows:
        console.print("[dim]inbox empty[/dim]")
    if mark_read:
        await repo_agenda.mark_read()


# --- approvals ---------------------------------------------------------------


@approvals_app.command("list")
def approvals_list(status: str = typer.Option("pending")) -> None:
    run(_approvals_list(status))


async def _approvals_list(status: str) -> None:
    from ..db import repo_ops

    rows = await repo_ops.list_approvals(None if status == "all" else status)
    if not rows:
        console.print("[dim]nothing pending[/dim]")
        return
    for row in rows:
        console.print(
            f"[bold]{row['id']}[/bold] {row['tool_name']} [dim]{row['risk']} · "
            f"{row['origin']} · {row['created_at']:%m-%d %H:%M} · {row['status']}[/dim]"
        )
        if row.get("reason"):
            console.print(f"   why: {row['reason']}")


@approvals_app.command("show")
def approvals_show(approval_id: str) -> None:
    run(_approvals_show(approval_id))


async def _approvals_show(approval_id: str) -> None:
    from ..db import repo_ops

    row = await repo_ops.get_approval(UUID(approval_id))
    if row is None:
        console.print("[red]no such approval[/red]")
        return
    console.print_json(json.dumps({k: str(v) for k, v in row.items()}, indent=2))


@approvals_app.command("approve")
def approvals_approve(approval_id: str, note: str | None = typer.Option(None)) -> None:
    """Approve a queued action and run it now."""
    run(_approvals_decide(approval_id, True, note))


@approvals_app.command("deny")
def approvals_deny(approval_id: str, note: str | None = typer.Option(None)) -> None:
    run(_approvals_decide(approval_id, False, note))


async def _approvals_decide(approval_id: str, approve: bool, note: str | None) -> None:
    from ..db import repo_ops
    from ..policy.approvals import AutoApprover
    from ..policy.engine import engine_from_config
    from ..tools.base import ToolContext
    from ..tools.executor import ToolExecutor
    from ..tools.registry import get_registry

    cfg = get_config()
    row = await repo_ops.get_approval(UUID(approval_id))
    if row is None:
        console.print("[red]no such approval[/red]")
        raise typer.Exit(1)
    if row["status"] != "pending":
        console.print(f"[yellow]already {row['status']}[/yellow]")
        return
    if not approve:
        await repo_ops.decide_approval(UUID(approval_id), "denied", "user", note)
        console.print("[green]denied[/green]")
        return

    args = row["args"]
    if repo_ops.args_hash(args) != row["args_sha256"]:
        console.print("[red]arguments changed since the request; refusing[/red]")
        raise typer.Exit(1)

    await repo_ops.decide_approval(UUID(approval_id), "approved", "user", note)
    registry = get_registry()
    executor = ToolExecutor(registry.tools, engine_from_config(cfg), AutoApprover(True))
    ctx = ToolContext(
        session_id=row.get("session_id"), turn_id=row.get("turn_id"), actor="user",
        origin="interactive", autonomy="act",
    )
    result = await executor.run(row["tool_name"], args, ctx)
    await repo_ops.finish_approval(
        UUID(approval_id), "executed" if result.ok else "failed",
        {"content": result.content[:2000], "ok": result.ok},
    )
    console.print(result.content[:2000])


# --- observability -----------------------------------------------------------


@app.command()
def trace(identifier: str) -> None:
    """Why did this happen? Shows the tree for a turn, trace or action."""
    from .commands_trace import render_trace

    run(render_trace(identifier, console))


@app.command()
def why(action_id: str) -> None:
    """Walk an action back to its root cause."""
    from .commands_trace import render_why

    run(render_why(action_id, console))


@app.command()
def undo(action_id: str) -> None:
    """Reverse an action, where that is possible."""
    from .commands_trace import undo_action

    ok = run(undo_action(action_id, console))
    raise typer.Exit(0 if ok else 1)


@app.command()
def actions(limit: int = typer.Option(30)) -> None:
    """Recent audit entries."""
    run(_actions(limit))


async def _actions(limit: int) -> None:
    from ..db import repo_ops

    for row in reversed(await repo_ops.recent_actions(limit)):
        color = {"ok": "green", "error": "red", "denied": "yellow"}.get(row["status"], "white")
        console.print(
            f"[dim]{row['ts']:%m-%d %H:%M:%S}[/dim] [{color}]{row['status']:<7}[/{color}] "
            f"{row['actor']:<18} {row['kind']:<12} {row['name']} [dim]{str(row['id'])[:8]}[/dim]"
        )


# --- policy and tools --------------------------------------------------------


@policy_app.command("check")
def policy_check() -> None:
    """Validate the policy file."""
    from ..policy.engine import engine_from_config

    try:
        engine = engine_from_config(get_config())
    except Exception as exc:
        console.print(f"[red]invalid:[/red] {exc}")
        raise typer.Exit(1) from exc
    console.print(
        f"[green]ok[/green] {len(engine.policy.hard_deny)} hard denies, "
        f"{len(engine.policy.rules)} rules"
    )


@policy_app.command("explain")
def policy_explain(
    tool_name: str,
    args: str = typer.Argument("{}"),
    autonomy: str = typer.Option("assist"),
    origin: str = typer.Option("interactive"),
    tainted: bool = typer.Option(False),
) -> None:
    """Would this call be allowed? Why?"""
    from ..policy.engine import PolicyContext, ToolCallInfo, engine_from_config
    from ..tools.registry import get_registry

    cfg = get_config()
    tool = get_registry().get(tool_name)
    if tool is None:
        console.print(f"[red]no such tool: {tool_name}[/red]")
        raise typer.Exit(1)
    decision = engine_from_config(cfg).evaluate(
        ToolCallInfo(
            name=tool.name, risk=tool.risk, tags=tool.tags, source=tool.source,
            args=json.loads(args), path_args=tool.path_args,
        ),
        PolicyContext(autonomy=autonomy, origin=origin, tainted=tainted),
    )
    color = {"allow": "green", "deny": "red", "require_approval": "yellow"}[decision.outcome]
    console.print(
        f"[{color}]{decision.outcome}[/{color}]  rule={decision.rule_id}"
        + (f"\n{decision.reason}" if decision.reason else "")
    )


@tools_app.command("list")
def tools_list() -> None:
    from ..tools.registry import get_registry

    table = Table(show_header=True, header_style="bold")
    table.add_column("tool")
    table.add_column("risk")
    table.add_column("tags")
    table.add_column("description", overflow="fold")
    for tool in sorted(get_registry().enabled(), key=lambda t: (t.risk, t.name)):
        table.add_row(tool.name, tool.risk, ",".join(tool.tags), tool.description)
    console.print(table)


@tools_app.command("sync")
def tools_sync() -> None:
    """Persist tool rows and refresh their embeddings."""
    from ..tools.registry import get_registry

    changed = run(get_registry().sync_embeddings())
    console.print(f"[green]synced[/green], {changed} embeddings recomputed")


@app.command("sandbox")
def sandbox(action: str = typer.Argument("build")) -> None:
    """Build the sandbox container image."""
    if action != "build":
        console.print("only 'build' is supported")
        raise typer.Exit(1)
    cfg = get_config()
    subprocess.run(
        ["docker", "build", "-t", cfg.sandbox.image, "-f", "docker/sandbox.Dockerfile", "."],
        cwd=REPO_ROOT, check=False,
    )


# --- daemon and mcp ----------------------------------------------------------


@daemon_app.command("run")
def daemon_run() -> None:
    """Run the background process in the foreground."""
    from ..daemon.main import run as daemon_main

    raise typer.Exit(run(daemon_main(get_config())))


@daemon_app.command("status")
def daemon_status() -> None:
    run(_daemon_status())


async def _daemon_status() -> None:
    from ..db import repo_ops

    row = await repo_ops.daemon_status()
    if not row:
        console.print("[yellow]daemon has never run[/yellow]")
        return
    console.print(
        f"pid {row['pid']} on {row['host']}, last heartbeat {row['heartbeat_at']:%Y-%m-%d %H:%M:%S}"
    )


@daemon_app.command("install-unit")
def daemon_install_unit() -> None:
    """Install the systemd --user unit."""
    source = REPO_ROOT / "deploy" / "agent-daemon.service"
    target = Path.home() / ".config" / "systemd" / "user" / "agent-daemon.service"
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(source, target)
    console.print(f"[green]installed[/green] {target}\n")
    console.print("systemctl --user daemon-reload")
    console.print("systemctl --user enable --now agent-daemon")


@mcp_app.command("serve")
def mcp_serve(
    http: bool = typer.Option(False, help="Serve over HTTP instead of stdio"),
    port: int | None = typer.Option(None),
) -> None:
    """Expose memory and agenda over MCP (for OpenClaw and other clients)."""
    from ..mcp_server import serve

    serve(http=http, port=port or get_config().mcp.http_port)


@mcp_app.command("list")
def mcp_list() -> None:
    """Show configured external MCP servers."""
    cfg = get_config()
    if not cfg.mcp.servers:
        console.print("[dim]no external MCP servers configured[/dim]")
        return
    for name, server in cfg.mcp.servers.items():
        state = "enabled" if server.enabled else "disabled"
        target = server.url or f"{server.command} {' '.join(server.args)}"
        console.print(f"[bold]{name}[/bold] [dim]{state} · {target}[/dim]")
    console.print("[dim]`agent mcp tools` connects and shows what they actually offer.[/dim]")


@mcp_app.command("tools")
def mcp_tools() -> None:
    """Connect to the configured servers and show the tools they import."""
    from ..mcp_client import close_external_tools, load_external_tools
    from ..tools.registry import get_registry

    async def _probe() -> None:
        registry = get_registry()
        clients = await load_external_tools(registry=registry)
        try:
            if not clients.loaded and not clients.failures:
                console.print("[dim]no external MCP servers configured[/dim]")
            for server, names in clients.loaded.items():
                console.print(f"[bold]mcp:{server}[/bold] [dim]{len(names)} tools[/dim]")
                for tool_name in names:
                    t = registry.get(tool_name)
                    if t is not None:
                        trust = "trusted" if t.trust_output else "untrusted output"
                        console.print(f"  {t.name} [dim]risk={t.risk} · {trust}[/dim]")
            for server, why in clients.failures.items():
                console.print(f"[red]mcp:{server}[/red] [dim]{why}[/dim]")
            for warning in clients.warnings:
                console.print(f"[yellow]{warning}[/yellow]")
        finally:
            await close_external_tools()

    run(_probe())


def main() -> None:
    app()


if __name__ == "__main__":
    main()
