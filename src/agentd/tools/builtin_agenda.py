"""Goals, open loops, reminders, watchers, notifications: the agent's own agenda."""

from __future__ import annotations

import json
from uuid import UUID

from ..config import Config
from ..db import repo_agenda, repo_connectors
from ..ids import parse_when, utcnow
from .base import Polled, Tool, ToolContext, ToolResult, feed_health, obj, required, tool
from .effects import IDEMPOTENT_WRITE, READ, UNSAFE_WRITE

# --- how much a loop can be believed -----------------------------------------
#
# A loop opened by a connector closes when that connector next sweeps and finds the thing
# gone from the source. So a feed that is switched off - a rejected credential parks one
# permanently - keeps every loop it ever opened at `open`, long after they were answered.
# That is the calendar's empty-window bug wearing different clothes: open-because-waiting
# and open-because-blind must not render the same way, so the freshness of the feed is part
# of the answer here too.


def feed_config(cfg: Config, connector: str) -> Polled | None:
    """The config entry a connector name refers to, or None if the config no longer has one.

    Names are composed in `connectors/__init__.py` as `<service>-<label>`, or the bare
    service where there can only ever be one of them. Resolved through `Config` rather than
    through `all_connectors()` on purpose: building a connector object inside a tool would
    put a credential-holding object on the agent's side of the fence to obtain one float.
    """
    service, _, label = connector.partition("-")
    if label and service in ("gmail", "gcal"):
        return cfg.connectors.google.accounts.get(label)
    if label and service == "imap":
        return cfg.connectors.imap.accounts.get(label)
    if connector == "github":
        return cfg.connectors.github
    if connector == "brightspace":
        return cfg.connectors.brightspace
    return None


async def feed_caveats(rows: list[dict]) -> list[str]:
    """One line for each feed in `rows` that cannot currently be believed.

    Silent when every feed is healthy, and silent for a connector the config no longer
    declares - there is no honest staleness threshold for a feed whose cadence is unknown,
    and a guessed one would cry stale forever.
    """
    named = sorted({row["connector"] for row in rows if row.get("connector")})
    if not named:
        return []
    # Imported here, not at module scope, for the same reason `builtin_calendar` does it:
    # the test fixture swaps `get_config` after this module is already imported.
    from ..config import get_config

    cfg = get_config()
    now = utcnow()
    states = {row["name"]: row for row in await repo_connectors.list_state()}
    out: list[str] = []
    for name in named:
        source = feed_config(cfg, name)
        if source is None:
            continue
        reading, healthy = feed_health(states.get(name, {}), source, now, noun=name)
        if not healthy:
            out.append(f"{reading}, so loops it opened may already be resolved.")
    return out


@tool(
    "goals_list",
    "List the user's current goals and their next steps.",
    obj(status={"type": "string", "enum": ["active", "paused", "done", "dropped"]}),
    tags=("agenda", "core"),
    always_on=True,
    effect_class=READ,
)
async def goals_list(args: dict, ctx: ToolContext) -> ToolResult:
    rows = await repo_agenda.list_goals(args.get("status", "active"))
    if not rows:
        return ToolResult(content="No goals recorded.")
    lines = []
    for g in rows:
        due = f", due {g['due_at']:%Y-%m-%d}" if g.get("due_at") else ""
        step = f"\n    next: {g['next_step']}" if g.get("next_step") else ""
        lines.append(f"- [{g['slug']}] {g['title']} (p{g['priority']}, {g['horizon']}{due}){step}")
    return ToolResult(content="\n".join(lines))


