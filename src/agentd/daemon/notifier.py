"""Getting a notification off this box and onto your phone.

Until now the only subscriber to `agent_inbox` was a running `agent chat`, so anything the
daemon raised while no terminal was open was lost — which makes proactive work pointless.

Three things this has to get right, none of them obvious:

- **`pg_notify` is fire-and-forget.** A row inserted while nobody was LISTENing is never
  replayed. So the loop drains unpushed rows on start *before* subscribing, and a slow tick
  re-drains in case the LISTEN connection dies quietly.
- **It must never call `repo_agenda.notify()`.** This loop subscribes to the channel that
  function writes to; notifying about a delivery failure would be a feedback loop. Failures
  go to the audit table instead. (`supervise()` in `main.py` does notify on crash, which is
  bounded by its backoff but is worth knowing about.)
- **It does not go through `web_fetch`.** That tool's SSRF guard blocks loopback and CGNAT
  precisely so the agent cannot reach tailnet services, and ntfy is one. This is daemon code
  with its own client; the guard stays as it is.
"""

from __future__ import annotations

import asyncio

import httpx

from ..config import Config
from ..db import repo_agenda, repo_ops
from ..db.pool import connection, listen
from ..memory.review import contains_secret
from .heartbeat import in_quiet_hours

PRIORITY = {"info": 3, "warn": 4, "error": 5}
TAGS = {"info": "information_source", "warn": "warning", "error": "rotating_light"}
LEVEL_ORDER = ["info", "warn", "error"]
MAX_ATTEMPTS = 6
BODY_LIMIT = 1500


def _redact(text: str | None) -> str:
    """The archive keeps what it was given; what leaves the box does not."""
    if not text:
        return ""
    return "[redacted: looks like a credential]" if contains_secret(text) else text[:BODY_LIMIT]


async def _unpushed(limit: int = 50) -> list[dict]:
    async with connection() as conn:
        cur = await conn.execute(
            """
            SELECT * FROM notifications
            WHERE pushed_at IS NULL AND push_attempts < %s
            ORDER BY id LIMIT %s
            """,
            (MAX_ATTEMPTS, limit),
        )
        return list(await cur.fetchall())


async def _claim(notification_id: int) -> bool:
    """At most once per row, even with a catch-up drain and a live NOTIFY racing."""
    async with connection() as conn:
        cur = await conn.execute(
            "UPDATE notifications SET pushed_at = now() WHERE id = %s AND pushed_at IS NULL "
            "RETURNING id",
            (notification_id,),
        )
        return await cur.fetchone() is not None


async def _record_attempt(notification_id: int) -> None:
    async with connection() as conn:
        await conn.execute(
            "UPDATE notifications SET push_attempts = push_attempts + 1 WHERE id = %s",
            (notification_id,),
        )


def quiet_window(cfg: Config) -> tuple[int, int]:
    """The hours push may not wake you. Falls back to the daemon's window, so a config
    written before [ntfy] quiet_hours existed keeps behaving exactly as it did."""
    if cfg.ntfy.quiet_hours is not None:
        return tuple(cfg.ntfy.quiet_hours)
    return tuple(cfg.daemon.quiet_hours)


def should_send(row: dict, cfg: Config) -> bool:
    level = row.get("level", "info")
    if LEVEL_ORDER.index(level) < LEVEL_ORDER.index(cfg.ntfy.min_level):
        return False
    from ..ids import utcnow

    if in_quiet_hours(utcnow().astimezone(), quiet_window(cfg)):
        # Held, not dropped: pushed_at stays NULL and the safety tick delivers it in the morning.
        return level == "error"
    return True


