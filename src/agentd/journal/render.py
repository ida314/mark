"""Turning journal events into lines a human reads. One renderer, both frontends.

The CLI and the Telegram channel show the same thing to the same person, so the mapping
from "what the runtime recorded" to "what that looks like" lives once. Before session 2c
each frontend rendered its own in-memory event objects, which is how two surfaces drift
apart on what they show of somebody's words.

**An event type this module does not know about renders as nothing, never as an error.**
`memory/retrieval.py` has the cautionary version of this: a `kind` missing from a dict
literal raises `KeyError` inside a `try/except` that degrades to a notice, so the whole
panel disappears silently. Here the lookup has no default branch to get wrong - the eight
event types no pass writes yet simply have no renderer, and `RENDERED_TYPES` says which do
so that the gap is data rather than a comment that goes stale.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from ..tools.base import flat
from .store import Event

ARG_VALUE_CHARS = 60
ARG_LINE_CHARS = 120


def brief_args(args: dict[str, Any]) -> str:
    """A tool call's arguments, short enough to sit on one line of a status line.

    `flat` is not cosmetic. The model's arguments are routinely built out of something a
    stranger wrote - the subject it is searching for, the statement it is proposing to
    remember - and every renderer of this string puts one call per line. A value carrying a
    newline would forge a line that looks like a call this code made.

    `reason` is skipped: the executor requires it on every write, and it is a sentence
    written for the audit row rather than a field worth reading twice on a phone.
    """
    parts = []
    for key, value in args.items():
        if key == "reason":
            continue
        text = flat(str(value), ARG_VALUE_CHARS + 1)
        parts.append(f"{key}={text[:ARG_VALUE_CHARS]}{'…' if len(text) > ARG_VALUE_CHARS else ''}")
    return ", ".join(parts)[:ARG_LINE_CHARS]


@dataclass(frozen=True)
class Line:
    """One rendered event. `style` is a rich style name; "" means plain."""

    text: str
    style: str = ""


def _tool_requested(payload: dict[str, Any]) -> Line:
    # The request rather than `tool_started`, because this is the one that carries the
    # arguments, and two lines per call is noise. The pair matters to a fold, not to a
    # person watching.
    return Line(f"→ {payload['name']}({brief_args(payload['args'])})", "dim")


def _tool_progress(payload: dict[str, Any]) -> Line:
    return Line(f"  … {payload['name']}: {payload['message']}", "dim")


def _tool_finished(payload: dict[str, Any]) -> Line:
    return Line(f"  ✓ {payload.get('summary') or payload['name']}", "green")


def _tool_failed(payload: dict[str, Any]) -> Line:
    denied = bool(payload["denied"])
    return Line(
        f"  {'✗' if denied else '!'} {payload['error'] or payload['name']}",
        "yellow" if denied else "red",
    )


def _worker_created(payload: dict[str, Any]) -> Line:
    return Line(f"→ delegating to {payload['name']}", "magenta")


def _worker_finished(payload: dict[str, Any]) -> Line:
    return Line(f"  {payload['name']}: {payload['status']}", "magenta")


def _message_appended(payload: dict[str, Any]) -> Line | None:
    # Only the mid-turn system nudges, which are the ones that explain a change in the
    # agent's behaviour the user would otherwise see as inconsistency: a tool withdrawn for
    # the rest of the turn, or the step budget being spent. The assembled system block has
    # no `step_id` and is not shown - it is 5kB of instructions the user did not write - and
    # the user's own message and the assistant's prose are already on screen.
    if payload["role"] != "system" or payload.get("step_id") is None:
        return None
    return Line(f"· {payload['preview']}", "dim")


def _checkpoint_written(payload: dict[str, Any]) -> Line:
    # Shown because a checkpoint is the runtime promising it could resume from here, and
    # somebody running with `[checkpoints] enabled` is running with it on in order to watch
    # that happen. `covers_seq` and not the event's own position: the number that says how
    # much of the run the snapshot accounts for.
    return Line(f"· checkpoint {payload['trigger']} @ seq {payload['covers_seq']}", "dim")


def _run_resumed(payload: dict[str, Any]) -> Line:
    # Shown, unlike the rest of the types the recovery passes write, because a resume is a
    # change in what the agent is doing that the person watching did not ask for in this
    # turn. The count of uncertain effects is on the line rather than left to a follow-up
    # command: "I picked this run back up" without "and two calls may already have happened"
    # is the half of the sentence that reads as reassurance.
    uncertain = len(payload["uncertain_effects"])
    tail = f", {uncertain} effect{'' if uncertain == 1 else 's'} uncertain" if uncertain else ""
    return Line(f"· resumed from seq {payload['from_seq']}{tail}", "yellow" if uncertain else "dim")


def _agent_finished(payload: dict[str, Any]) -> Line | None:
    # A turn that failed is the one case where the journal knows something the prose stream
    # cannot say: `run_turn` yields no answer at all when the model call raised.
    if payload["status"] == "failed":
        return Line(f"turn failed: {payload.get('error') or 'no reason recorded'}", "red")
    return None


_RENDERERS: dict[str, Callable[[dict[str, Any]], Line | None]] = {
    "tool_requested": _tool_requested,
    "tool_progress": _tool_progress,
    "tool_finished": _tool_finished,
    "tool_failed": _tool_failed,
    "worker_created": _worker_created,
    "worker_finished": _worker_finished,
    "message_appended": _message_appended,
    "checkpoint_written": _checkpoint_written,
    "run_resumed": _run_resumed,
    "agent_finished": _agent_finished,
}

# Which types have a human rendering at all. The rest - `agent_started`, `tool_started`, the
# `effect_*` pair and the types Passes 4d and 5 write - are facts a fold wants and a person
# does not, and are skipped rather than given an invented line.
RENDERED_TYPES: frozenset[str] = frozenset(_RENDERERS)


def render_event(event: Event) -> Line | None:
    """One line for one event, or None when there is nothing for a human in it."""
    renderer = _RENDERERS.get(event.type)
    if renderer is None:
        return None
    return renderer(event.payload)


__all__ = [
    "ARG_LINE_CHARS",
    "ARG_VALUE_CHARS",
    "RENDERED_TYPES",
    "Line",
    "brief_args",
    "render_event",
]