@tool(
    "goal_upsert",
    "Create or update a goal.",
    required(
        obj(
            title={"type": "string"},
            slug={"type": "string"},
            description={"type": "string"},
            status={"type": "string", "enum": ["active", "paused", "done", "dropped"]},
            horizon={
                "type": "string",
                "enum": ["today", "week", "month", "quarter", "year", "life"],
            },
            priority={"type": "integer", "minimum": 1, "maximum": 5},
            due_at={"type": "string"},
            next_step={"type": "string"},
        ),
        "title",
    ),
    risk="draft",
    tags=("agenda",),
    # idempotent_write: keyed on `slug`, which defaults to slugify(title), so a
    # replay of the same arguments lands on the same row (ON CONFLICT DO UPDATE).
    effect_class=IDEMPOTENT_WRITE,
)
async def goal_upsert(args: dict, ctx: ToolContext) -> ToolResult:
    goal_id = await repo_agenda.upsert_goal(
        title=args["title"],
        slug=args.get("slug"),
        description=args.get("description"),
        status=args.get("status", "active"),
        horizon=args.get("horizon", "quarter"),
        priority=int(args.get("priority", 3)),
        due_at=parse_when(args["due_at"]) if args.get("due_at") else None,
        next_step=args.get("next_step"),
    )
    return ToolResult(content=f"Goal saved ({goal_id}).")


@tool(
    "open_loop_add",
    "Track an unfinished thread so it is not forgotten.",
    required(
        obj(
            title={"type": "string"},
            detail={"type": "string"},
            due_at={"type": "string"},
            waiting_on={"type": "string"},
        ),
        "title",
    ),
    risk="draft",
    tags=("agenda",),
    # unsafe_write: a fresh uuid7 per call and no dedup key, so a replay opens a
    # second loop the user then has to close twice.
    effect_class=UNSAFE_WRITE,
)
async def open_loop_add(args: dict, ctx: ToolContext) -> ToolResult:
    loop_id = await repo_agenda.add_open_loop(
        title=args["title"],
        detail=args.get("detail"),
        due_at=parse_when(args["due_at"]) if args.get("due_at") else None,
        waiting_on=args.get("waiting_on"),
    )
    return ToolResult(content=f"Open loop tracked ({loop_id}).")


@tool(
    "open_loops_list",
    "List open loops (unfinished threads). Each one records something that was true when it "
    "was noticed, not a live reading: where a loop names the feed it came from, that is "
    "where to check whether it is still true before acting on it.",
    obj(status={"type": "string", "enum": ["open", "waiting", "closed"]}),
    tags=("agenda", "core"),
    effect_class=READ,
)
async def open_loops_list(args: dict, ctx: ToolContext) -> ToolResult:
    rows = await repo_agenda.list_open_loops_with_source(args.get("status", "open"))
    if not rows:
        return ToolResult(content="No open loops.")
    # The connector name and nothing else. `detail` and the archived `source_title` hold
    # their words - see `connectors/mail.py:detail` - and this result is not wrapped in
    # <untrusted_content>. The external id stays out too: it is an implementation detail of
    # somebody else's service, and it belongs in an explicit dereference, not in a listing.
    lines = [
        f"- {r['title']}"
        + (f" (due {r['due_at']:%Y-%m-%d})" if r.get("due_at") else "")
        + (f" waiting on {r['waiting_on']}" if r.get("waiting_on") else "")
        + f" [{r['id']}]"
        + (f" from {r['connector']}" if r.get("connector") else "")
        for r in rows
    ]
    caveats = await feed_caveats(rows)
    return ToolResult(content="\n".join(lines + ([""] + caveats if caveats else [])))


@tool(
    "open_loop_close",
    "Close an open loop by its id.",
    required(obj(id={"type": "string"}), "id"),
    risk="draft",
    tags=("agenda",),
    # idempotent_write: UPDATE ... WHERE id, and closing a closed loop is a no-op.
    # The only thing a replay moves is `closed_at`.
    effect_class=IDEMPOTENT_WRITE,
)
async def open_loop_close(args: dict, ctx: ToolContext) -> ToolResult:
    try:
        await repo_agenda.close_open_loop(UUID(args["id"]))
    except ValueError:
        return ToolResult(content="That is not a valid loop id.", ok=False)
    return ToolResult(content="Closed.")


