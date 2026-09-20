"""Tool definitions. A tool is data plus a handler; nothing here executes anything."""

from __future__ import annotations

import hashlib
import html as htmllib
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol
from uuid import UUID

from .effects import EffectClass, check_effect_class

Risk = str  # read | draft | write | external | destructive


@dataclass
class ToolContext:
    """Everything a handler is allowed to know about its caller."""

    session_id: UUID | None = None
    turn_id: UUID | None = None
    action_id: UUID | None = None
    # Where this call sits in the run journal. `run_id` is the orchestration run (the
    # top-level turn), `step_id` the position inside it; a worker's steps are scoped by its
    # worker id, so they cannot collide with its caller's. Both are None only where there is
    # genuinely no run - `policy/replay.execute_approved` runs a queued call long after the
    # turn that asked for it ended. Pass 3 hashes them into `idempotency_key`.
    run_id: str | None = None
    step_id: str | None = None
    actor: str = "main"
    origin: str = "interactive"
    autonomy: str = "assist"
    tainted: bool = False
    # Stronger than `tainted`, and deliberately separate. Taint says "untrusted text is in
    # context", which every web page raises; this says "the user's private data is in
    # context", which only a handful of tools can raise and which closes the egress door
    # rather than merely asking about it. Conflating them would mean fetching page 1 of a
    # search makes page 2 impossible.
    private: bool = False
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class ToolResult:
    content: str
    ok: bool = True
    trust: str = "trusted"  # 'untrusted' marks content the model must not obey
    data: dict[str, Any] = field(default_factory=dict)
    undo: dict[str, Any] | None = None
    candidates: list[dict[str, Any]] = field(default_factory=list)


Handler = Callable[[dict[str, Any], ToolContext], Awaitable[ToolResult]]
PreviewFn = Callable[[dict[str, Any]], str]


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict[str, Any]  # JSON Schema for the arguments
    handler: Handler
    # Whether re-running this call after a crash is safe: read | idempotent_write |
    # unsafe_write, defined in `effects.py`. Keyword-only and with no default, so the
    # question is answered where the tool is written rather than inferred later by
    # something that cannot know. Distinct from `risk`, which asks whether the user should
    # be *asked first*; two different questions with two different wrong answers.
    effect_class: EffectClass = field(kw_only=True)
    risk: Risk = "read"
    tags: tuple[str, ...] = ()
    path_args: tuple[str, ...] = ()
    always_on: bool = False
    source: str = "builtin"
    trust_output: bool = True
    # True for a tool that reads the user's own private data - mail today, a mailbox or a
    # calendar tomorrow. The loop raises ToolContext.private from this, and the policy's
    # hard denies key off it. It is not about trusting the *source*; a tool can be both
    # private_output (it read your mail) and trust_output=False (a stranger wrote it).
    private_output: bool = False
    preview: PreviewFn | None = None
    enabled: bool = True

    def __post_init__(self) -> None:
        check_effect_class(self.name, self.effect_class)

    @property
    def needs_reason(self) -> bool:
        """Write-or-worse tools must say why. That string is what `agent why` prints."""
        return self.risk in ("write", "external", "destructive")

    def openai_schema(self) -> dict[str, Any]:
        params = json.loads(json.dumps(self.parameters))  # deep copy
        if self.needs_reason:
            params.setdefault("properties", {})["reason"] = {
                "type": "string",
                "description": "Why this action is needed, in one sentence, for the user's log.",
            }
            required = params.setdefault("required", [])
            if "reason" not in required:
                required.append("reason")
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": params,
            },
        }

    def embed_text(self) -> str:
        return f"{self.name}: {self.description} [tags: {', '.join(self.tags)}]"

    def desc_sha256(self) -> str:
        return hashlib.sha256(self.embed_text().encode()).hexdigest()


