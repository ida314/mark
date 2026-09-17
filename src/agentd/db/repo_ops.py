"""Audit log, approvals queue, tool registry rows, run bookkeeping."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from ..ids import uuid7
from .pool import connection, fetch_all, fetch_one


@dataclass
class ActionRecord:
    """One completed step of anything the system did. Append-only."""

    actor: str
    kind: str
    name: str
    status: str
    id: UUID = field(default_factory=uuid7)
    trace_id: str | None = None
    span_id: str | None = None
    parent_id: UUID | None = None
    session_id: UUID | None = None
    turn_id: UUID | None = None
    rationale: str | None = None
    input: Any = None
    output: Any = None
    error: str | None = None
    policy: dict | None = None
    refs: dict | None = None
    undo: dict | None = None
    duration_ms: int | None = None
    tokens_in: int | None = None
    tokens_out: int | None = None


def _j(value: Any) -> str | None:
    return None if value is None else json.dumps(value, default=str)


async def write_action(rec: ActionRecord) -> UUID:
    async with connection() as conn:
        await conn.execute(
            """
            INSERT INTO actions
              (id, trace_id, span_id, parent_id, session_id, turn_id, actor, kind, name, status,
               rationale, input, output, error, policy, refs, undo, duration_ms,
               tokens_in, tokens_out)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """,
            (
                rec.id, rec.trace_id, rec.span_id, rec.parent_id, rec.session_id, rec.turn_id,
                rec.actor, rec.kind, rec.name, rec.status, rec.rationale,
                _j(rec.input), _j(rec.output), rec.error, _j(rec.policy), _j(rec.refs),
                _j(rec.undo), rec.duration_ms, rec.tokens_in, rec.tokens_out,
            ),
        )
    return rec.id


async def get_action(action_id: UUID) -> dict | None:
    return await fetch_one("SELECT * FROM actions WHERE id = %s", (action_id,))


async def actions_for_turn(turn_id: UUID) -> list[dict]:
    return await fetch_all("SELECT * FROM actions WHERE turn_id = %s ORDER BY ts", (turn_id,))


async def actions_by_trace(trace_id: str) -> list[dict]:
    return await fetch_all("SELECT * FROM actions WHERE trace_id = %s ORDER BY ts", (trace_id,))


async def recent_actions(limit: int = 30) -> list[dict]:
    return await fetch_all("SELECT * FROM actions ORDER BY ts DESC LIMIT %s", (limit,))


async def action_children(parent_id: UUID) -> list[dict]:
    return await fetch_all("SELECT * FROM actions WHERE parent_id = %s ORDER BY ts", (parent_id,))


# --- approvals ---------------------------------------------------------------

def args_hash(args: dict) -> str:
    return hashlib.sha256(json.dumps(args, sort_keys=True, default=str).encode()).hexdigest()


async def queue_approval(
    *,
    tool_name: str,
    args: dict,
    risk: str,
    policy_rule: str,
    origin: str,
    reason: str | None = None,
    preview: str | None = None,
    session_id: UUID | None = None,
    turn_id: UUID | None = None,
    action_id: UUID | None = None,
) -> UUID:
    aid = uuid7()
    async with connection() as conn:
        await conn.execute(
            """
            INSERT INTO approvals
              (id, origin, session_id, turn_id, action_id, tool_name, args, args_sha256,
               risk, policy_rule, reason, preview)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """,
            (
                aid, origin, session_id, turn_id, action_id, tool_name, json.dumps(args),
                args_hash(args), risk, policy_rule, reason, preview,
            ),
        )
    return aid


async def list_approvals(status: str | None = "pending", limit: int = 50) -> list[dict]:
    if status:
        return await fetch_all(
            "SELECT * FROM approvals WHERE status = %s ORDER BY created_at DESC LIMIT %s",
            (status, limit),
        )
    return await fetch_all("SELECT * FROM approvals ORDER BY created_at DESC LIMIT %s", (limit,))


async def get_approval(approval_id: UUID) -> dict | None:
    return await fetch_one("SELECT * FROM approvals WHERE id = %s", (approval_id,))


async def decide_approval(
    approval_id: UUID, status: str, decided_by: str, note: str | None = None
) -> None:
    async with connection() as conn:
        await conn.execute(
            """
            UPDATE approvals
            SET status = %s, decided_at = now(), decided_by = %s, decision_note = %s
            WHERE id = %s
            """,
            (status, decided_by, note, approval_id),
        )


async def finish_approval(approval_id: UUID, status: str, result: dict) -> None:
    async with connection() as conn:
        await conn.execute(
            "UPDATE approvals SET status = %s, executed_at = now(), result = %s WHERE id = %s",
            (status, json.dumps(result, default=str), approval_id),
        )


async def expire_approvals() -> int:
    async with connection() as conn:
        cur = await conn.execute(
            "UPDATE approvals SET status = 'expired' "
            "WHERE status = 'pending' AND expires_at < now()"
        )
        return cur.rowcount


# --- tool registry rows ------------------------------------------------------

async def upsert_tool_row(
    *,
    name: str,
    source: str,
    description: str,
    input_schema: dict,
    tags: list[str],
    risk: str,
    always_on: bool,
    desc_sha256: str,
    embedding: list[float] | None,
    embedding_model: str | None,
) -> None:
    async with connection() as conn:
        await conn.execute(
            """
            INSERT INTO tools (name, source, description, input_schema, tags, risk, always_on,
                               desc_sha256, embedding, embedding_model, updated_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s, now())
            ON CONFLICT (name) DO UPDATE SET
              source = EXCLUDED.source, description = EXCLUDED.description,
              input_schema = EXCLUDED.input_schema, tags = EXCLUDED.tags, risk = EXCLUDED.risk,
              always_on = EXCLUDED.always_on, desc_sha256 = EXCLUDED.desc_sha256,
              embedding = COALESCE(EXCLUDED.embedding, tools.embedding),
              embedding_model = COALESCE(EXCLUDED.embedding_model, tools.embedding_model),
              updated_at = now()
            """,
            (
                name, source, description, json.dumps(input_schema), tags, risk, always_on,
                desc_sha256, embedding, embedding_model,
            ),
        )


async def tool_rows() -> list[dict]:
    return await fetch_all("SELECT * FROM tools ORDER BY name")


async def tools_by_similarity(vec: list[float], limit: int = 8) -> list[dict]:
    return await fetch_all(
        """
        SELECT name, 1 - (embedding <=> %s::vector) AS score
        FROM tools
        WHERE enabled AND embedding IS NOT NULL
        ORDER BY embedding <=> %s::vector
        LIMIT %s
        """,
        (vec, vec, limit),
    )


# --- runs and daemon status --------------------------------------------------

async def start_run(kind: str, session_id: UUID | None = None) -> UUID:
    rid = uuid7()
    async with connection() as conn:
        await conn.execute(
            "INSERT INTO consolidation_runs (id, kind, session_id) VALUES (%s, %s, %s)",
            (rid, kind, session_id),
        )
    return rid


async def finish_run(
    run_id: UUID, status: str, stats: dict, md_commit: str | None = None,
    error: str | None = None,
) -> None:
    async with connection() as conn:
        await conn.execute(
            """
            UPDATE consolidation_runs
            SET finished_at = now(), status = %s, stats = %s, md_commit = %s, error = %s
            WHERE id = %s
            """,
            (status, json.dumps(stats, default=str), md_commit, error, run_id),
        )


async def heartbeat(name: str, pid: int, host: str, info: dict | None = None) -> None:
    async with connection() as conn:
        await conn.execute(
            """
            INSERT INTO daemon_status (name, pid, host, started_at, heartbeat_at, info)
            VALUES (%s, %s, %s, now(), now(), %s)
            ON CONFLICT (name) DO UPDATE SET
              pid = EXCLUDED.pid, host = EXCLUDED.host, heartbeat_at = now(),
              info = EXCLUDED.info
            """,
            (name, pid, host, json.dumps(info or {}, default=str)),
        )


async def daemon_status(name: str = "daemon") -> dict | None:
    return await fetch_one("SELECT * FROM daemon_status WHERE name = %s", (name,))
