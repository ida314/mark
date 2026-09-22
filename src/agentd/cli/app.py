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
secrets_app = typer.Typer(help="Credentials the agent itself cannot read.")
connectors_app = typer.Typer(help="Daemon-side feeds. They hold credentials the agent cannot read.")
telegram_app = typer.Typer(help="The Telegram chat channel.")
journal_app = typer.Typer(help="The run journal: the event log every frontend subscribes to.")

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
app.add_typer(secrets_app, name="secrets")
app.add_typer(connectors_app, name="connectors")
app.add_typer(telegram_app, name="telegram")
app.add_typer(journal_app, name="journal")

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

    # The review queue. `agent doctor` already reports whether the daemon is running, but a
    # yellow "not running" beside an otherwise green table reads as benign — on 2026-09-18 it
    # was sitting next to five candidates that would never be adjudicated. What was missing is
    # the consequence, so this row states it.
    if db_ok:
        try:
            from ..db import repo_memory, repo_ops
            from ..ids import utcnow as _now

            health = await repo_memory.queue_health()
            now = _now()

            def _waited(ts) -> float:
                return (now - ts).total_seconds() if ts else 0.0

            user_wait = _waited(health["oldest_user_at"])
            oldest = _waited(health["oldest_pending_at"])
            detail = f"{health['pending']} pending, {health['needs_review']} need review"
            if oldest:
                detail += f", oldest {int(oldest // 60)}m"

            # The queue has exactly one consumer. If it is not running, a shallow queue is
            # not a healthy queue — it is a queue that has not filled up *yet*. Reporting
            # this green beside a yellow "daemon: not running" is what made the original
            # failure invisible, so the two facts are judged together.
            beat = (await repo_ops.daemon_status() or {}).get("heartbeat_at")
            drain_dead = beat is None or _waited(beat) > 2 * cfg.daemon.heartbeat_interval_s

            if user_wait > cfg.review.user_pending_warn_after_s:
                row("review queue", False,
                    f"{detail} — a correction of yours is still unadjudicated; "
                    "run: agent memory queue --process")
            elif health["pending"] and drain_dead:
                row("review queue", None,
                    f"{detail} — nothing is draining it (daemon down); "
                    "run: agent memory queue --process")
            elif oldest > cfg.review.pending_warn_after_s:
                row("review queue", None, f"{detail} — run: agent memory queue --process")
            else:
                row("review queue", True, detail)
        except Exception as exc:
            row("review queue", False, str(exc)[:200])

    # policy
    try:
        from ..policy.engine import engine_from_config

        engine = engine_from_config(cfg)
        row("policy", True, f"{len(engine.policy.rules)} rules, {len(engine.policy.hard_deny)} hard denies")
    except Exception as exc:
        row("policy", False, str(exc)[:200])

    # secrets vault: only its permissions, never its contents
    from .. import secrets as vault

    problem = vault.check_permissions()
    if vault.SECRETS_FILE.exists():
        try:
            entries = len(vault.describe())
        except vault.VaultPermissionError:
            entries = 0
        row("secrets", problem is None, problem or f"{entries} entries, mode 0600")

    # the chat channel: whether it can run, never the token.
    #
    # Reported whenever there is any evidence of intent — a token, an allowlist, or the
    # flag — rather than only when enabled. `enabled = false` with everything else in place
    # is a real state somebody gets stuck in, and a doctor that says nothing about it is a
    # doctor that helped them get stuck.
    from ..daemon import telegram as tg

    set_up = bool(cfg.telegram.allowed_chat_ids) or tg.token() is not None
    if cfg.telegram.enabled or set_up:
        why = tg.configured(cfg)
        if not cfg.telegram.enabled:
            row(
                "channel:telegram", None,
                "set up but [telegram] enabled = false in config.toml, so nothing polls",
            )
        else:
            row(
                "channel:telegram",
                None if why else True,
                why or f"{len(cfg.telegram.allowed_chat_ids)} allowed chat id(s)",
            )

    # connectors: status only, never a credential
    if db_ok and cfg.connectors.enabled:
        from . import commands_connect

        try:
            await commands_connect.doctor_rows(cfg, row)
        except Exception as exc:
            row("connectors", False, str(exc)[:200])

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


# --- secrets -----------------------------------------------------------------


