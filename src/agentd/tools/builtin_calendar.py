"""The calendar the daemon already fetched, read back without touching Google.

`connectors/gcal.py` polls every `poll_interval_s` (120s by default) over a
`horizon_days` window and archives each event as a `raw_events` row. Until now nothing
read those rows back except `daemon/heartbeat.situation_report`, so the agent could be
asked what its own database already knew and answer "I have no calendar tool".

This is a SELECT, not a second ingestion path. Google is not called, no credential is
touched, no scope is checked, and there is nothing to rate-limit - which is the whole
argument for reading the archive rather than adding a live Calendar tool: the poll is
already happening, and paying for it twice buys 120 seconds of freshness at the cost of a
second auth surface and a network round trip in the middle of a turn.

**Empty-because-free and empty-because-broken must not render the same way.** A tool over
a store somebody else fills has one serious failure mode: the daemon stops, the query
returns zero rows, and the agent says "you have nothing scheduled this week" - fluent,
confident, wrong. That is the same shape as every other bug this codebase has had (a
failure degrading to a plausible NULL), so freshness is not a decoration on the result, it
is part of the answer. Every reply carries the age of the data, a feed that has fallen
behind says so *instead of* reporting an empty calendar, and a connector that is disabled
or has never polled is named as such.

Two deliberate trust choices:

- **`trust_output=False`.** An event summary was written by whoever sent the invitation,
  so it is exactly the injection boundary an open-loop title is. The heartbeat handles this
  by rendering counts and clock times only, because it interpolates into a prompt directly.
  A tool does not have to be that austere: it returns through the executor, which wraps the
  whole result in `<untrusted_content>` and seals it. That is what buys the summaries.
- **`private_output=False`, unlike the Gmail tools.** Reading your mail shuts the egress
  door for the rest of the session, and that is right for a mailbox: the model chooses what
  to fetch, live, from an adversarial corpus. This reads rows the daemon already chose,
  already archived and already shows in the heartbeat without an interlock, and the fields
  it renders are clock times, a location and a summary - no descriptions, no attachments,
  no bodies. Shutting off web and writes because somebody asked what time their seminar
  starts is approval fatigue pointed at the wrong door. Flip the flag on the decorator if
  you disagree; nothing else has to change.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from ..config import Config, GoogleAccountConfig
from ..db import repo_archive, repo_connectors
from ..ids import utcnow
from .base import ToolContext, ToolResult, describe_age, feed_health, flat, obj, tool
from .effects import UNAUDITED

MAX_EVENTS = 50
DEFAULT_HOURS = 24
TITLE_CHARS = 120
LOCATION_CHARS = 80  # a street address that fits is worth the extra column


def calendar_accounts(cfg: Config) -> dict[str, GoogleAccountConfig]:
    return {
        label: account
        for label, account in sorted(cfg.connectors.google.accounts.items())
        if account.calendar and account.address
    }


def resolve_calendar(cfg: Config, label: str | None) -> tuple[str, GoogleAccountConfig] | str:
    """(label, account) for the calendar to read, or a sentence explaining why not.

    Prose rather than an exception, the same convention `builtin_mail.resolve_account`
    uses: "which calendar did you mean" is a question the model can usually answer itself
    if it is told what the options are.
    """
    accounts = calendar_accounts(cfg)
    if not accounts:
        return (
            "No calendar is configured. Set `calendar = true` on a Google account under "
            "[connectors.google.accounts] and run `agent connectors auth <label>`."
        )
    if label:
        if label in accounts:
            return label, accounts[label]
        return (
            f"No calendar account labelled {label!r}. Configured: "
            f"{', '.join(accounts)}."
        )
    if len(accounts) > 1:
        return f"Several calendars are configured ({', '.join(accounts)}); say which one."
    only = next(iter(accounts.items()))
    return only


def _moment(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


def one_line(row: dict[str, Any], now: datetime) -> str:
    """One event, in the house style. Every field in it was written by somebody else."""
    payload = row.get("payload") or {}
    start = row["occurred_at"].astimezone()
    end = _moment(payload.get("end"))
    # `source_title` is where `connectors/base.ingest` files the item's own title. Not the
    # `content` column, which for a calendar event is the *description* - somebody else's
    # free prose, often long, and not what "what is on my calendar" is asking for.
    title = flat(payload.get("source_title"), TITLE_CHARS) or "(untitled)"

    when = f"{start:%a %d %b}"
    if payload.get("all_day"):
        when += " (all day)"
    else:
        when += f" {start:%H:%M}"
        if end is not None:
            local_end = end.astimezone()
            when += f"–{local_end:%H:%M}" if local_end.date() == start.date() else "–…"

    extras = []
    location = flat(payload.get("location") or "", LOCATION_CHARS)
    if location:
        extras.append(location)
    attendees = payload.get("attendees")
    if isinstance(attendees, int) and attendees > 1:
        extras.append(f"{attendees} attendees")
    tail = f"  ({'; '.join(extras)})" if extras else ""
    return f"- {when}  {title}{tail}"


@tool(
    "calendar_upcoming",
    "The user's own calendar: what is scheduled in the next N hours, with start times, "
    "titles and locations. Reads the copy the daemon keeps in sync, so it is fast and "
    "needs no network, and every answer says how fresh it is. Covers upcoming events only "
    "— it cannot search the past or look beyond the sync horizon.",
    obj(
        hours={
            "type": "integer",
            "description": f"How far ahead to look. Default {DEFAULT_HOURS}; 168 is a week.",
        },
        account={"type": "string", "description": "calendar account label, e.g. nyu"},
    ),
    tags=("calendar", "untrusted"),
    always_on=True,
    trust_output=False,
    effect_class=UNAUDITED,
)
async def calendar_upcoming(args: dict, ctx: ToolContext) -> ToolResult:
    # Imported here, not at module scope, for the same reason `builtin_mail` does it:
    # the test fixture swaps `get_config` after this module is already imported.
    from ..config import get_config

    cfg = get_config()
    resolved = resolve_calendar(cfg, args.get("account"))
    if isinstance(resolved, str):
        return ToolResult(content=resolved, ok=False)
    label, account = resolved

    horizon_hours = account.horizon_days * 24
    try:
        hours = int(args.get("hours", DEFAULT_HOURS))
    except (TypeError, ValueError):
        hours = DEFAULT_HOURS
    hours = max(1, min(hours, horizon_hours))

    now = utcnow()
    rows = await repo_archive.upcoming_events(f"gcal-{label}.%", hours, MAX_EVENTS)
    state = await repo_connectors.load_state(f"gcal-{label}")
    freshness, healthy = feed_health(state, account, now)

    window = f"the next {hours}h" if hours < 48 else f"the next {round(hours / 24)} days"
    if not rows:
        # The whole point of the freshness check. An empty window under a broken feed is
        # not an empty calendar, and saying so is the difference between "you are free"
        # and "I cannot see your calendar".
        if not healthy:
            return ToolResult(
                content=(
                    f"Cannot say what is in {window}: {freshness}. The archived copy is "
                    f"the only calendar source, so this is not the same as being free."
                ),
                ok=False,
                data={"stale": True, "account": label},
            )
        return ToolResult(
            content=f"Nothing scheduled in {window} ({freshness}).",
            data={"count": 0, "account": label},
        )

    header = f"{len(rows)} event(s) in {window} for {label} ({freshness})"
    if not healthy:
        header += ". Treat this as a partial list: more may have been scheduled since"
    body = "\n".join(one_line(row, now) for row in rows)
    truncated = f"\n(only the first {MAX_EVENTS} are shown)" if len(rows) == MAX_EVENTS else ""
    return ToolResult(
        content=f"{header}:\n{body}{truncated}",
        trust="untrusted",
        data={"count": len(rows), "account": label, "stale": not healthy},
    )


TOOLS = [calendar_upcoming]

# `describe_age` and `feed_health` moved to `base` once coursework became a second caller.
# Re-exported here so `builtin_calendar.feed_health` keeps resolving for anything that
# learned the name from this module.
__all__ = [
    "TOOLS", "calendar_upcoming", "describe_age", "feed_health", "one_line", "resolve_calendar",
]
