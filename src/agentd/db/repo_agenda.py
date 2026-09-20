"""Goals, open loops, watchers and the notification inbox."""

from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any
from uuid import UUID

from ..ids import uuid7
from .pool import connection, fetch_all, fetch_one


def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:60] or "goal"


# --- goals -------------------------------------------------------------------

async def upsert_goal(
    *, title: str, description: str | None = None, slug: str | None = None,
    status: str = "active", horizon: str = "quarter", priority: int = 3,
    due_at: datetime | None = None, next_step: str | None = None,
) -> UUID:
    slug = slug or slugify(title)
    async with connection() as conn:
        cur = await conn.execute(
            """
            INSERT INTO goals (id, slug, title, description, status, horizon, priority,
                               due_at, next_step)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (slug) DO UPDATE SET
              title = EXCLUDED.title,
              description = COALESCE(EXCLUDED.description, goals.description),
              status = EXCLUDED.status, horizon = EXCLUDED.horizon,
              priority = EXCLUDED.priority,
              due_at = COALESCE(EXCLUDED.due_at, goals.due_at),
              next_step = COALESCE(EXCLUDED.next_step, goals.next_step),
              updated_at = now()
            RETURNING id
            """,
            (uuid7(), slug, title, description, status, horizon, priority, due_at, next_step),
        )
        row = await cur.fetchone()
        return row["id"]


async def list_goals(status: str | None = "active", limit: int = 50) -> list[dict]:
    if status:
        return await fetch_all(
            "SELECT * FROM goals WHERE status = %s ORDER BY priority, created_at LIMIT %s",
            (status, limit),
        )
    return await fetch_all("SELECT * FROM goals ORDER BY priority, created_at LIMIT %s", (limit,))


async def goal_by_slug(slug: str) -> dict | None:
    return await fetch_one("SELECT * FROM goals WHERE slug = %s", (slug,))


async def set_goal_status(slug: str, status: str) -> None:
    async with connection() as conn:
        await conn.execute(
            "UPDATE goals SET status = %s, updated_at = now() WHERE slug = %s", (status, slug)
        )


async def goals_due_for_review() -> list[dict]:
    return await fetch_all(
        """
        SELECT * FROM goals
        WHERE status = 'active'
          AND (last_reviewed_at IS NULL OR last_reviewed_at + review_every < now())
        ORDER BY priority
        """
    )


async def mark_goal_reviewed(goal_id: UUID) -> None:
    async with connection() as conn:
        await conn.execute("UPDATE goals SET last_reviewed_at = now() WHERE id = %s", (goal_id,))


# --- open loops --------------------------------------------------------------

async def add_open_loop(
    *, title: str, detail: str | None = None, goal_id: UUID | None = None,
    due_at: datetime | None = None, waiting_on: str | None = None,
    source_event_id: UUID | None = None,
) -> UUID:
    lid = uuid7()
    async with connection() as conn:
        await conn.execute(
            """
            INSERT INTO open_loops (id, title, detail, goal_id, due_at, waiting_on,
                                    source_event_id, status)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
            """,
            (
                lid, title, detail, goal_id, due_at, waiting_on, source_event_id,
                "waiting" if waiting_on else "open",
            ),
        )
    return lid


async def list_open_loops(status: str | None = "open", limit: int = 50) -> list[dict]:
    if status:
        return await fetch_all(
            """
            SELECT * FROM open_loops WHERE status = %s
            ORDER BY due_at NULLS LAST, created_at LIMIT %s
            """,
            (status, limit),
        )
    return await fetch_all(
        "SELECT * FROM open_loops ORDER BY created_at DESC LIMIT %s", (limit,)
    )


async def list_open_loops_with_source(status: str | None = "open", limit: int = 50) -> list[dict]:
    """`list_open_loops`, plus which feed opened each one.

    The join is `open_loops.source_event_id -> raw_events.event_id`, the same one
    `connectors.base.tracked_loops` makes for the sweeps - stated once more here rather than
    generalised, because that one asks "which of my items are still open" and this one asks
    "where did this row come from", and they want different columns.

    LEFT, and deliberately: a loop from `open_loop_add`, from the MCP server or from the
    review gate has no `source_event_id` at all, and a loop the agent opened itself is not a
    loop with a broken provenance link. Those rows come back with `connector` as None.
    """
    where = "WHERE l.status = %s" if status else ""
    params: tuple[Any, ...] = (status, limit) if status else (limit,)
    order = "l.due_at NULLS LAST, l.created_at" if status else "l.created_at DESC"
    return await fetch_all(
        f"""
        SELECT l.*, e.payload->>'connector' AS connector
        FROM open_loops l
        LEFT JOIN raw_events e ON e.event_id = l.source_event_id
        {where}
        ORDER BY {order} LIMIT %s
        """,
        params,
    )