@secrets_app.command("set")
def secrets_set(
    ref: str = typer.Argument(..., help="e.g. github/dyd2008 or google/you@example.com"),
    field: str = typer.Argument(..., help="e.g. token, refresh_token"),
    value: str | None = typer.Argument(None, help="omit to read from stdin, keeping it out of history"),
) -> None:
    """Store a credential. The agent cannot read this file: the policy hard-denies the path."""
    from .. import secrets as vault

    if value is None or value == "-":
        import sys

        value = (sys.stdin.read() if not sys.stdin.isatty() else typer.prompt(field, hide_input=True))
        value = value.strip()
    if not value:
        console.print("[red]empty value[/red]")
        raise typer.Exit(1)
    vault.put(ref, **{field: value})
    console.print(
        f"[green]stored[/green] {ref}.{field} "
        f"[dim]({vault.Secret(value).fingerprint()}) in {vault.SECRETS_FILE}[/dim]"
    )


@secrets_app.command("list")
def secrets_list() -> None:
    """Show what is stored — names and fingerprints only, never values."""
    from .. import secrets as vault

    try:
        entries = vault.describe()
    except vault.VaultPermissionError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc
    if not entries:
        console.print(f"[dim]nothing stored in {vault.SECRETS_FILE}[/dim]")
        return
    for ref, fields in entries:
        marks = []
        for field in fields:
            got = vault.get(ref, field)
            marks.append(f"{field}={got.fingerprint()}" if got else field)
        console.print(f"[bold]{ref}[/bold] [dim]{'  '.join(marks)}[/dim]")


@secrets_app.command("rm")
def secrets_rm(ref: str) -> None:
    """Forget a credential."""
    from .. import secrets as vault

    console.print("[green]removed[/green]" if vault.remove(ref) else f"[yellow]no such entry[/yellow] {ref}")


# --- connectors --------------------------------------------------------------


@connectors_app.command("list")
def connectors_list() -> None:
    """What the daemon is watching, and whether it is actually working."""
    from . import commands_connect

    run(commands_connect.render_list(get_config(), console))


@connectors_app.command("poll")
def connectors_poll(name: str) -> None:
    """Poll one connector now, in the foreground."""
    from . import commands_connect

    raise typer.Exit(run(commands_connect.poll_now(get_config(), name, console)))


@connectors_app.command("enable")
def connectors_enable(name: str) -> None:
    """Turn a connector back on after fixing whatever stopped it."""
    from . import commands_connect

    raise typer.Exit(run(commands_connect.set_enabled(get_config(), name, True, console)))


@connectors_app.command("disable")
def connectors_disable(name: str) -> None:
    """Stop a connector without editing config."""
    from . import commands_connect

    raise typer.Exit(run(commands_connect.set_enabled(get_config(), name, False, console)))


@connectors_app.command("auth")
def connectors_auth(
    label: str = typer.Argument(..., help="a key under [connectors.google.accounts]"),
    port: int = typer.Option(
        0, help="fixed loopback port, for `ssh -L` when the browser is on another machine"
    ),
) -> None:
    """Authorise one Google account. Opens a consent page; stores only a refresh token."""
    from . import commands_connect

    raise typer.Exit(run(commands_connect.google_auth(get_config(), label, console, port=port)))


@connectors_app.command("reset")
def connectors_reset(name: str) -> None:
    """Clear the cursor so the next poll re-reads everything."""
    from . import commands_connect

    raise typer.Exit(run(commands_connect.reset(get_config(), name, console)))


async def _telegram_check(cfg) -> int:
    """The body of `agent telegram check`, separated so it can be tested.

    A typer command that wraps `asyncio.run` cannot be called from a test that is already
    inside an event loop, and the thing worth testing here is the error handling rather
    than the decorator.
    """
    import httpx

    from ..daemon import telegram as tg

    if tg.token() is None:
        console.print("[yellow]no token:[/yellow] agent secrets set telegram/bot token")
        return 1
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            me = await tg.call(cfg, client, "getMe")
    except PermissionError:
        # The one failure that will never fix itself, so it gets a sentence rather than a
        # traceback: a rejected token needs a human and BotFather, not a retry.
        console.print(
            "[red]telegram rejected the token (401)[/red]\n"
            "It is revoked, mistyped, or from a different bot. In Telegram: @BotFather -> "
            "/mybots -> your bot -> API Token -> Revoke, then\n"
            "  agent secrets set telegram/bot token   [dim](reads stdin; nothing echoes)[/dim]"
        )
        return 1
    except Exception as exc:
        console.print(f"[red]could not reach telegram:[/red] {type(exc).__name__}: {exc}")
        return 1

    console.print(f"[green]ok[/green] @{me.get('username')} ({me.get('first_name')})")
    allowed = cfg.telegram.allowed_chat_ids
    console.print(
        f"allowed chat ids: {allowed}" if allowed
        else "[yellow]allowed_chat_ids is empty, so nobody can talk to it[/yellow]"
    )
    return 0


