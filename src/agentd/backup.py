"""Backups, because "your memory is durable" is a claim that has to survive this box dying.

Everything durable lives in two places: the Postgres volume and the git-backed markdown repo.
This takes a consistent copy of both into one timestamped directory, and can put them back.

`pg_dump` is deliberately run inside the database container rather than on the host: the host
has no client installed, and a mismatched major version would refuse the dump anyway.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from .config import Config, get_config
from .ids import utcnow

MANIFEST = "manifest.json"
DUMP = "database.dump"
BUNDLE = "memory.bundle"


@dataclass
class BackupResult:
    path: Path
    bytes_db: int
    bytes_repo: int
    pruned: list[str]


def _dsn_parts(dsn: str) -> tuple[str, str]:
    parsed = urlparse(dsn)
    return (parsed.username or "agent"), (parsed.path.lstrip("/") or "agent")


async def _run(*args: str, stdin_from: Path | None = None, stdout_to: Path | None = None) -> bytes:
    """Run a command, raising with its stderr attached — a silent backup failure is the worst kind."""
    stdout = open(stdout_to, "wb") if stdout_to else asyncio.subprocess.PIPE
    stdin = open(stdin_from, "rb") if stdin_from else None
    try:
        proc = await asyncio.create_subprocess_exec(
            *args, stdin=stdin, stdout=stdout, stderr=asyncio.subprocess.PIPE
        )
        out, err = await proc.communicate()
        if proc.returncode != 0:
            raise RuntimeError(f"{args[0]} failed ({proc.returncode}): {err.decode()[:500]}")
        return out or b""
    finally:
        if stdin:
            stdin.close()
        if stdout_to:
            stdout.close()


async def create(cfg: Config | None = None, *, keep: int = 14) -> BackupResult:
    cfg = cfg or get_config()
    cfg.ensure_dirs()
    user, database = _dsn_parts(cfg.db.dsn)
    stamp = utcnow().strftime("%Y%m%dT%H%M%SZ")
    dest = cfg.paths.backups / stamp
    dest.mkdir(parents=True, exist_ok=True)

    await _run(
        "docker", "exec", cfg.db.container,
        "pg_dump", "-U", user, "-d", database, "-Fc", "--no-owner",
        stdout_to=dest / DUMP,
    )

    repo = cfg.paths.memory_repo
    bytes_repo = 0
    if (repo / ".git").exists():
        # --all rather than HEAD: the history is the point, not just the current state.
        await _run("git", "-C", str(repo), "bundle", "create", str(dest / BUNDLE), "--all")
        bytes_repo = (dest / BUNDLE).stat().st_size

    bytes_db = (dest / DUMP).stat().st_size
    (dest / MANIFEST).write_text(
        json.dumps(
            {
                "created_at": utcnow().isoformat(),
                "database": database,
                "dsn_host": urlparse(cfg.db.dsn).netloc.split("@")[-1],
                "dump_bytes": bytes_db,
                "bundle_bytes": bytes_repo,
                "memory_repo": str(repo),
            },
            indent=2,
        )
        + "\n"
    )

    pruned = _prune(cfg.paths.backups, keep)
    return BackupResult(path=dest, bytes_db=bytes_db, bytes_repo=bytes_repo, pruned=pruned)


def _prune(root: Path, keep: int) -> list[str]:
    if keep <= 0:
        return []
    backups = sorted(p for p in root.iterdir() if p.is_dir() and (p / MANIFEST).exists())
    doomed = backups[:-keep] if len(backups) > keep else []
    for path in doomed:
        shutil.rmtree(path, ignore_errors=True)
    return [p.name for p in doomed]


def latest(cfg: Config | None = None) -> Path | None:
    cfg = cfg or get_config()
    if not cfg.paths.backups.exists():
        return None
    backups = sorted(
        p for p in cfg.paths.backups.iterdir() if p.is_dir() and (p / MANIFEST).exists()
    )
    return backups[-1] if backups else None


async def restore(
    source: Path, *, into: str, cfg: Config | None = None, drop_existing: bool = False
) -> str:
    """Restore a backup into `into`. Defaults elsewhere in the CLI to a scratch database,
    because a restore you have never rehearsed is a rumour, and rehearsing it should not
    risk the live one."""
    cfg = cfg or get_config()
    user, _ = _dsn_parts(cfg.db.dsn)
    dump = source / DUMP
    if not dump.exists():
        raise FileNotFoundError(f"no {DUMP} in {source}")

    if drop_existing:
        await _run(
            "docker", "exec", cfg.db.container,
            "psql", "-U", user, "-d", "postgres", "-c", f'DROP DATABASE IF EXISTS "{into}"',
        )
    await _run(
        "docker", "exec", cfg.db.container,
        "psql", "-U", user, "-d", "postgres", "-c", f'CREATE DATABASE "{into}"',
    )
    # Stream the dump in over stdin so nothing has to be copied into the container first.
    proc = subprocess.run(
        ["docker", "exec", "-i", cfg.db.container,
         "pg_restore", "-U", user, "-d", into, "--no-owner"],
        stdin=open(dump, "rb"), capture_output=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"pg_restore failed: {proc.stderr.decode()[:800]}")
    return into


async def verify(source: Path, cfg: Config | None = None) -> dict[str, int]:
    """Restore into a scratch database and count what came back. This is the only way to know
    a backup is real; `agent backup --verify` runs it."""
    cfg = cfg or get_config()
    user, _ = _dsn_parts(cfg.db.dsn)
    scratch = "agent_restore_check"
    await restore(source, into=scratch, cfg=cfg, drop_existing=True)
    counts: dict[str, int] = {}
    for table in ("facts", "raw_events", "actions", "goals", "open_loops"):
        out = await _run(
            "docker", "exec", cfg.db.container,
            "psql", "-U", user, "-d", scratch, "-tAc", f"SELECT count(*) FROM {table}",
        )
        counts[table] = int(out.decode().strip() or 0)
    await _run(
        "docker", "exec", cfg.db.container,
        "psql", "-U", user, "-d", "postgres", "-c", f'DROP DATABASE IF EXISTS "{scratch}"',
    )
    return counts
