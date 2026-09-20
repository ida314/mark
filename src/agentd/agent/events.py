"""UI events emitted by a turn. The CLI renders these; the daemon logs them."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..tools.base import flat

ARG_VALUE_CHARS = 60
ARG_LINE_CHARS = 120


def brief_args(args: dict[str, Any]) -> str:
    """A tool call's arguments, short enough to sit on one line of a status line.

    Lives here rather than in one renderer because the second caller proved it general: the
    CLI prints this beside a tool name and so does the Telegram progress message, and two
    copies of the rule about how much of somebody's words to show is how the two drift.

    `flat` is not cosmetic here. The model's arguments are routinely built out of something
    a stranger wrote - the subject it is searching for, the statement it is proposing to
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


@dataclass
class TextChunk:
    text: str


@dataclass
class ThinkingChunk:
    text: str


@dataclass
class ToolStarted:
    name: str
    args: dict[str, Any]


@dataclass
class ToolFinished:
    name: str
    ok: bool
    summary: str
    denied: bool = False


@dataclass
class SubagentStarted:
    name: str
    task: str


@dataclass
class SubagentFinished:
    name: str
    status: str
    summary: str


@dataclass
class Notice:
    text: str
    level: str = "info"


@dataclass
class TurnFinished:
    turn_id: str
    text: str
    steps: int
    usage: dict[str, int] = field(default_factory=dict)


UIEvent = (
    TextChunk
    | ThinkingChunk
    | ToolStarted
    | ToolFinished
    | SubagentStarted
    | SubagentFinished
    | Notice
    | TurnFinished
)