async def _telegram_whoami(cfg) -> int:
    """The body of `agent telegram whoami`. Reads pending updates without consuming them,
    so the daemon still sees the message."""
    import httpx

    from ..daemon import telegram as tg

    try:
        async with httpx.AsyncClient(timeout=20) as client:
            updates = await tg.call(cfg, client, "getUpdates", timeout=0)
    except PermissionError:
        console.print("[red]telegram rejected the token[/red] — run `agent telegram check`")
        return 1
    except Exception as exc:
        console.print(f"[red]could not reach telegram:[/red] {type(exc).__name__}")
        return 1

    seen: dict[int, str] = {}
    for update in updates:
        chat = ((update.get("message") or {}).get("chat")) or {}
        if chat.get("id") is not None:
            seen[int(chat["id"])] = str(chat.get("username") or chat.get("first_name") or "")
    if not seen:
        console.print("[dim]no recent messages — send the bot anything, then re-run[/dim]")
        return 1
    for chat_id, who in seen.items():
        marked = " [green](already allowed)[/green]" if chat_id in cfg.telegram.allowed_chat_ids else ""
        console.print(f"{chat_id}  {who}{marked}")
    console.print("\n[dim]add to config.toml: [telegram] allowed_chat_ids = [...][/dim]")
    return 0


@telegram_app.command("check")
def telegram_check() -> None:
    """Is the bot token good, and who is the bot?"""
    raise typer.Exit(run(_telegram_check(get_config())))


@telegram_app.command("whoami")
def telegram_whoami() -> None:
    """Print the chat id of whoever has messaged the bot, so you can allowlist yourself."""
    raise typer.Exit(run(_telegram_whoami(get_config())))


# --- backups -----------------------------------------------------------------


@app.command()
def backup(
    keep: int | None = typer.Option(None, help="How many backups to retain"),
    verify: bool = typer.Option(False, help="Restore into a scratch database and count rows"),
) -> None:
    """Snapshot the database and the memory repo."""
    from .. import backup as backup_mod

    cfg = get_config()
    result = run(backup_mod.create(cfg, keep=keep if keep is not None else cfg.db.backup_keep))
    console.print(
        f"[green]{result.path}[/green]  db {result.bytes_db / 1e6:.1f} MB"
        f"  repo {result.bytes_repo / 1e3:.0f} kB"
        + (f"  [dim]pruned {len(result.pruned)}[/dim]" if result.pruned else "")
    )
    if verify:
        counts = run(backup_mod.verify(result.path, cfg))
        console.print("[green]restore verified[/green] " + "  ".join(
            f"{k}={v}" for k, v in counts.items()
        ))


@app.command()
def restore(
    source: str | None = typer.Option(None, "--from", help="Backup directory (default: latest)"),
    into: str = typer.Option("agent_restored", help="Database to restore into"),
    force: bool = typer.Option(False, help="Allow restoring over the live database"),
) -> None:
    """Restore a backup. Defaults to a new database, not the live one."""
    from .. import backup as backup_mod

    cfg = get_config()
    path = Path(source).expanduser() if source else backup_mod.latest(cfg)
    if path is None:
        console.print("[red]no backups found[/red]")
        raise typer.Exit(1)

    live = cfg.db.dsn.rsplit("/", 1)[-1]
    if into == live and not force:
        console.print(
            f"[red]refusing to restore over the live database '{live}'[/red]\n"
            "Restore somewhere else, look at it, then point the DSN at it — or pass --force."
        )
        raise typer.Exit(1)

    name = run(backup_mod.restore(path, into=into, cfg=cfg, drop_existing=force))
    console.print(f"[green]restored[/green] {path} -> database '{name}'")
    bundle = path / backup_mod.BUNDLE
    if bundle.exists():
        console.print(
            f"[dim]memory repo: git clone {bundle} <dir>  (or: git -C <repo> pull {bundle})[/dim]"
        )


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
    from ..memory import review

    status, reason, _fact_id = await review.propose_and_review(
        statement=text, proposed_by="user", confidence=0.95,
        evidence=[{"source": "user"}],
    )
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