@tool(
    "reminder_set",
    "Remind the user about something at a time, or after a delay like '2h'.",
    required(obj(text={"type": "string"}, at={"type": "string"}), "text", "at"),
    risk="draft",
    tags=("agenda",),
    # unsafe_write: inserts a watcher that fires a notification at the user. A replay
    # is a second reminder, which is a real-world action nobody can take back.
    effect_class=UNSAFE_WRITE,
)
async def reminder_set(args: dict, ctx: ToolContext) -> ToolResult:
    when = parse_when(args["at"])
    if when is None:
        return ToolResult(content=f"Could not understand the time {args['at']!r}.", ok=False)
    await repo_agenda.add_watcher(
        name=f"reminder: {args['text'][:40]}",
        kind="once",
        spec={"at": when.isoformat()},
        action={"type": "notify", "text": args["text"]},
        created_by=ctx.actor,
        next_fire_at=when,
    )
    return ToolResult(content=f"Reminder set for {when.astimezone():%Y-%m-%d %H:%M %Z}.")


def _watcher_preview(args: dict) -> str:
    return (
        f"watcher '{args.get('name', 'unnamed')}' kind={args.get('kind')}\n"
        f"spec: {json.dumps(args.get('spec', {}))}\n"
        f"action: {args.get('action_type', 'notify')} -> {args.get('prompt', args.get('text', ''))}"
    )


@tool(
    "watcher_add",
    "Create a recurring or file-triggered watcher that can wake the agent later.",
    required(
        obj(
            name={"type": "string"},
            kind={"type": "string", "enum": ["once", "interval", "cron", "file"]},
            spec={
                "type": "object",
                "description": "{at} | {every_s} | {cron, tz} | {paths, debounce_s}",
            },
            action_type={"type": "string", "enum": ["notify", "agent"]},
            prompt={"type": "string", "description": "Notification text, or the agent's task"},
        ),
        "name",
        "kind",
        "spec",
        "prompt",
    ),
    risk="write",
    tags=("agenda",),
    preview=_watcher_preview,
    # unsafe_write: a replay is a second watcher, and an `agent` action watcher wakes
    # the agent to do arbitrary work on a schedule.
    effect_class=UNSAFE_WRITE,
)
async def watcher_add(args: dict, ctx: ToolContext) -> ToolResult:
    from ..daemon.scheduler import next_fire

    spec = args["spec"]
    kind = args["kind"]
    action_type = args.get("action_type", "notify")
    action = (
        {"type": "notify", "text": args["prompt"]}
        if action_type == "notify"
        else {"type": "agent", "prompt": args["prompt"]}
    )
    watcher_id = await repo_agenda.add_watcher(
        name=args["name"], kind=kind, spec=spec, action=action, created_by=ctx.actor,
        autonomy="observe", next_fire_at=next_fire(kind, spec, utcnow()),
    )
    return ToolResult(content=f"Watcher created ({watcher_id}).")


@tool(
    "notify_user",
    "Send the user a notification: it appears in their chat window and inbox.",
    required(obj(title={"type": "string"}, body={"type": "string"},
                 level={"type": "string", "enum": ["info", "warn", "error"]}), "title"),
    risk="draft",
    tags=("core",),
    always_on=True,
    # unsafe_write: the row is what the daemon pushes to chat and inbox, so a replay
    # is a second notification. `Queued` is not the same as `not yet real`.
    effect_class=UNSAFE_WRITE,
)
async def notify_user(args: dict, ctx: ToolContext) -> ToolResult:
    level = args.get("level", "info")
    await repo_agenda.notify(
        source=ctx.actor, title=args["title"], body=args.get("body"), level=level,
    )
    # Say what actually happens, not what was attempted. This tool writes a row; the daemon
    # decides whether it leaves the box, and "Notification sent." was a claim about neither.
    from ..config import get_config
    from ..daemon.notifier import why_held

    held = why_held(level, get_config())
    if held:
        return ToolResult(content=f"Queued, but not pushed: {held}.")
    return ToolResult(content="Queued; the daemon pushes it within a minute.")


TOOLS: list[Tool] = [
    goals_list, goal_upsert, open_loop_add, open_loops_list, open_loop_close,
    reminder_set, watcher_add, notify_user,
]
