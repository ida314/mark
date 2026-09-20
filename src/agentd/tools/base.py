"""Tool definitions. A tool is data plus a handler; nothing here executes anything."""

from __future__ import annotations

import hashlib
import html as htmllib
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

Risk = str  # read | draft | write | external | destructive


@dataclass
class ToolContext:
    """Everything a handler is allowed to know about its caller."""

    session_id: UUID | None = None
    turn_id: UUID | None = None
    action_id: UUID | None = None
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
