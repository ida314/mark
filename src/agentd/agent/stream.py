"""What a turn hands back to whoever is driving it: prose, and nothing else.

This module is what is left of `agent/events.py` after session 2c, and the thing that is
gone matters more than the thing that remains. There used to be an in-process event bus
here - `ToolStarted`, `ToolFinished`, `SubagentStarted`, `SubagentFinished`, `TurnFinished` -
carrying tool and worker state to the CLI and to Telegram while the journal recorded the
same facts under different names. Two paths for one truth: the frontend could show a tool
that the journal never recorded, a reconnecting frontend had nothing to replay, and
`SubagentStarted` was rendered by the CLI and emitted by nobody at all, which is the kind of
thing only a second path can hide.

Lifecycle state now has exactly one home. `journal/feed.py` is how anything subscribes to
it, including a process that was not running when the events were written.

**The rule for this module: it carries no agent, tool or worker state.** A `Delta` is the
model's own output as it arrives, and an `Answer` is the finished text. Neither is a fact
about execution - they are the conversation, which the journal deliberately keeps only a
200-character preview of. A `Notice` is a remark addressed to the human. If something here
ever needs to say what a tool is doing, it belongs in the journal and the frontend should
read it from the feed.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Delta:
    """A fragment of the model's output as it streams.

    `thinking` marks a reasoning delta, which is the model's scratch work rather than its
    answer: it is shown only when asked for and is not part of the archived text.
    """

    text: str
    thinking: bool = False


@dataclass(frozen=True)
class Notice:
    """Something the runtime wants to tell the human, in words, mid-turn.

    Not a lifecycle event and deliberately not journaled: there is one of these left in the
    codebase, and it reports that memory retrieval failed and the turn is continuing with
    less context than it should have. See the outcome record for why that condition is still
    invisible to a fold, and why inventing an eighteenth event type for it was not this
    session's call to make.
    """

    text: str
    level: str = "info"


@dataclass(frozen=True)
class Answer:
    """The turn's final text, yielded once, last.

    The journal says that a turn ended, how it ended, and how long it took; it keeps only a
    preview of what was said, because it is a record of what happened rather than a second
    copy of the conversation. So the full text comes back this way, and everything else a
    caller might want about the ending - status, steps, tokens - is read from
    `agent_finished` rather than duplicated here.
    """

    turn_id: str
    text: str


TurnStream = Delta | Notice | Answer

# Every type `run_turn` may yield. A guard test asserts a turn yields nothing outside this
# set, which is what stops the event bus growing back one convenient field at a time.
STREAM_TYPES: tuple[type, ...] = (Delta, Notice, Answer)


__all__ = ["STREAM_TYPES", "Answer", "Delta", "Notice", "TurnStream"]
