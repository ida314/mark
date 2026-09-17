"""Migration runner: numbered SQL files, applied once, checksum-verified."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

from ..config import REPO_ROOT, Config, get_config

MIGRATIONS_DIR = REPO_ROOT / "migrations"
LOCK_KEY = 0x4147_454E  # "AGEN"

BOOTSTRAP = """
CREATE TABLE IF NOT EXISTS schema_migrations (
  version text PRIMARY KEY,
  checksum text NOT NULL,
  applied_at timestamptz NOT NULL DEFAULT now()
)
"""


@dataclass(frozen=True)
class Migration:
    version: str
    path: Path
    sql: str

    @property
    def checksum(self) -> str:
        return hashlib.sha256(self.sql.encode()).hexdigest()


def discover(directory: Path | None = None) -> list[Migration]:
    directory = directory or MIGRATIONS_DIR
    out = []
    for path in sorted(directory.glob("*.sql")):
        out.append(Migration(version=path.stem, path=path, sql=path.read_text()))
    return out


class MigrationDrift(RuntimeError):
    """An already-applied migration file changed on disk."""


async def applied_versions(conn: psycopg.AsyncConnection) -> dict[str, str]:
    await conn.execute(BOOTSTRAP)
    cur = await conn.execute("SELECT version, checksum FROM schema_migrations")
    return {r["version"]: r["checksum"] for r in await cur.fetchall()}


async def pending(conn: psycopg.AsyncConnection, directory: Path | None = None) -> list[Migration]:
    done = await applied_versions(conn)
    out = []
    for m in discover(directory):
        if m.version not in done:
            out.append(m)
        elif done[m.version] != m.checksum:
            raise MigrationDrift(
                f"migration {m.version} changed after being applied; "
                "roll it forward in a new file instead"
            )
    return out


async def migrate(
    cfg: Config | None = None, *, dsn: str | None = None, directory: Path | None = None
) -> list[str]:
    """Apply every pending migration, each in its own transaction. Returns versions applied."""
    cfg = cfg or get_config()
    conn = await psycopg.AsyncConnection.connect(
        dsn or cfg.db.dsn, autocommit=True, row_factory=dict_row
    )
    applied: list[str] = []
    try:
        await conn.execute("SELECT pg_advisory_lock(%s)", (LOCK_KEY,))
        try:
            for m in await pending(conn, directory):
                async with conn.transaction():
                    await conn.execute(m.sql)
                    await conn.execute(
                        "INSERT INTO schema_migrations (version, checksum) VALUES (%s, %s)",
                        (m.version, m.checksum),
                    )
                applied.append(m.version)
        finally:
            await conn.execute("SELECT pg_advisory_unlock(%s)", (LOCK_KEY,))
    finally:
        await conn.close()
    return applied


async def status(cfg: Config | None = None) -> tuple[list[str], list[str]]:
    """(applied, pending) version lists."""
    cfg = cfg or get_config()
    conn = await psycopg.AsyncConnection.connect(
        cfg.db.dsn, autocommit=True, row_factory=dict_row
    )
    try:
        done = await applied_versions(conn)
        all_versions = [m.version for m in discover()]
        return (
            [v for v in all_versions if v in done],
            [v for v in all_versions if v not in done],
        )
    finally:
        await conn.close()