def why_held(level: str, cfg: Config) -> str | None:
    """None when a notification at this level leaves the box now; otherwise a sentence
    saying what happens to it instead.

    This exists because `notify_user` used to answer "Notification sent." whatever the
    truth was — it writes a row, and the row is all it knows. Whoever reads that then has
    to explain an absence with no evidence, which is exactly the situation a model fills in
    with a plausible story. The three reasons a push does not happen are all knowable at
    the moment the row is written, so they are said then.
    """
    if not cfg.ntfy.enabled:
        return "push is off (ntfy.enabled = false), so it stays in `agent notifications`"
    if LEVEL_ORDER.index(level) < LEVEL_ORDER.index(cfg.ntfy.min_level):
        return (
            f"ntfy.min_level is {cfg.ntfy.min_level}, so {level} notifications are never pushed"
        )
    from ..ids import utcnow

    start, end = quiet_window(cfg)
    if in_quiet_hours(utcnow().astimezone(), (start, end)) and level != "error":
        return (
            f"quiet hours ({start:02d}:00-{end:02d}:00) hold everything below error, "
            f"so it reaches your phone at {end:02d}:00"
        )
    return None


def _headers(row: dict, cfg: Config) -> dict[str, str]:
    level = row.get("level", "info")
    headers = {
        # Redacted and flattened like the body. It was neither: `_redact` only ever saw
        # `body`, so a credential in a title left the box intact - and a newline in an HTTP
        # header value is header injection rather than a cosmetic problem.
        "Title": _redact(" ".join(str(row.get("title") or "agent").split()))[:200] or "agent",
        "Priority": str(PRIORITY.get(level, 3)),
        "Tags": TAGS.get(level, "information_source"),
        "Markdown": "yes",
    }
    from .. import secrets as vault

    token = vault.get("ntfy/default", "token")
    if token:
        headers["Authorization"] = f"Bearer {token.reveal()}"
    return headers


def _body(row: dict) -> str:
    body = _redact(row.get("body"))
    ref = row.get("ref") or {}
    approval = ref.get("approval")
    if approval:
        # There is no HTTP surface on this box to hang an action button off, so the next step
        # is a command you can paste.
        body = (body + "\n\n" if body else "") + f"agent approvals approve {approval}"
    return body or str(row.get("title", ""))


async def push(row: dict, cfg: Config, client: httpx.AsyncClient) -> bool:
    url = f"{cfg.ntfy.base_url.rstrip('/')}/{cfg.ntfy.topic}"
    response = await client.post(
        url, content=_body(row).encode(), headers=_headers(row, cfg), timeout=cfg.ntfy.timeout_s
    )
    return response.status_code < 300


async def _deliver(row: dict, cfg: Config, client: httpx.AsyncClient) -> None:
    if not should_send(row, cfg):
        return
    try:
        ok = await push(row, cfg, client)
    except Exception as exc:
        await _record_attempt(row["id"])
        await repo_ops.write_action(
            repo_ops.ActionRecord(
                actor="daemon", kind="push", name="ntfy", status="error", error=str(exc)[:300],
                refs={"notification": row["id"]},
            )
        )
        return
    if ok:
        await _claim(row["id"])
    else:
        await _record_attempt(row["id"])


async def notifier_loop(cfg: Config, stop: asyncio.Event) -> None:
    if not cfg.ntfy.enabled:
        await stop.wait()
        return

    async with httpx.AsyncClient(follow_redirects=False) as client:

        async def drain() -> None:
            for row in await _unpushed():
                await _deliver(row, cfg, client)

        await drain()  # anything raised while the daemon was down

        async def tick() -> None:
            while not stop.is_set():
                try:
                    await asyncio.wait_for(stop.wait(), timeout=cfg.ntfy.sweep_s)
                    return
                except TimeoutError:
                    pass
                try:
                    await drain()
                except Exception:
                    pass  # the LISTEN path is the primary; this is the safety net

        sweeper = asyncio.create_task(tick())
        try:
            async for payload in listen("agent_inbox"):
                if stop.is_set():
                    break
                try:
                    row = await repo_agenda.get_notification(int(payload))
                except (TypeError, ValueError):
                    continue
                if row and row.get("pushed_at") is None:
                    await _deliver(row, cfg, client)
        finally:
            sweeper.cancel()
            await asyncio.gather(sweeper, return_exceptions=True)