@memory_app.command("queue")
def memory_queue(
    process: bool = typer.Option(False, "--process", help="Adjudicate everything pending, now"),
) -> None:
    """How deep the review queue is, and optionally drain it.

    `process_pending` is otherwise reachable only from the daemon's consolidation loop, so a
    stopped daemon means proposed memories never become facts and nothing says so.
    """
    run(_memory_queue(process))


async def _memory_queue(process: bool) -> None:
    from ..db import repo_memory
    from ..ids import utcnow
    from ..memory import review as review_mod

    health = await repo_memory.queue_health()
    now = utcnow()

    def age(ts) -> str:
        if ts is None:
            return "[dim]—[/dim]"
        seconds = int((now - ts).total_seconds())
        if seconds < 90:
            return f"{seconds}s"
        if seconds < 5400:
            return f"{seconds // 60}m"
        return f"{seconds // 3600}h"

    table = Table(show_header=True, header_style="bold")
    table.add_column("metric")
    table.add_column("value")
    table.add_row("pending", str(health["pending"]))
    table.add_row("needs your review", str(health["needs_review"]))
    table.add_row("oldest pending", age(health["oldest_pending_at"]))
    table.add_row("oldest from you", age(health["oldest_user_at"]))
    table.add_row("rejected (7d)", str(health["rejected_7d"]))
    console.print(table)

    if not process:
        if health["pending"]:
            console.print("[dim]pass --process to adjudicate these now[/dim]")
        return
    stats = await review_mod.process_pending()
    console.print(
        f"[green]processed[/green] accepted={stats.accepted} merged={stats.merged} "
        f"superseded={stats.superseded} rejected={stats.rejected} "
        f"needs_review={stats.needs_review}"
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

    # candidate_memories has no embedding_model column, and rows already adjudicated are
    # frozen history -- only claims still awaiting a decision are worth the recompute.
    rows = await fetch_all(
        "SELECT id, statement AS text FROM candidate_memories "
        "WHERE status IN ('pending', 'needs_review')"
    )
    if rows:
        vectors = await embedder.embed([r["text"] for r in rows])
        async with connection() as conn:
            for row, vector in zip(rows, vectors, strict=True):
                await conn.execute(
                    "UPDATE candidate_memories SET embedding = %s WHERE id = %s",
                    (vector, row["id"]),
                )
        console.print(f"[green]candidate_memories[/green]: {len(rows)} re-embedded")


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


@loops_app.command("show")
def loops_show(loop_id: str) -> None:
    """Everything about one loop, including the detail no tool shows the model."""
    run(_loops_show(loop_id))


async def _loops_show(loop_id: str) -> None:
    from ..db.pool import fetch_one

    row = await fetch_one(
        "SELECT * FROM open_loops WHERE id::text LIKE %s ORDER BY created_at LIMIT 1",
        (loop_id + "%",),
    )
    if not row:
        console.print(f"[yellow]no loop matching[/yellow] {loop_id}")
        raise typer.Exit(1)
    console.print(f"[bold]{row['title']}[/bold]")
    console.print(f"[dim]{row['id']}  {row['status']}[/dim]")
    if row.get("due_at"):
        console.print(f"due {row['due_at']:%Y-%m-%d %H:%M}")
    if row.get("waiting_on"):
        console.print(f"waiting on {row['waiting_on']}")
    if row.get("detail"):
        # Deliberately only here. This is where a connector puts the other end's own words,
        # and no tool renders it to the model -- see tools/builtin_agenda.open_loops_list.
        console.print()
        console.print(row["detail"])
    if row.get("source_event_id"):
        console.print(f"[dim]from event {row['source_event_id']}[/dim]")


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
    """Thin wrapper. What approving *means* lives in policy/replay.py, because the Telegram
    channel decides the same thing and two definitions would eventually disagree."""
    from ..policy.replay import deny_approval, execute_approved

    try:
        target = UUID(approval_id)
    except ValueError:
        console.print("[red]not an approval id[/red]")
        raise typer.Exit(1) from None

    ok, message = await (
        execute_approved(target, origin="interactive", note=note)
        if approve
        else deny_approval(target, note)
    )
    console.print(message if ok else f"[yellow]{message}[/yellow]")


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


# --- the run journal ---------------------------------------------------------
#
# A subscriber in its own process, which is what makes "the frontend reads the journal" a
# claim about the feed rather than about one renderer: these commands hold no state but the
# last id they printed, and that is what `--since` takes back.


@journal_app.command("runs")
def journal_runs(limit: int = typer.Option(20, help="Most recent runs first.")) -> None:
    """Which runs are in the journal, and how much of each."""
    from ..journal.store import JournalStore, default_path

    cfg = get_config()
    with JournalStore(default_path(cfg)) as store:
        rows = list(reversed(store.runs()))[:limit]
        table = Table("run", "events", "last seq", "first", "last")
        for info in rows:
            table.add_row(
                info.run_id, str(info.events), str(info.last_seq),
                info.first_ts[:19], info.last_ts[:19],
            )
        console.print(table)
        console.print(f"[dim]{store.count()} events, last id {store.last_id()}[/dim]")


@journal_app.command("show")
def journal_show(
    run_id: str,
    after: int = typer.Option(0, help="Only events after this seq."),
) -> None:
    """One run as a sequence: what happened, in order, from the journal alone."""
    from ..journal.store import JournalStore, default_path

    cfg = get_config()
    with JournalStore(default_path(cfg)) as store:
        events = store.read(run_id, after_seq=after)
        if not events:
            # A turn id is what the rest of the CLI takes (`agent trace`, `agent why`), and
            # for a turn whose caller named its own run - the REPL and Telegram both do, so
            # that they can subscribe before the turn opens - it is not the run id. Rather
            # than make somebody grep for it, resolve it.
            resolved = next(
                (
                    e.run_id
                    for e in store.read_all()
                    if e.type == "agent_started" and e.payload.get("turn_id") == run_id
                ),
                None,
            )
            if resolved is None:
                console.print(f"[yellow]no events for run or turn {run_id}[/yellow]")
                raise typer.Exit(1)
            console.print(f"[dim]turn {run_id} ran in run {resolved}[/dim]")
            events = store.read(resolved, after_seq=after)
        for event in events:
            console.print(_journal_line(event))


@journal_app.command("follow")
def journal_follow(
    run_id: str | None = typer.Option(None, "--run", help="Only this run."),
    since: int | None = typer.Option(
        None, help="Resume after this event id. Omit to start at the end of the file."
    ),
    replay: bool = typer.Option(False, help="Start from the first event ever written."),
) -> None:
    """Subscribe to the journal and print events as they land.

    The id in each line is the cursor: pass the last one you saw back as `--since` and the
    feed resumes with no gap and no duplicate. That is the whole reconnection mechanism -
    there is no session, no queue and nothing buffered on the writer's side to lose.
    """
    from ..journal.feed import JournalTail
    from ..journal.store import default_path

    cfg = get_config()
    start = 0 if replay else since
    tail = JournalTail.attach(default_path(cfg), run_id=run_id, since=start or 0)
    if start is None:
        tail.last_id = tail.store.last_id()
    console.print(f"[dim]following {tail.store.path} from id {tail.last_id}[/dim]")

    async def _follow() -> None:
        async for event in tail.follow():
            console.print(_journal_line(event))

    try:
        run(_follow())
    except KeyboardInterrupt:
        pass
    finally:
        console.print(f"[dim]last event id: {tail.last_id}[/dim]")


@journal_app.command("checkpoints")
def journal_checkpoints(run_id: str) -> None:
    """The snapshots taken of one run, oldest first."""
    from ..journal.checkpoints import Checkpointer
    from ..journal.writer import JournalWriter

    cfg = get_config()
    writer = JournalWriter.open(cfg)
    try:
        rows = Checkpointer(writer, cfg=cfg).entries(run_id)
    finally:
        writer.close()
    if not rows:
        console.print(f"[yellow]no checkpoints for run {run_id}[/yellow]")
        if not cfg.checkpoints.enabled:
            console.print("[dim][checkpoints] enabled is false, so none are written[/dim]")
        raise typer.Exit(1)
    table = Table("checkpoint", "trigger", "covers seq", "event seq", "open effects", "at")
    for cp in rows:
        table.add_row(
            cp.checkpoint_id, cp.trigger, str(cp.covers_seq), str(cp.event_seq),
            str(len(cp.effects_cursor.open_keys)), cp.created_at[:19],
        )
    console.print(table)


@journal_app.command("mark")
def journal_mark(run_id: str) -> None:
    """Write a checkpoint of a run now: the `manual` boundary, asked for by a person."""
    from ..journal.checkpoints import Checkpointer, CheckpointError, enabled
    from ..journal.writer import JournalWriter

    cfg = get_config()
    if not enabled(cfg):
        console.print("[yellow][checkpoints] enabled is false; nothing was written[/yellow]")
        raise typer.Exit(1)
    writer = JournalWriter.open(cfg)
    try:
        cp = Checkpointer(writer, cfg=cfg).write(run_id, trigger="manual")
    except CheckpointError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc
    finally:
        writer.close()
    console.print(
        f"checkpoint [bold]{cp.checkpoint_id}[/bold] covers run {cp.run_id} "
        f"through seq {cp.covers_seq}"
    )


@journal_app.command("resume")
def journal_resume(
    run_id: str,
    apply: bool = typer.Option(
        False, "--apply", help="Reconcile and announce. Without it, this only reports."
    ),
    reason: str = typer.Option("manual", help="Why the run is being picked up. Journaled."),
    notice: bool = typer.Option(
        False, "--notice", help="Also print the block a resumed orchestrator would be given."
    ),
) -> None:
    """Fold a run back out of the journal and say what the crash left open.

    Reports by default and writes only with `--apply`, because reconciliation moves ledger
    rows and appends to the run, and the person asking "what happened to that run" has not
    yet agreed to either.

    The counts and the checkpoint line are a report. The part a person is asked to act on
    comes from `agent/observations.py`, which is the one place that sentence is written:
    session 4b left it unphrased precisely so the CLI and the resumed turn could not drift
    into asking two different questions about the same call.
    """
    from ..agent import observations as obs
    from ..journal import resume as resume_mod
    from ..journal.writer import JournalWriter

    cfg = get_config()
    writer = JournalWriter.open(cfg)
    try:
        if apply:
            done = resume_mod.resume(run_id, writer=writer, reason=reason)
            plan, applied = done.plan, done.applied
        else:
            writer.flush()
            plan, applied = resume_mod.plan(run_id, store=writer.store), False
    except resume_mod.ResumeError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc
    finally:
        writer.close()

    console.print(resume_mod.summary(plan))
    checkpoint = plan.checkpoint
    console.print(
        f"[dim]checkpoint: {checkpoint.trigger} @ seq {checkpoint.covers_seq}[/dim]"
        if checkpoint
        else "[dim]no checkpoint behind this run; folded from the first event[/dim]"
    )
    missing = len(plan.rehydration.truncated)
    if missing:
        console.print(
            f"[dim]{missing} of {len(plan.rehydration.messages)} messages are held as "
            f"previews; the bodies are in the archive[/dim]"
        )
    question = obs.prompt(plan)
    if question:
        # `markup=False` because the arguments are in this text and a fetched URL or a
        # filename is free to contain a square bracket. Rich would read that as a style
        # tag: at best the line loses characters, at worst a value forges a colour the
        # runtime never chose. The URL has to arrive exactly as it was called.
        console.print()
        console.print(question, markup=False, highlight=False)
        console.print()
    if notice:
        # The same observations, addressed to the model instead of the person. Reachable by
        # hand for the same reason `agent journal mark` is: nothing produces a resumed turn
        # until Pass 5, and a block nobody can read is a block nobody checks.
        block = obs.notice(plan)
        console.print(block or "[dim]nothing interrupted; the notice would be empty[/dim]",
                      markup=not block, highlight=False)
    if applied:
        console.print(f"resumed at seq {plan.from_seq}; {len(plan.reconciliation.orphans)} closed")
    elif plan.needs_resume:
        console.print("[dim]nothing written; pass --apply to reconcile and announce[/dim]")
    else:
        console.print("[dim]nothing to resume[/dim]")


@journal_app.command("continue")
def journal_continue(
    run_id: str,
    message: str = typer.Argument(..., help="What to say to the orchestrator that picks it up."),
    apply: bool = typer.Option(
        False, "--apply", help="Actually take the turn. Without it, this only reports the plan."
    ),
    allow_rerun: list[str] = typer.Option(
        [],
        "--allow-rerun",
        help=(
            "Name a tool whose interrupted calls in this run may be run again. This is you "
            "saying the duplicate is acceptable; the runtime refuses otherwise."
        ),
    ),
    autonomy: str = typer.Option("assist", help="observe | assist | act"),
) -> None:
    """Pick a dead run up and take the next turn of it.

    The continuation path session 4b named and did not build. It decides between the two
    ways a run can come back - replaying the conversation when it is recent and still fits,
    compressing it into a handoff when it is not - and reports which it took and why before
    it does anything.

    `--allow-rerun` is Dylan's requirement B from the other side. A crash can leave an
    `unsafe_write` announced and never answered, which means it *may* have gone out; the
    executor refuses an identical call in that run rather than trusting a 27B to obey a
    sentence in its prompt. Naming the tool here is the only way past, and it is a person
    typing it, which is what "unless the user has said to run it again" means.
    """
    from ..agent import rehydrate
    from ..agent.loop import AgentLoop, Session
    from ..agent.stream import Answer, Delta
    from ..journal import resume as resume_mod
    from ..journal.writer import JournalWriter
    from ..policy.approvals import QueueApprover, RerunGrants

    cfg = get_config()

    async def go() -> None:
        writer = JournalWriter.open(cfg)
        try:
            if apply:
                resume_mod.resume(run_id, writer=writer, reason="continue")
            else:
                writer.flush()
            plan = await rehydrate.restart(run_id, store=writer.store, cfg=cfg)
        finally:
            writer.close()

        console.print(resume_mod.summary(plan.plan))
        age = "unknown" if plan.age_s is None else f"{plan.age_s / 3600:.1f}h"
        console.print(
            f"[bold]{plan.path}[/bold]"
            + (f" ({plan.reason})" if plan.reason else "")
            + f" - last activity {age} ago, conversation "
            f"{plan.source.get('conversation_tokens', '?')} of {plan.budget_tokens} "
            "estimated tokens"
        )
        if plan.handoff is not None:
            console.print(
                f"[dim]handoff: {plan.handoff_source}, "
                f"{len(plan.handoff.dropped_manifest)} manifest items"
                + (f", generated in {plan.generated_ms} ms" if plan.generated_ms else "")
                + "[/dim]"
            )
        elif not plan.lossless:
            console.print("[yellow]no handoff and no replay: see the record below[/yellow]")
            console.print(plan.source, markup=False, highlight=False)
        if plan.closing:
            console.print(
                f"[dim]{len(plan.closing)} interrupted call(s) will be closed out to the "
                "model as unresolved[/dim]"
            )
        if not apply:
            console.print("[dim]nothing written; pass --apply to take the turn[/dim]")
            return
        if plan.session_id is None:
            console.print("[red]this run has no session, so there is no turn to take[/red]")
            raise typer.Exit(1)

        grants = RerunGrants()
        for tool in allow_rerun:
            grants.allow_tool(run_id=run_id, tool=tool)

        session = await Session.resume(plan.session_id, autonomy=autonomy, cfg=cfg)
        # The handoff this continuation decided on, not whatever the session last stored.
        # On the lossless path that is None, and None is the instruction: replay.
        session.handoff = plan.handoff
        # The same approver `agent ask` uses, and for the same reason: this is one turn
        # from a command line, not a REPL, so there is no prompt loop to ask into. A write
        # that needs approval is queued and the model is told so, which it can act on.
        loop = AgentLoop(cfg=cfg, approver=QueueApprover(origin="interactive"))
        loop.executor.rerun_grants = grants
        console.print()
        async for event in loop.run_turn(session, message, origin="resume", run_id=run_id):
            if isinstance(event, Delta) and not event.thinking:
                console.print(event.text, end="", markup=False, highlight=False)
            elif isinstance(event, Answer):
                console.print()

    run(go())


@journal_app.command("fork")
def journal_fork(
    run_id: str,
    at: int = typer.Option(..., "--at", help="The seq to rewind to. Nothing after it is lost."),
    apply: bool = typer.Option(
        False, "--apply", help="Open the new run. Without it, this only reports."
    ),
    reason: str = typer.Option("manual", help="Why the run is being forked. Journaled."),
    as_run: str = typer.Option(
        "", "--as", help="The new run's id. Minted when not given; must not already exist."
    ),
) -> None:
    """Rewind a conversation to a position, into a new run, undoing nothing.

    The run being forked is not touched: no event is written into it, no ledger row moves,
    nothing is deleted. What it did after that point stands, and the disclosure says what
    that was - automatic reversal, file rollback included, is this pass's *Must not*, and a
    fork that quietly restored a conversation the disk disagrees with is the dishonest
    version of this command.

    Reports by default and writes only with `--apply`, for the same reason
    `agent journal resume` does: opening a run is a thing the person asking "what would this
    lose" has not agreed to.
    """
    from ..agent import observations as obs
    from ..journal import fork as fork_mod
    from ..journal import resume as resume_mod
    from ..journal.writer import JournalWriter

    cfg = get_config()
    writer = JournalWriter.open(cfg)
    child: str | None = None
    try:
        if apply:
            done = fork_mod.fork(
                run_id, at_seq=at, writer=writer, reason=reason,
                new_run_id=as_run or None,
            )
            plan, child = done.plan, done.run_id
        else:
            writer.flush()
            plan = fork_mod.plan_fork(run_id, at_seq=at, store=writer.store)
    except (fork_mod.ForkError, resume_mod.ResumeError) as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc
    finally:
        writer.close()

    console.print(fork_mod.summary(plan))
    checkpoint = plan.checkpoint
    console.print(
        f"[dim]checkpoint: {checkpoint.trigger} @ seq {checkpoint.covers_seq}[/dim]"
        if checkpoint
        else "[dim]no checkpoint at or before the fork point; folded from the first event[/dim]"
    )
    if plan.open_at_fork:
        # Interrupted at the fork point itself, so they belong to the parent's reconciliation
        # and not to the child. Named here rather than folded into the disclosure: the
        # disclosure is about what the rewound part did, and these are calls the rewind is
        # not even past.
        console.print(
            f"[yellow]{len(plan.open_at_fork)} call(s) were still open at seq {at}; "
            f"`agent journal resume {run_id}` is what reconciles those[/yellow]"
        )
    # `markup=False` for session 4c's reason: the arguments are in this text, and a path or
    # a URL containing a square bracket would otherwise be read by rich as a style tag.
    console.print()
    console.print(obs.disclosure(plan), markup=False, highlight=False)
    console.print()
    if child:
        console.print(f"forked into run [bold]{child}[/bold] at seq {plan.forked_from_seq}")
    else:
        console.print("[dim]nothing written; pass --apply to open the new run[/dim]")


def _journal_line(event) -> str:
    """One event as one line. The id first, because it is what a reconnect needs."""
    from ..journal.render import render_event

    rendered = render_event(event)
    worker = event.payload.get("worker_id")
    tail = f"  {rendered.text}" if rendered else ""
    return (
        f"[dim]{event.id:>6}[/dim] [dim]{event.ts[11:19]}[/dim] "
        f"{'w' if worker else ' '} seq {event.seq:>3} [bold]{event.type}[/bold]{tail}"
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


@policy_app.command("diff")
def policy_diff() -> None:
    """Show how your policy file differs from the one this version ships.

    Deliberately not a `sync --force`: silently overwriting a security file is exactly what
    you do not want. Read the diff, then copy it across yourself if you agree with it.
    """
    import difflib

    from ..config import DEFAULT_POLICY

    cfg = get_config()
    if cfg.policy_file == DEFAULT_POLICY:
        console.print("[dim]using the shipped policy directly[/dim]")
        return
    if not cfg.policy_file.exists():
        console.print(f"[yellow]no policy file at[/yellow] {cfg.policy_file}")
        raise typer.Exit(1)

    lines = list(
        difflib.unified_diff(
            DEFAULT_POLICY.read_text().splitlines(keepends=True),
            cfg.policy_file.read_text().splitlines(keepends=True),
            fromfile=f"shipped ({DEFAULT_POLICY})",
            tofile=f"yours ({cfg.policy_file})",
        )
    )
    if not lines:
        console.print("[green]identical[/green] to the shipped policy")
        return
    for line in lines:
        colour = "green" if line.startswith("+") else "red" if line.startswith("-") else "dim"
        console.print(f"[{colour}]{line.rstrip()}[/{colour}]")


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