def tool(
    name: str,
    description: str,
    parameters: dict[str, Any],
    *,
    effect_class: EffectClass,
    risk: Risk = "read",
    tags: tuple[str, ...] = (),
    path_args: tuple[str, ...] = (),
    always_on: bool = False,
    trust_output: bool = True,
    private_output: bool = False,
    preview: PreviewFn | None = None,
) -> Callable[[Handler], Tool]:
    def decorate(handler: Handler) -> Tool:
        return Tool(
            name=name,
            description=description,
            parameters=parameters,
            handler=handler,
            effect_class=effect_class,
            risk=risk,
            tags=tags,
            path_args=path_args,
            always_on=always_on,
            trust_output=trust_output,
            private_output=private_output,
            preview=preview,
        )

    return decorate


def flat(value: str | None, limit: int) -> str:
    """Their field, on one line.

    Not cosmetic. Values from outside are rendered as `- ` bullets, and a subject or an
    event summary containing a newline followed by `- ...` forges an entry that looks like
    one this code wrote. Collapsing whitespace is what makes a bullet list a structure the
    model can trust even though every value in it is a stranger's.

    Lives here rather than in one tool module because the second caller proved it general:
    a mail subject and a calendar summary are the same problem, written by the same kind of
    stranger. Unescaping happens after the split, so whatever it produces is still one line,
    and the executor seals the one tag sequence that would matter.
    """
    return htmllib.unescape(" ".join(str(value or "").split()))[:limit]


def obj(**properties: Any) -> dict[str, Any]:
    """Shorthand for a JSON-Schema object with no required fields."""
    return {"type": "object", "properties": properties}


def required(schema: dict[str, Any], *names: str) -> dict[str, Any]:
    schema["required"] = list(names)
    return schema


# --- freshness of a store somebody else fills --------------------------------
#
# Lives here rather than in one tool module for the same reason `flat` does: the second
# caller proved it general. A calendar tool and a coursework tool read rows a connector
# archived, and both have the one failure mode that matters for a store you do not fill
# yourself — the daemon stops, the query returns nothing, and the tool says "you are free"
# when it means "I cannot see". Freshness is therefore part of the answer, not a decoration
# on it, and the same words should carry that in every tool that reads an archive.

# A feed is late once it has missed this many polls in a row. Four rather than one because
# a single missed poll is the internet being the internet, and crying stale on every jitter
# teaches the reader to ignore the line that matters.
STALE_POLLS = 4
MIN_STALE_S = 600.0


class Polled(Protocol):
    """Anything that names its own poll cadence: a Google account, a Brightspace feed.

    Structural rather than a shared base class, so `tools/` keeps not importing `config`.
    """

    poll_interval_s: float


def describe_age(seconds: float) -> str:
    if seconds < 90:
        return "just now"
    if seconds < 5400:
        return f"{round(seconds / 60)} minutes ago"
    if seconds < 172800:
        return f"{round(seconds / 3600)} hours ago"
    return f"{round(seconds / 86400)} days ago"


def feed_health(
    state: dict[str, Any], source: Polled, now: datetime, *, noun: str = "calendar"
) -> tuple[str, bool]:
    """(how the freshness reads, whether the rows can be trusted to be complete).

    False does not mean the rows are wrong — they are whatever the last good poll saw. It
    means the *absence* of a row says nothing, so the caller must not report an empty
    window as an empty calendar.

    `noun` names the feed in the sentence, because "the calendar feed is behind" and "the
    Brightspace feed is behind" are the same fact about different doors, and a reader who
    has both configured needs to know which one to go and fix.
    """
    if not state:
        return f"the {noun} feed has never run", False
    if not state.get("enabled", True):
        why = state.get("disabled_reason") or "no reason recorded"
        return f"the {noun} feed is disabled ({flat(why, 120)})", False
    last = state.get("last_success_at")
    if last is None:
        return f"the {noun} feed has not completed a poll yet", False
    age = (now - last).total_seconds()
    limit = max(source.poll_interval_s * STALE_POLLS, MIN_STALE_S)
    if age > limit:
        return f"the {noun} feed is behind — last synced {describe_age(age)}", False
    return f"synced {describe_age(age)}", True
