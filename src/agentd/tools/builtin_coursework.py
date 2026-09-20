"""The coursework deadlines the daemon already fetched, read back without touching D2L.

`connectors/brightspace.py` polls the per-user iCal feed every `poll_interval_s` (1800s by
default) over a `horizon_days` window and archives each entry as a `raw_events` row of kind
`brightspace.assignment`. Some of those become open loops; the ones that do not - all-day
entries, anything `require` filtered out - are archived all the same and were, until now,
readable by nothing. "What is due this week" was a question this agent could only answer
from the loops it happened to have opened, which is a different and smaller question.

This is a SELECT, the same shape as `builtin_calendar`, and for the same reason: the poll
is already happening. Reading the archive costs no network round trip in the middle of a
turn, and - the part that matters here - **it never touches the feed URL**. That URL is a
credential (see the connector's docstring), so a tool that does not need it should not open
the vault to get it. Nothing in this module can leak a token it never reads.

**Empty-because-free and empty-because-broken must not render the same way.** Same failure
mode as the calendar, with sharper teeth: a missed seminar is an inconvenience and a missed
deadline is a grade. `feed_health` is therefore part of every answer, a feed that has fallen
behind says so *instead of* reporting an empty week, and the Brightspace cadence being 1800s
rather than 120s means "behind" here is measured in hours, not minutes.

Two deliberate scope limits, stated rather than discovered:

- **Upcoming only.** `repo_archive.upcoming_events` filters `occurred_at >= now()`, so a
  deadline that has passed is not returned even though the connector archived it (it ingests
  from `now - 12h`). "What did I miss" is a real question and this is not the tool for it;
  `open_loops_list` is, because an overdue loop is exactly what `overdue_loops()` tracks.
- **Deadlines only.** No announcements, no grades, no submission state. Those live behind
  Valence, which needs an application key a student cannot register.

Trust choices, both inherited from `builtin_calendar` and both deliberate:

- **`trust_output=False`.** An assignment title was written by an instructor, which is to
  say by somebody who is not the user. It returns through the executor, which wraps the
  result in `<untrusted_content>` and seals it - which is what buys rendering their words
  at all, rather than the counts-and-clock-times austerity the heartbeat needs.
- **`private_output=False`.** Course deadlines are not a mailbox. These are rows the daemon
  already chose and already archived, rendered as a due time, a course code and a title.
  Shutting the egress door for the rest of the session because somebody asked when their
  problem set is due is approval fatigue pointed at the wrong door.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from ..config import BrightspaceConnectorConfig, Config
from ..db import repo_archive, repo_connectors
from ..ids import utcnow
from .base import ToolContext, ToolResult, feed_health, flat, obj, tool
from .effects import UNAUDITED

STATE_NAME = "brightspace"  # one feed, so unlike gcal the state row carries no label
KIND_LIKE = "brightspace.%"
MAX_ITEMS = 50
DEFAULT_DAYS = 7
TITLE_CHARS = 120
COURSE_CHARS = 60


def resolve_brightspace(cfg: Config) -> BrightspaceConnectorConfig | str:
    """The feed's settings, or a sentence explaining why there are none.

    Prose rather than an exception, the convention `resolve_calendar` and
    `builtin_mail.resolve_account` both use. Deliberately checks only the config switch:
    whether the *credential* is present is the connector's business, and it records the
    answer in `connector_state.disabled_reason`, which `feed_health` reads back and quotes.
    Asking the vault here would duplicate that check and open the token for no reason.
    """
    if not cfg.connectors.enabled or not cfg.connectors.brightspace.enabled:
        return (
            "Brightspace is not enabled. Set `enabled = true` under [connectors.brightspace] "
            "in config.toml, then add the feed URL from Calendar -> Subscribe with "
            "`agent secrets set brightspace/<label> ics_url`."
        )
    return cfg.connectors.brightspace


def describe_until(seconds: float) -> str:
    """How much runway is left, which for a deadline is the part that changes behaviour.

    A date alone makes the reader do the arithmetic, and the reader here is a model that
    may not know today's date without asking. Coarse on purpose past a day: the difference
    between 49 and 53 hours does not change what anybody does next.
    """
    if seconds < 3600:
        return f"in {max(1, round(seconds / 60))} min"
    if seconds < 86400:
        return f"in {round(seconds / 3600)}h"
    days = round(seconds / 86400)
    return f"in {days} day" if days == 1 else f"in {days} days"


def course_of(payload: dict[str, Any]) -> str:
    """Which course this belongs to, from wherever this feed happens to put it.

    CATEGORIES is the field D2L documents for it and the field NYU's feed leaves empty; the
    course arrives in LOCATION instead ("MATH-UA 120.016 Discrete Mathematics, Fall 2026"),
    which is not what LOCATION means anywhere else and is not worth arguing with. Both are
    read, in that order, because another institution's feed may well do it the documented
    way and neither field costs anything to check.

    Rendered with `flat` rather than run through the connector's `course_code` extractor:
    that extractor exists because a loop title is interpolated into a prompt unquarantined,
    and this result is not. It also throws away everything but the code, and "Discrete
    Mathematics" is the half a reader recognises.
    """
    for field in ("categories", "location"):
        value = flat(payload.get(field) or "", COURSE_CHARS)
        # "Special Topics:" is what this feed offers for a course with no listed section.
        # A label that is only punctuation is noise in every row, so it is not a course.
        if value.strip().rstrip(":").strip():
            return value.strip().rstrip(":").strip()
    return ""


def one_line(row: dict[str, Any], now: datetime) -> str:
    """One deadline, in the house style. Every field in it was written by somebody else."""
    payload = row.get("payload") or {}
    due = row["occurred_at"].astimezone()
    # `source_title` is where `connectors/base.ingest` files the item's own title. Not
    # `content`, which for a feed entry is the *description* - an instructor's free prose,
    # often long, and not what "what is due" is asking for.
    title = flat(payload.get("source_title"), TITLE_CHARS) or "(untitled)"
    course = course_of(payload)

    when = f"{due:%a %d %b}"
    when += " (all day)" if payload.get("all_day") else f" {due:%H:%M}"
    left = describe_until((row["occurred_at"] - now).total_seconds())
    head = f"- {when}  ({left})  "
    return f"{head}{course} — {title}" if course else f"{head}{title}"


def matches(row: dict[str, Any], needle: str) -> bool:
    """Case-insensitive substring over the fields this tool actually renders.

    Over the rendered fields rather than the whole payload, so that what the filter matches
    is what the reader can see - a filter that silently hits a description nobody is shown
    looks like a bug from the outside.
    """
    payload = row.get("payload") or {}
    hay = f"{payload.get('source_title') or ''} {course_of(payload)}".lower()
    return needle.lower() in hay


@tool(
    "coursework_due",
    "The user's course deadlines: assignments, quizzes and exams due in the next N days, "
    "from the Brightspace calendar feed the daemon keeps in sync. Fast, needs no network, "
    "and every answer says how fresh it is. Upcoming only — it cannot show deadlines that "
    "have already passed, grades, announcements or whether something was submitted.",
    obj(
        days={
            "type": "integer",
            "description": f"How far ahead to look. Default {DEFAULT_DAYS}; 1 is today.",
        },
        course={
            "type": "string",
            "description": "Only deadlines whose course or title contains this, e.g. 'CSCI-UA 480'",
        },
    ),
    tags=("coursework", "untrusted"),
    always_on=True,
    trust_output=False,
    effect_class=UNAUDITED,
)
async def coursework_due(args: dict, ctx: ToolContext) -> ToolResult:
    # Imported here, not at module scope, for the same reason `builtin_calendar` does it:
    # the test fixture swaps `get_config` after this module is already imported.
    from ..config import get_config

    settings = resolve_brightspace(get_config())
    if isinstance(settings, str):
        return ToolResult(content=settings, ok=False)

    try:
        days = int(args.get("days", DEFAULT_DAYS))
    except (TypeError, ValueError):
        days = DEFAULT_DAYS
    # Clamped to what the poll actually covers: answering for day 60 of a 21-day window
    # would be reporting on days nobody has looked at.
    days = max(1, min(days, settings.horizon_days))
    course = str(args.get("course") or "").strip()

    now = utcnow()
    rows = await repo_archive.upcoming_events(KIND_LIKE, days * 24, MAX_ITEMS)
    state = await repo_connectors.load_state(STATE_NAME)
    freshness, healthy = feed_health(state, settings, now, noun="Brightspace")

    window = "today" if days == 1 else f"the next {days} days"
    scope = f" matching {course!r}" if course else ""
    if course:
        rows = [row for row in rows if matches(row, course)]

    if not rows:
        # The whole point of the freshness check. An empty window under a broken feed is not
        # an empty week, and saying so is the difference between "nothing is due" and "I
        # cannot see your deadlines".
        if not healthy:
            return ToolResult(
                content=(
                    f"Cannot say what is due in {window}: {freshness}. The archived feed is "
                    f"the only coursework source, so this is not the same as nothing being due."
                ),
                ok=False,
                data={"stale": True},
            )
        return ToolResult(
            content=f"Nothing due in {window}{scope} ({freshness}).",
            data={"count": 0},
        )

    header = f"{len(rows)} deadline(s) in {window}{scope} ({freshness})"
    if not healthy:
        header += ". Treat this as a partial list: more may have been set since"
    body = "\n".join(one_line(row, now) for row in rows)
    truncated = f"\n(only the first {MAX_ITEMS} are shown)" if len(rows) == MAX_ITEMS else ""
    return ToolResult(
        content=f"{header}:\n{body}{truncated}",
        trust="untrusted",
        data={"count": len(rows), "stale": not healthy},
    )


TOOLS = [coursework_due]

__all__ = [
    "TOOLS", "course_of", "coursework_due", "describe_until", "matches", "one_line",
    "resolve_brightspace",
]
