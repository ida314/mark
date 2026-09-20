"""Sessions and the append-only raw event archive."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID

from ..ids import utcnow, uuid7
from .pool import connection, fetch_all, fetch_one


@dataclass
class RawEvent:
    kind: str
    actor: str
    content: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)
    trust: str = "trusted"
    session_id: UUID | None = None
    turn_id: UUID | None = None
    event_id: UUID = field(default_factory=uuid7)
    occurred_at: datetime = field(default_factory=utcnow)


async def create_session(channel: str = "cli", title: str | None = None) -> UUID:
    sid = uuid7()
    async with connection() as conn:
        await conn.execute(
            "INSERT INTO sessions (id, channel, title) VALUES (%s, %s, %s)", (sid, channel, title)
        )
    return sid


async def touch_session(session_id: UUID) -> None:
    async with connection() as conn:
        await conn.execute(
            "UPDATE sessions SET last_activity_at = now() WHERE id = %s", (session_id,)
        )


async def end_session(session_id: UUID, summary: str | None = None) -> None:
    async with connection() as conn:
        await conn.execute(
            "UPDATE sessions SET ended_at = now(), summary = COALESCE(%s, summary) WHERE id = %s",
            (summary, session_id),
        )


async def get_session(session_id: UUID) -> dict | None:
    return await fetch_one("SELECT * FROM sessions WHERE id = %s", (session_id,))


async def latest_session(channel: str = "cli") -> dict | None:
    return await fetch_one(
        "SELECT * FROM sessions WHERE channel = %s ORDER BY last_activity_at DESC LIMIT 1",
        (channel,),
    )


async def append_event(event: RawEvent) -> UUID:
    """Archive one thing that happened. Never updated, never deleted."""
    sha = (
        hashlib.sha256(event.content.encode()).hexdigest()
        if event.content is not None
        else None
    )
    async with connection() as conn:
        await conn.execute(
            """
            INSERT INTO raw_events
              (event_id, occurred_at, session_id, turn_id, kind, actor, content, payload,
               trust, content_sha256)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                event.event_id,
                event.occurred_at,
                event.session_id,
                event.turn_id,
                event.kind,
                event.actor,
                event.content,
                json.dumps(event.payload),
                event.trust,
                sha,
            ),
        )
    return event.event_id


async def append_event_once(event: RawEvent) -> UUID | None:
    """Archive one thing, unless `payload['dedup_key']` is already in the archive.

    Returns the event id, or None when it was already there. That distinction is the whole
    point: a connector uses it to decide whether this is the first time it has seen
    something, and therefore whether to act on it. Doing the check in one statement rather
    than a SELECT followed by an INSERT is what makes an overlapping poll harmless.

    `DO NOTHING` is also the only conflict action the schema permits here: `forbid_mutation()`
    fires BEFORE UPDATE OR DELETE, so a `DO UPDATE` would be refused by the append-only
    trigger. The archive stays append-only by construction rather than by convention.
    """
    sha = (
        hashlib.sha256(event.content.encode()).hexdigest()
        if event.content is not None
        else None
    )
    async with connection() as conn:
        cur = await conn.execute(
            """
            INSERT INTO raw_events
              (event_id, occurred_at, session_id, turn_id, kind, actor, content, payload,
               trust, content_sha256)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT ((payload->>'dedup_key')) WHERE (payload->>'dedup_key') IS NOT NULL
            DO NOTHING
            RETURNING event_id
            """,
            (
                event.event_id,
                event.occurred_at,
                event.session_id,
                event.turn_id,
                event.kind,
                event.actor,
                event.content,
                json.dumps(event.payload),
                event.trust,
                sha,
            ),
        )
        row = await cur.fetchone()
    return event.event_id if row else None


async def events_for_session(
    session_id: UUID, *, after_id: int = 0, limit: int = 2000
) -> list[dict]:
    return await fetch_all(
        """
        SELECT * FROM raw_events
        WHERE session_id = %s AND id > %s
        ORDER BY id LIMIT %s
        """,
        (session_id, after_id, limit),
    )


async def max_event_id(session_id: UUID) -> int:
    row = await fetch_one(
        "SELECT COALESCE(max(id), 0) AS m FROM raw_events WHERE session_id = %s", (session_id,)
    )
    return int(row["m"]) if row else 0


async def recent_messages(session_id: UUID, limit: int = 200) -> list[dict]:
    """User and assistant turns, oldest first, for rebuilding the chat history window."""
    rows = await fetch_all(
        """
        SELECT * FROM raw_events
        WHERE session_id = %s AND kind IN ('user_message', 'assistant_message')
        ORDER BY id DESC LIMIT %s
        """,
        (session_id, limit),
    )
    return list(reversed(rows))


async def upcoming_events(kind_like: str, hours: int, limit: int = 50) -> list[dict]:
    """Connector-archived events starting in the next `hours`, most recent version of each.

    `DISTINCT ON` is doing real work here. The archive is append-only and version-sensitive
    by design, so an event that was moved twice is three rows; the last one written is the
    one that is true, and counting all three would report a day three times as full as it is.
    """
    rows = await fetch_all(
        """
        SELECT DISTINCT ON (payload->>'external_id') occurred_at, kind, payload
        FROM raw_events
        WHERE kind LIKE %s
          AND payload->>'external_id' IS NOT NULL
          AND occurred_at >= now()
          AND occurred_at < now() + make_interval(hours => %s)
        ORDER BY payload->>'external_id', id DESC
        LIMIT %s
        """,
        (kind_like, hours, limit),
    )
    return sorted(rows, key=lambda r: r["occurred_at"])


async def set_consolidated_upto(session_id: UUID, event_id: int) -> None:
    async with connection() as conn:
        await conn.execute(
            "UPDATE sessions SET consolidated_upto = %s WHERE id = %s", (event_id, session_id)
        )


async def sessions_needing_consolidation(idle_seconds: int) -> list[dict]:
    return await fetch_all(
        """
        SELECT s.* FROM sessions s
        WHERE (s.ended_at IS NOT NULL
               OR s.last_activity_at < now() - make_interval(secs => %s))
          AND EXISTS (
            SELECT 1 FROM raw_events e
            WHERE e.session_id = s.id AND e.id > s.consolidated_upto)
        ORDER BY s.last_activity_at
        LIMIT 20
        """,
        (idle_seconds,),
    )
