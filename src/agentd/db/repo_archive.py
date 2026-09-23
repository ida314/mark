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


async def session_read_private(session_id: UUID) -> bool:
    """Has this session ever pulled the user's private data into context?

    The interlock that keeps mail from leaving the box lives on the in-process `Session`, so
    a resumed conversation would otherwise start with the door open again. The archive is
    the durable record: every private tool result was written with `payload->>'private'`.
    """
    row = await fetch_one(
        """
        SELECT 1 AS found FROM raw_events
        WHERE session_id = %s AND kind = 'tool_result' AND payload->>'private' = 'true'
        LIMIT 1
        """,
        (session_id,),
    )
    return row is not None


async def max_event_id(session_id: UUID) -> int:
    row = await fetch_one(
        "SELECT COALESCE(max(id), 0) AS m FROM raw_events WHERE session_id = %s", (session_id,)
    )
    return int(row["m"]) if row else 0


async def recent_messages(
    session_id: UUID, limit: int = 200, *, after_id: int | None = None
) -> list[dict]:
    """User and assistant turns, oldest first, for rebuilding the chat history window.

    `after_id` is a handoff watermark (session 5b): everything at or below it has been
    replaced by a handoff object, so a conversation that has handed off asks for the part
    the handoff does not already state rather than re-reading what it summarised. `id` is
    the archive's own identity column and is monotonic, which is what the existing
    `ORDER BY id DESC` already relies on.

    A worker's messages are excluded (session 6b). A sub-agent runs inside its caller's
    session, so its final prose is archived here as an `assistant_message` on that session
    like any other - and without this clause the next orchestrator turn rebuilt its history
    from rows a worker wrote, which is "worker transcripts never enter orchestrator context"
    broken in the one place nobody would look for it. The rows stay in the archive, which is
    where the runtime reads them from; what changes is that nothing replays them into a
    prompt. The actor is `subagent:<role>` (`run_subagent` sets it), and the delegating
    model's own door to that work is the `delegate` tool's result.
    """
    rows = await fetch_all(
        """
        SELECT * FROM raw_events
        WHERE session_id = %s AND kind IN ('user_message', 'assistant_message')
          AND actor NOT LIKE 'subagent:%%'
          AND (%s::bigint IS NULL OR id > %s::bigint)
        ORDER BY id DESC LIMIT %s
        """,
        (session_id, after_id, after_id, limit),
    )
    return list(reversed(rows))


async def recent_message_sizes(session_id: UUID, limit: int = 200) -> list[tuple[int, int]]:
    """`(id, characters)` for this session's messages, newest first.

    Sizes rather than bodies: choosing where a handoff's watermark goes (session 5b) needs
    to know how much each message costs and nothing about what it says, and a 42KB paste is
    not worth loading to measure.

    The same set of rows `recent_messages` replays, worker exclusion included: a watermark is
    chosen by adding these sizes up, and counting a message that will never be replayed
    measures a conversation nobody is going to be shown.
    """
    rows = await fetch_all(
        """
        SELECT id, coalesce(length(content), 0) AS chars FROM raw_events
        WHERE session_id = %s AND kind IN ('user_message', 'assistant_message')
          AND actor NOT LIKE 'subagent:%%'
        ORDER BY id DESC LIMIT %s
        """,
        (session_id, limit),
    )
    return [(int(r["id"]), int(r["chars"])) for r in rows]


# The kinds a handoff's manifest can list, and the lookup can resolve. Three rather than
# two: `tool_result` is not replayed into any later prompt and never was, so a successor
# has no more access to it than to a message the handoff replaced. Leaving it out would
# make the manifest a list of what the *handoff* dropped rather than of what the successor
# does not have, and those differ by exactly the material a turn spent its whole step
# budget gathering.
MANIFEST_KINDS: tuple[str, ...] = (
    "user_message", "assistant_message", "tool_result", "assistant_step",
)


async def manifest_rows(
    session_id: UUID,
    *,
    upto_id: int,
    watermark: int | None,
    excerpt_chars: int,
    limit: int,
) -> list[dict]:
    """What this session holds that a successor will not see, newest first.

    Two different lines, because the two kinds of item are dropped for two different
    reasons. A `tool_result` is never replayed into a later prompt at all, so everything at
    or below `upto_id` is gone from the successor's view. A conversation message is dropped
    only if it fell below the handoff's `watermark`; the ones above it are carried verbatim
    and listing them would be a manifest that offers to fetch what the reader already has.
    `watermark = NULL` means nothing was dropped from the conversation, and the SQL says so
    by matching no message rows rather than by matching all of them.

    Excerpts are taken in SQL, so a 42,000-character paste is described without being read
    into this process - the same reason `recent_message_sizes` returns sizes.
    """
    rows = await fetch_all(
        """
        SELECT e.id, e.kind, e.actor, e.trust,
               coalesce(length(e.content), 0) AS chars,
               left(coalesce(e.content, ''), %s) AS excerpt,
               (e.payload->>'private' = 'true') AS private
        FROM raw_events e
        WHERE e.session_id = %s
          AND e.id <= %s
          AND (
                (e.kind = 'tool_result')
             OR (e.kind IN ('user_message', 'assistant_message')
                 AND %s::bigint IS NOT NULL AND e.id <= %s::bigint)
             -- A step row is listed only when it is the *only* copy of what the model
             -- said. A turn that finished archives its prose twice - once per step, once
             -- joined as the answer - and offering both would hand the successor two refs
             -- for one piece of text. A turn that died has no joined answer, and then the
             -- step rows are the only record there is.
             OR (e.kind = 'assistant_step' AND NOT EXISTS (
                   SELECT 1 FROM raw_events a
                   WHERE a.session_id = e.session_id AND a.turn_id = e.turn_id
                     AND a.kind = 'assistant_message'))
          )
        ORDER BY e.id DESC LIMIT %s
        """,
        (excerpt_chars, session_id, upto_id, watermark, watermark, limit),
    )
    return list(rows)


async def archived_item(session_id: UUID, event_id: int) -> dict | None:
    """One archived row of this session, by its own id, or None.

    Scoped to the session in the query rather than checked afterwards. The caller is a tool
    the model can call with any integer it likes, and a lookup that fetched the row first
    and compared session ids second would be one forgotten branch away from reading another
    conversation's mail.
    """
    return await fetch_one(
        """
        SELECT id, kind, actor, trust, content, occurred_at,
               (payload->>'private' = 'true') AS private
        FROM raw_events
        WHERE session_id = %s AND id = %s AND kind = ANY(%s)
        """,
        (session_id, event_id, list(MANIFEST_KINDS)),
    )


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
