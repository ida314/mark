"""UI events emitted by a turn. The CLI renders these; the daemon logs them."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


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
