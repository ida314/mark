"""Watchers: timers, intervals, cron. Firing one may notify, or wake a whole agent turn."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from typing import Any

from ..config import Config, get_config
from ..db import repo_agenda, repo_ops
from ..db.repo_archive import RawEvent, append_event
from ..db.repo_ops import ActionRecord
from ..ids import parse_when, utcnow
from ..obs import otel


def next_fire(kind: str, spec: dict[str, Any], now: datetime | None = None) -> datetime | None:
    """When should this watcher fire next? None means never again."""
    now = now or utcnow()
    if kind == "once":
        when = parse_when(str(spec.get("at", ""))) if spec.get("at") else None
        return when if when and when > now else (None if when else now + timedelta(minutes=1))
    if kind == "interval":
        every = float(spec.get("every_s", 3600))
        return now + timedelta(seconds=max(30.0, every))
    if kind == "cron":
        try:
            from croniter import croniter

            base = croniter(spec.get("cron", "0 * * * *"), now)
            return base.get_next(datetime)
        except Exception:
            return now + timedelta(hours=1)
    return None  # file watchers are event-driven, not scheduled


async def fire_watcher(watcher: dict, cfg: Config | None = None) -> str:
    """Run one watcher's action. Returns a short outcome string."""
    cfg = cfg or get_config()
    action = watcher["action"] or {}
    kind = action.get("type", "notify")
    with otel.span("watcher.fire", {"watcher.name": watcher["name"], "watcher.kind": kind}):
        outcome = "notified"
        if kind == "notify":
            await repo_agenda.notify(
                source=f"watcher:{watcher['name']}",
                title=action.get("text", watcher["name"]),
                ref={"watcher": str(watcher["id"])},
            )
        elif kind == "agent":
            from ..agent.loop import AgentLoop, Session
            from ..policy.approvals import QueueApprover

            session = await Session.create(channel="daemon", autonomy=watcher["autonomy"])
            loop = AgentLoop(cfg=cfg, approver=QueueApprover(origin="daemon"), actor="daemon")
            text = ""
            async for event in loop.run_turn(
                session,
                action.get("prompt", ""),
                origin="daemon",
                autonomy=watcher["autonomy"],
            ):
                from ..agent.stream import Answer

                if isinstance(event, Answer):
                    text = event.text
            if text:
                await repo_agenda.notify(
                    source=f"watcher:{watcher['name']}", title=watcher["name"], body=text[:2000],
                    ref={"watcher": str(watcher["id"]), "session": str(session.id)},
                )
            outcome = "agent turn completed"
        await append_event(
            RawEvent(
                kind="watcher_fired", actor="daemon", content=watcher["name"],
                payload={"watcher": str(watcher["id"]), "action": kind},
            )
        )
        await repo_ops.write_action(
            ActionRecord(
                actor="daemon", kind="watcher_fire", name=watcher["name"], status="ok",
                rationale=f"watcher {watcher['id']} was due", output={"outcome": outcome},
                **otel.current_ids(),
            )
        )
    return outcome


async def scheduler_loop(cfg: Config, stop: asyncio.Event) -> None:
    """Poll for due watchers; NOTIFY wakes us early when one is added."""
    while not stop.is_set():
        try:
            for watcher in await repo_agenda.claim_due_watchers():
                try:
                    await fire_watcher(watcher, cfg)
                except Exception as exc:
                    await repo_agenda.notify(
                        source="daemon", level="error",
                        title=f"Watcher '{watcher['name']}' failed", body=str(exc)[:500],
                    )
                finally:
                    await repo_agenda.reschedule_watcher(
                        watcher["id"], next_fire(watcher["kind"], watcher["spec"] or {})
                    )
        except Exception:
            await asyncio.sleep(5)
        try:
            await asyncio.wait_for(stop.wait(), timeout=cfg.daemon.scheduler_poll_s)
        except TimeoutError:
            pass
