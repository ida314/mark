"""Where each connector is up to.

One row per connector, holding a cursor, a kill switch and enough bookkeeping for
`agent connectors list` and `agent doctor` to answer "is this thing actually working?"
without a network call.

Two things here are deliberate rather than incidental:

- **`next_poll_at` is in the database, not in memory.** Backoff that lives in a process is
  reset by a restart, and a crash-restart loop that ignores a rate limit is how you earn a
  secondary limit from GitHub.
- **Being rate-limited is not a failure.** `record_failure(count_it=False)` exists so a busy
  hour does not look like an outage and auto-disable the connector.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from .pool import connection, fetch_all


async def load_state(name: str) -> dict:
    """Read this connector's row, creating it on first sight.

    The no-op `DO UPDATE` exists purely so `RETURNING` always fires — `DO NOTHING` returns
    no row on conflict, which would mean a second round trip on every single poll.
    """
    async with connection() as conn:
        cur = await conn.execute(
            """
            INSERT INTO connector_state (name) VALUES (%s)
            ON CONFLICT (name) DO UPDATE SET name = connector_state.name
            RETURNING *
            """,
            (name,),
        )
        row = await cur.fetchone()
    return dict(row or {})


async def record_success(
    name: str,
    *,
    cursor: dict[str, Any],
    next_poll_at: datetime,
    new_items: int = 0,
    new_loops: int = 0,
) -> None:
    async with connection() as conn:
        await conn.execute(
            """
            UPDATE connector_state SET
              cursor = %s,
              last_polled_at = now(),
              last_success_at = now(),
              next_poll_at = %s,
              consecutive_failures = 0,
              last_error = NULL,
              items_seen = items_seen + %s,
              loops_opened = loops_opened + %s,
              updated_at = now()
            WHERE name = %s
            """,
            (json.dumps(cursor), next_poll_at, new_items, new_loops, name),
        )


async def record_failure(
    name: str,
    *,
    error: str,
    next_poll_at: datetime | None,
    count_it: bool = True,
) -> None:
    """Record a poll that did not work.

    `count_it=False` is the rate-limit path: the source is working exactly as designed and
    has asked us to wait. Counting that would let a busy hour trip the failure threshold and
    disable a connector that is perfectly healthy.
    """
    async with connection() as conn:
        await conn.execute(
            """
            UPDATE connector_state SET
              last_polled_at = now(),
              next_poll_at = %s,
              consecutive_failures = consecutive_failures + %s,
              last_error = %s,
              updated_at = now()
            WHERE name = %s
            """,
            (next_poll_at, 1 if count_it else 0, error[:500], name),
        )


async def set_enabled(name: str, enabled: bool, *, reason: str | None = None) -> None:
    """Flip the switch. Enabling clears the wreckage of whatever turned it off and asks for
    an immediate retry, because the reason you enable a connector is that you just fixed it."""
    async with connection() as conn:
        if enabled:
            await conn.execute(
                """
                UPDATE connector_state SET
                  enabled = true, disabled_reason = NULL, consecutive_failures = 0,
                  last_error = NULL, next_poll_at = now(), updated_at = now()
                WHERE name = %s
                """,
                (name,),
            )
        else:
            await conn.execute(
                """
                UPDATE connector_state SET
                  enabled = false, disabled_reason = %s, updated_at = now()
                WHERE name = %s
                """,
                (reason, name),
            )


async def record_sweep(name: str) -> None:
    async with connection() as conn:
        await conn.execute(
            "UPDATE connector_state SET last_sweep_at = now(), updated_at = now() WHERE name = %s",
            (name,),
        )


async def reset_cursor(name: str) -> None:
    """Forget where we were, forcing a full unconditional re-fetch.

    Safe precisely because ingestion is idempotent by unique index — re-reading everything
    re-archives nothing.
    """
    async with connection() as conn:
        await conn.execute(
            "UPDATE connector_state SET cursor = '{}', next_poll_at = now(), updated_at = now() "
            "WHERE name = %s",
            (name,),
        )


async def list_state() -> list[dict]:
    return await fetch_all("SELECT * FROM connector_state ORDER BY name")
