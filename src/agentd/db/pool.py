"""Async connection pool plus a LISTEN helper."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import psycopg
from pgvector.psycopg import register_vector_async
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from ..config import Config, get_config

_pool: AsyncConnectionPool | None = None


async def _configure(conn: psycopg.AsyncConnection) -> None:
    conn.row_factory = dict_row
    try:
        await register_vector_async(conn)
    except Exception:  # extension missing before the first migration runs
        pass


async def get_pool(cfg: Config | None = None) -> AsyncConnectionPool:
    global _pool
    if _pool is None:
        cfg = cfg or get_config()
        _pool = AsyncConnectionPool(
            cfg.db.dsn,
            min_size=cfg.db.min_size,
            max_size=cfg.db.max_size,
            configure=_configure,
            open=False,
            kwargs={"autocommit": True},
        )
        await _pool.open(wait=True, timeout=30)
    return _pool


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


@asynccontextmanager
async def connection(cfg: Config | None = None) -> AsyncIterator[psycopg.AsyncConnection]:
    pool = await get_pool(cfg)
    async with pool.connection() as conn:
        yield conn


@asynccontextmanager
async def transaction(cfg: Config | None = None) -> AsyncIterator[psycopg.AsyncConnection]:
    async with connection(cfg) as conn, conn.transaction():
        yield conn


async def fetch_all(sql: str, params: dict | list | tuple | None = None) -> list[dict]:
    async with connection() as conn:
        cur = await conn.execute(sql, params)
        return await cur.fetchall()


async def fetch_one(sql: str, params: dict | list | tuple | None = None) -> dict | None:
    async with connection() as conn:
        cur = await conn.execute(sql, params)
        return await cur.fetchone()


async def execute(sql: str, params: dict | list | tuple | None = None) -> None:
    async with connection() as conn:
        await conn.execute(sql, params)


async def listen(channel: str, cfg: Config | None = None) -> AsyncIterator[str]:
    """Yield payloads from a Postgres NOTIFY channel on a dedicated connection."""
    cfg = cfg or get_config()
    conn = await psycopg.AsyncConnection.connect(cfg.db.dsn, autocommit=True)
    try:
        await conn.execute(f'LISTEN "{channel}"')
        gen = conn.notifies()
        async for note in gen:
            yield note.payload
    finally:
        await conn.close()


async def wait_ready(cfg: Config | None = None, timeout: float = 60.0) -> bool:
    """Poll until Postgres accepts connections. Used by `agent db wait`."""
    cfg = cfg or get_config()
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        try:
            conn = await psycopg.AsyncConnection.connect(cfg.db.dsn, connect_timeout=3)
            await conn.close()
            return True
        except Exception:
            await asyncio.sleep(1.0)
    return False
