"""The REPL: streaming answers, inline approvals, notifications that arrive mid-conversation."""

from __future__ import annotations

import asyncio
import contextlib
from uuid import UUID

from prompt_toolkit import PromptSession
from prompt_toolkit.patch_stdout import patch_stdout
from rich.console import Console
from rich.panel import Panel
from rich.syntax import Syntax

from ..agent.events import (
    Notice,
    SubagentFinished,
    SubagentStarted,
    TextChunk,
    ThinkingChunk,
    ToolFinished,
    ToolStarted,
    TurnFinished,
)
from ..agent.loop import AgentLoop, Session
from ..config import Config
from ..db import repo_agenda, repo_archive
from ..db.pool import listen
from ..obs import otel
from ..policy.approvals import ApprovalRequest, ApprovalResult, SessionGrants

console = Console()

HELP = """
/autonomy [observe|assist|act]   show or change what may happen without asking
/tools                           tools currently available
/context                         what memory was retrieved for the last turn
/remember TEXT                   propose a durable memory
/inbox                           unread notifications
/approvals                       pending approvals
/trace                           trace of the last turn
/new                             start a fresh session
/help, /exit
"""


class CliApprover:
    """Ask the human in front of us, with a preview of exactly what will happen."""

    def __init__(self, grants: SessionGrants, session: PromptSession) -> None:
        self.grants = grants
        self.prompt_session = session

    async def request(self, req: ApprovalRequest) -> ApprovalResult:
        body = [f"[bold]{req.tool_name}[/bold]  risk={req.risk}  rule={req.decision.rule_id}"]
        if req.reason:
            body.append(f"\n[italic]Why:[/italic] {req.reason}")
        if req.decision.reason:
            body.append(f"[dim]{req.decision.reason}[/dim]")
        args = {k: v for k, v in req.args.items() if k != "content"}
        body.append(f"\n[dim]{args}[/dim]")
        console.print(Panel("\n".join(body), title="Approval needed", border_style="yellow"))
        if req.preview:
            console.print(
                Syntax(
                    req.preview[:2000],
                    "diff" if req.preview.startswith(("---", "+++", "@@", "-", "+")) else "bash",
                    theme="ansi_dark",
                    word_wrap=True,
                )
            )

        answer = (
            await self.prompt_session.prompt_async("approve? [y]es / [n]o / [s]ession: ")
        ).strip().lower()
        if answer.startswith("s"):
            self.grants.grant(req.tool_name, req.args)
            return ApprovalResult(approved=True, decided_by="user")
        if answer.startswith("y"):
            return ApprovalResult(approved=True, decided_by="user")
        note = (await self.prompt_session.prompt_async("reason (optional): ")).strip() or None
        return ApprovalResult(approved=False, note=note, decided_by="user")


async def _inbox_watcher(stop: asyncio.Event) -> None:
    """Print notifications above the prompt as they arrive."""
    try:
        async for payload in listen("agent_inbox"):
            if stop.is_set():
                return
            with contextlib.suppress(Exception):
                row = await repo_agenda.get_notification(int(payload))
                if row:
                    style = {"info": "cyan", "warn": "yellow", "error": "red"}.get(
                        row["level"], "cyan"
                    )
                    console.print(
                        f"\n[{style}]● {row['title']}[/{style}]"
                        + (f"\n  {row['body'][:400]}" if row.get("body") else "")
                    )
    except asyncio.CancelledError:
        raise
    except Exception:
        return


