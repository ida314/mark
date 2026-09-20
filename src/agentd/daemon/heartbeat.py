"""The periodic 'is there anything useful to do?' tick.

The expensive part is guarded: build a deterministic situation report first, and only
spend an LLM turn when something actually changed and is actionable.
"""

from __future__ import annotations

import asyncio
import hashlib
from datetime import datetime

from ..config import Config
from ..db import repo_agenda, repo_archive, repo_ops
from ..obs import otel

HEARTBEAT_PROMPT = """Here is the current situation report.

{report}

Is there anything genuinely useful to do right now, on the user's behalf, without
interrupting them for something trivial?

You may: notify the user, note a goal or open loop, or queue an action for approval.
You may not send anything outside this machine.

If nothing is worth doing, reply with exactly NOTHING."""


def in_quiet_hours(now: datetime, quiet: tuple[int, int]) -> bool:
    start, end = quiet
    hour = now.astimezone().hour
    return hour >= start or hour < end if start > end else start <= hour < end


async def situation_report() -> tuple[str, bool]:
    """Deterministic summary of what needs attention. Returns (text, actionable)."""
    lines: list[str] = []
    actionable = False

    overdue = await repo_agenda.overdue_loops()
    if overdue:
        actionable = True
        lines.append("Overdue open loops:")
        lines.extend(f"- {r['title']} (due {r['due_at']:%Y-%m-%d})" for r in overdue[:10])

    goals = await repo_agenda.goals_due_for_review()
    if goals:
        actionable = True
        lines.append("Goals due for review:")
        lines.extend(f"- {g['title']} (next: {g.get('next_step') or 'unset'})" for g in goals[:10])

    approvals = await repo_ops.list_approvals("pending", limit=10)
    if approvals:
        actionable = True
        lines.append("Actions waiting for the user's approval:")
        lines.extend(f"- {a['tool_name']} ({a['id']})" for a in approvals)

    unread = await repo_agenda.list_notifications(unread_only=True, limit=5)
    if unread:
        lines.append(f"{len(unread)} unread notifications.")

    # Calendar events are archived rather than turned into open loops, so this is the only
    # place they surface. Counts and clock times only: an event summary was written by
    # whoever sent the invitation, and this string goes straight into an LLM prompt below.
    events = await repo_archive.upcoming_events("gcal-%", hours=24)
    if events:
        first = events[0]["occurred_at"].astimezone()
        lines.append(
            f"{len(events)} calendar events in the next 24h; the next starts "
            f"{first:%H:%M}."
        )

    from ..db import repo_memory
    from ..ids import utcnow

    health = await repo_memory.queue_health()
    if health["needs_review"]:
        lines.append(f"{health['needs_review']} memories need your review.")
    # A correction the user typed is not a backlog item; it is a belief they think the agent
    # already holds. Say so loudly enough to be actionable.
    if health["oldest_user_at"]:
        waited = int((utcnow() - health["oldest_user_at"]).total_seconds() // 60)
        actionable = True
        lines.append(
            f"A memory you corrected {waited}m ago is still unadjudicated "
            f"({health['pending']} pending in total)."
        )

    return ("\n".join(lines) or "Nothing pending."), actionable


async def heartbeat_loop(cfg: Config, stop: asyncio.Event) -> None:
    from ..ids import utcnow

    last_hash = ""
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=cfg.daemon.heartbeat_interval_s)
            return
        except TimeoutError:
            pass
        try:
            now = utcnow()
            if in_quiet_hours(now, tuple(cfg.daemon.quiet_hours)):
                continue
            report, actionable = await situation_report()
            digest = hashlib.sha256(report.encode()).hexdigest()
            if not actionable or digest == last_hash:
                continue  # nothing new: no model call at all
            last_hash = digest

            from ..agent.loop import AgentLoop, Session
            from ..agent.stream import Answer
            from ..policy.approvals import QueueApprover

            with otel.span("daemon.heartbeat"):
                session = await Session.create(channel="daemon", autonomy="observe")
                loop = AgentLoop(
                    cfg=cfg.model_copy(
                        update={"agent": cfg.agent.model_copy(update={"max_steps": 4})}
                    ),
                    approver=QueueApprover(origin="daemon"),
                    actor="daemon:heartbeat",
                )
                async for event in loop.run_turn(
                    session,
                    HEARTBEAT_PROMPT.format(report=report),
                    origin="daemon",
                    autonomy="observe",
                ):
                    if isinstance(event, Answer) and event.text.strip() not in ("", "NOTHING"):
                        await repo_agenda.notify(
                            source="heartbeat", title="Suggestion", body=event.text[:2000]
                        )
        except Exception as exc:
            await repo_agenda.notify(
                source="daemon", level="error", title="Heartbeat failed", body=str(exc)[:500]
            )
