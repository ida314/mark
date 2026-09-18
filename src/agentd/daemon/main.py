"""The always-on half: infrastructure stays up, the model only wakes when something happens."""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import socket
from datetime import datetime
from datetime import time as dtime
from functools import partial

import psycopg

from ..config import Config, get_config
from ..connectors import all_connectors
from ..connectors.base import run_connector
from ..db import repo_agenda, repo_ops
from ..db.pool import close_pool, connection
from ..ids import utcnow
from ..obs import otel
from .heartbeat import heartbeat_loop
from .notifier import notifier_loop
from .scheduler import scheduler_loop

DAEMON_LOCK_KEY = 0x4147_4544  # "AGED"


async def _acquire_singleton(conn: psycopg.AsyncConnection) -> bool:
    cur = await conn.execute("SELECT pg_try_advisory_lock(%s) AS ok", (DAEMON_LOCK_KEY,))
    row = await cur.fetchone()
    return bool(row["ok"])


async def filewatch_loop(cfg: Config, stop: asyncio.Event) -> None:
    """React to file changes: user edits in the memory repo, plus any file watchers."""
    from watchfiles import awatch

    from ..db.repo_archive import RawEvent, append_event
    from ..memory.consolidate import sync_user_edits
    from .scheduler import fire_watcher

    while not stop.is_set():
        watchers = await repo_agenda.file_watchers()
        paths: dict[str, dict] = {}
        for watcher in watchers:
            for path in (watcher["spec"] or {}).get("paths", []):
                paths[path] = watcher
        watch_paths = [p for p in paths if os.path.exists(p)]
        memory_repo = str(cfg.paths.memory_repo)
        if os.path.exists(memory_repo):
            watch_paths.append(memory_repo)
        if not watch_paths:
            try:
                await asyncio.wait_for(stop.wait(), timeout=60)
            except TimeoutError:
                pass
            continue

        try:
            async for changes in awatch(*watch_paths, stop_event=stop, debounce=2000):
                touched = {str(path) for _change, path in changes}
                if any(path.startswith(memory_repo) for path in touched):
                    if not any("/.git/" in path or path.endswith(".lock") for path in touched):
                        await sync_user_edits(cfg)
                for prefix, watcher in paths.items():
                    if any(path.startswith(prefix) for path in touched):
                        await append_event(
                            RawEvent(
                                kind="file_change", actor="daemon",
                                content=", ".join(sorted(touched)[:20]),
                                payload={"watcher": str(watcher["id"])},
                            )
                        )
                        await fire_watcher(watcher, cfg)
        except Exception:
            await asyncio.sleep(5)


async def consolidation_loop(cfg: Config, stop: asyncio.Event) -> None:
    from ..memory.consolidate import maybe_consolidate_idle, nightly

    hour, _, minute = cfg.daemon.nightly_at.partition(":")
    nightly_at = dtime(int(hour), int(minute or 0))
    last_nightly: datetime | None = None

    while not stop.is_set():
        try:
            await maybe_consolidate_idle(cfg)
            now = utcnow().astimezone()
            if (
                now.time() >= nightly_at
                and (last_nightly is None or last_nightly.date() < now.date())
            ):
                last_nightly = now
                await nightly(cfg)
                await _nightly_backup(cfg)
        except Exception as exc:
            await repo_agenda.notify(
                source="daemon", level="error", title="Consolidation failed", body=str(exc)[:500]
            )
        try:
            await asyncio.wait_for(stop.wait(), timeout=300)
        except TimeoutError:
            pass


async def _nightly_backup(cfg: Config) -> None:
    """A failed backup is worth waking someone for; it is the one job whose whole point is
    that you find out before you need it."""
    from ..backup import create

    try:
        result = await create(cfg, keep=cfg.db.backup_keep)
    except Exception as exc:
        await repo_agenda.notify(
            source="daemon", level="error", title="Backup failed", body=str(exc)[:500]
        )
        return
    await repo_ops.write_action(
        repo_ops.ActionRecord(
            actor="daemon", kind="backup", name=result.path.name, status="ok",
            output={"db_bytes": result.bytes_db, "repo_bytes": result.bytes_repo,
                    "pruned": result.pruned},
        )
    )