async def run_chat(cfg: Config, *, autonomy: str, resume: str | None, show_thinking: bool) -> None:
    otel.setup("agent-cli", cfg)
    cfg.ensure_dirs()

    if resume:
        session = await Session.resume(UUID(resume), autonomy=autonomy)
        console.print(f"[dim]Resumed session {session.id}[/dim]")
    else:
        session = await Session.create(channel="cli", autonomy=autonomy)

    prompt_session: PromptSession = PromptSession()
    grants = SessionGrants()
    approver = CliApprover(grants, prompt_session)
    loop = AgentLoop(cfg=cfg, approver=approver)
    loop.executor.grants = grants

    stop = asyncio.Event()
    inbox_task = asyncio.create_task(_inbox_watcher(stop))

    console.print(
        f"[bold]agent[/bold]  session {str(session.id)[:8]}  autonomy=[cyan]{autonomy}[/cyan]"
        "   /help for commands"
    )
    last_turn_id: str | None = None

    try:
        while True:
            try:
                with patch_stdout():
                    user_input = await prompt_session.prompt_async("\n› ")
            except (EOFError, KeyboardInterrupt):
                break
            text = user_input.strip()
            if not text:
                continue

            if text.startswith("/"):
                if text in ("/exit", "/quit"):
                    break
                handled, autonomy = await _slash(
                    text, session, loop, cfg, autonomy, last_turn_id
                )
                if handled:
                    continue

            console.print()
            streaming = False
            async for event in loop.run_turn(session, text, autonomy=autonomy):
                if isinstance(event, TextChunk):
                    console.print(event.text, end="", markup=False, highlight=False)
                    streaming = True
                elif isinstance(event, ThinkingChunk) and show_thinking:
                    console.print(f"[dim]{event.text}[/dim]", end="", markup=True)
                elif isinstance(event, ToolStarted):
                    if streaming:
                        console.print()
                        streaming = False
                    console.print(f"[dim]→ {event.name}({_brief(event.args)})[/dim]")
                elif isinstance(event, ToolFinished):
                    mark = "✗" if event.denied else ("✓" if event.ok else "!")
                    color = "yellow" if event.denied else ("green" if event.ok else "red")
                    console.print(f"[{color}]  {mark} {event.summary}[/{color}]")
                elif isinstance(event, SubagentStarted):
                    console.print(f"[magenta]→ delegating to {event.name}[/magenta]")
                elif isinstance(event, SubagentFinished):
                    console.print(f"[magenta]  {event.name}: {event.status}[/magenta]")
                elif isinstance(event, Notice):
                    console.print(f"[yellow]{event.text}[/yellow]")
                elif isinstance(event, TurnFinished):
                    last_turn_id = event.turn_id
                    console.print()
    finally:
        stop.set()
        inbox_task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await inbox_task
        await repo_archive.end_session(session.id)
        console.print(
            f"\n[dim]Session {session.id} saved. It will be consolidated into memory shortly "
            f"(or run: agent consolidate --session {session.id}).[/dim]"
        )


async def _slash(
    text: str, session: Session, loop: AgentLoop, cfg: Config, autonomy: str,
    last_turn_id: str | None,
) -> tuple[bool, str]:
    """Returns (handled, autonomy)."""
    command, _, rest = text.partition(" ")
    rest = rest.strip()

    if command == "/help":
        console.print(HELP, markup=False)  # the option lists contain [brackets]
    elif command == "/autonomy":
        if rest in ("observe", "assist", "act"):
            autonomy = rest
            session.autonomy = rest
            console.print(f"[cyan]autonomy = {rest}[/cyan]")
        else:
            console.print(f"autonomy = {autonomy}  (observe | assist | act)")
    elif command == "/tools":
        for tool in sorted(loop.registry.enabled(), key=lambda t: t.name):
            console.print(f"  [bold]{tool.name}[/bold] [dim]{tool.risk}[/dim] {tool.description}")
    elif command == "/context":
        pack = loop.last_pack
        console.print(pack.text if pack and pack.text else "[dim]nothing retrieved yet[/dim]")
        if pack:
            console.print(f"[dim]{pack.stats}[/dim]")
    elif command == "/remember":
        from ..db import repo_memory
        from ..memory import review

        candidate_id = await repo_memory.insert_candidate(
            statement=rest, proposed_by="user", confidence=0.95,
            structured={"category": "other"}, evidence=[{"source": "user"}],
            session_id=session.id,
        )
        rows = await repo_memory.pending_candidates()
        for row in rows:
            if row["id"] == candidate_id:
                status, reason = await review.process_candidate(row, cfg)
                console.print(f"[green]{status}[/green]: {reason}")
    elif command == "/inbox":
        rows = await repo_agenda.list_notifications(unread_only=True)
        if not rows:
            console.print("[dim]inbox empty[/dim]")
        for row in rows:
            console.print(f"  ● {row['title']}  [dim]{row['created_at']:%H:%M}[/dim]")
        await repo_agenda.mark_read()
    elif command == "/approvals":
        from ..db import repo_ops

        rows = await repo_ops.list_approvals("pending")
        if not rows:
            console.print("[dim]nothing pending[/dim]")
        for row in rows:
            console.print(f"  {row['id']}  {row['tool_name']}  [dim]{row['created_at']:%H:%M}[/dim]")
    elif command == "/trace":
        from .commands_trace import render_trace

        if last_turn_id:
            await render_trace(last_turn_id, console)
        else:
            console.print("[dim]no turn yet[/dim]")
    elif command == "/new":
        console.print("[dim]start a new session with: agent chat[/dim]")
    else:
        return False, autonomy
    return True, autonomy


def _brief(args: dict) -> str:
    parts = []
    for key, value in args.items():
        if key == "reason":
            continue
        text = str(value)
        parts.append(f"{key}={text[:60]}{'…' if len(text) > 60 else ''}")
    return ", ".join(parts)[:120]