async def close_open_loop(loop_id: UUID) -> None:
    async with connection() as conn:
        await conn.execute(
            "UPDATE open_loops SET status = 'closed', closed_at = now() WHERE id = %s", (loop_id,)
        )


async def overdue_loops() -> list[dict]:
    return await fetch_all(
        """
        SELECT * FROM open_loops
        WHERE status IN ('open','waiting') AND due_at IS NOT NULL AND due_at < now()
          AND (snooze_until IS NULL OR snooze_until < now())
        ORDER BY due_at
        """
    )


async def loop_exists(title: str) -> bool:
    row = await fetch_one(
        "SELECT 1 AS x FROM open_loops WHERE title = %s AND status <> 'closed' LIMIT 1", (title,)
    )
    return row is not None


# --- watchers ----------------------------------------------------------------

async def add_watcher(
    *, name: str, kind: str, spec: dict[str, Any], action: dict[str, Any],
    created_by: str, autonomy: str = "observe", next_fire_at: datetime | None = None,
) -> UUID:
    wid = uuid7()
    async with connection() as conn:
        await conn.execute(
            """
            INSERT INTO watchers (id, name, kind, spec, action, autonomy, next_fire_at, created_by)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
            """,
            (
                wid, name, kind, json.dumps(spec, default=str), json.dumps(action, default=str),
                autonomy, next_fire_at, created_by,
            ),
        )
    return wid


async def list_watchers(enabled_only: bool = False) -> list[dict]:
    if enabled_only:
        return await fetch_all("SELECT * FROM watchers WHERE enabled ORDER BY next_fire_at")
    return await fetch_all("SELECT * FROM watchers ORDER BY created_at DESC")


async def file_watchers() -> list[dict]:
    return await fetch_all("SELECT * FROM watchers WHERE enabled AND kind = 'file'")


async def claim_due_watchers(limit: int = 10) -> list[dict]:
    """Claim due watchers so two daemons never fire the same one."""
    async with connection() as conn:
        async with conn.transaction():
            cur = await conn.execute(
                """
                SELECT * FROM watchers
                WHERE enabled AND kind <> 'file' AND next_fire_at IS NOT NULL
                  AND next_fire_at <= now()
                ORDER BY next_fire_at
                FOR UPDATE SKIP LOCKED
                LIMIT %s
                """,
                (limit,),
            )
            rows = await cur.fetchall()
            if rows:
                await conn.execute(
                    "UPDATE watchers SET last_fired_at = now(), fire_count = fire_count + 1, "
                    "next_fire_at = NULL WHERE id = ANY(%s)",
                    ([r["id"] for r in rows],),
                )
    return rows


async def reschedule_watcher(watcher_id: UUID, next_fire_at: datetime | None) -> None:
    async with connection() as conn:
        await conn.execute(
            "UPDATE watchers SET next_fire_at = %s, enabled = %s WHERE id = %s",
            (next_fire_at, next_fire_at is not None, watcher_id),
        )


async def set_watcher_enabled(watcher_id: UUID, enabled: bool) -> None:
    async with connection() as conn:
        await conn.execute(
            "UPDATE watchers SET enabled = %s WHERE id = %s", (enabled, watcher_id)
        )


# --- notifications -----------------------------------------------------------

async def notify(
    *, source: str, title: str, body: str | None = None, level: str = "info",
    ref: dict | None = None,
) -> int:
    async with connection() as conn:
        cur = await conn.execute(
            """
            INSERT INTO notifications (source, level, title, body, ref)
            VALUES (%s,%s,%s,%s,%s) RETURNING id
            """,
            (source, level, title, body, json.dumps(ref or {}, default=str)),
        )
        row = await cur.fetchone()
        return row["id"]


async def list_notifications(unread_only: bool = True, limit: int = 50) -> list[dict]:
    if unread_only:
        return await fetch_all(
            "SELECT * FROM notifications WHERE read_at IS NULL ORDER BY created_at DESC LIMIT %s",
            (limit,),
        )
    return await fetch_all(
        "SELECT * FROM notifications ORDER BY created_at DESC LIMIT %s", (limit,)
    )


async def get_notification(notification_id: int) -> dict | None:
    return await fetch_one("SELECT * FROM notifications WHERE id = %s", (notification_id,))


async def mark_read(ids: list[int] | None = None) -> None:
    async with connection() as conn:
        if ids:
            await conn.execute(
                "UPDATE notifications SET read_at = now() WHERE id = ANY(%s)", (ids,)
            )
        else:
            await conn.execute(
                "UPDATE notifications SET read_at = now() WHERE read_at IS NULL"
            )