async def housekeeping_loop(cfg: Config, stop: asyncio.Event) -> None:
    while not stop.is_set():
        try:
            await repo_ops.expire_approvals()
            await repo_ops.heartbeat(
                "daemon", os.getpid(), socket.gethostname(), {"status": "running"}
            )
            await _reap_sandboxes()
        except Exception:
            pass
        try:
            await asyncio.wait_for(stop.wait(), timeout=3600)
        except TimeoutError:
            pass


async def _reap_sandboxes() -> None:
    proc = await asyncio.create_subprocess_exec(
        "docker", "ps", "-q", "--filter", "name=agent-sbx-", "--filter", "status=running",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
    )
    out, _ = await proc.communicate()
    ids = out.decode().split()
    for container in ids:
        inspect = await asyncio.create_subprocess_exec(
            "docker", "inspect", "-f", "{{.State.StartedAt}}", container,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        started, _ = await inspect.communicate()
        try:
            when = datetime.fromisoformat(started.decode().strip().replace("Z", "+00:00"))
        except ValueError:
            continue
        if (utcnow() - when).total_seconds() > 3600:
            await (await asyncio.create_subprocess_exec("docker", "kill", container)).wait()


async def supervise(name: str, factory, cfg: Config, stop: asyncio.Event) -> None:
    """Restart a failed task with backoff instead of taking the daemon down."""
    delay = 5
    while not stop.is_set():
        try:
            await factory(cfg, stop)
            return
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await repo_agenda.notify(
                source="daemon", level="error", title=f"Task '{name}' crashed",
                body=f"{type(exc).__name__}: {exc}"[:500],
            )
            try:
                await asyncio.wait_for(stop.wait(), timeout=delay)
                return
            except TimeoutError:
                delay = min(delay * 2, 300)


async def run(cfg: Config | None = None) -> int:
    cfg = cfg or get_config()
    cfg.ensure_dirs()
    otel.setup("agent-daemon", cfg)

    async with connection(cfg) as conn:
        if not await _acquire_singleton(conn):
            print("Another daemon already holds the lock; exiting.")
            return 1

        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError):
                loop.add_signal_handler(sig, stop.set)

        await repo_ops.heartbeat("daemon", os.getpid(), socket.gethostname(), {"status": "starting"})

        from ..mcp_client import close_external_tools, load_external_tools

        clients = await load_external_tools(cfg)
        for server, why in clients.failures.items():
            print(f"mcp:{server} unavailable — {why}")

        tasks = [
            asyncio.create_task(supervise("scheduler", scheduler_loop, cfg, stop)),
            asyncio.create_task(supervise("filewatch", filewatch_loop, cfg, stop)),
            asyncio.create_task(supervise("consolidation", consolidation_loop, cfg, stop)),
            asyncio.create_task(supervise("heartbeat", heartbeat_loop, cfg, stop)),
            asyncio.create_task(supervise("housekeeping", housekeeping_loop, cfg, stop)),
            asyncio.create_task(supervise("notifier", notifier_loop, cfg, stop)),
        ]
        # One supervised task per connector rather than one loop fanning out: supervise()
        # names the failing task in its crash notification, so a broken Gmail poller reports
        # as "connector:gmail" instead of taking GitHub down with it.
        tasks += [
            asyncio.create_task(
                supervise(f"connector:{c.name}", partial(run_connector, c), cfg, stop)
            )
            for c in all_connectors(cfg)
        ]
        print(f"agent daemon running (pid {os.getpid()}); Ctrl-C to stop")
        await stop.wait()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await close_external_tools()
        await repo_ops.heartbeat("daemon", os.getpid(), socket.gethostname(), {"status": "stopped"})
    await close_pool()
    return 0
