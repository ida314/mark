"""Provider-agnostic LLM interface. Models are replaceable; this contract is not."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel


@dataclass
class CallParams:
    temperature: float = 0.7
    top_p: float = 0.8
    max_tokens: int = 4096
    thinking: bool = False
    model: str | None = None


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: str  # raw JSON text as produced by the model
    # True when the stream carried a name and not one byte of arguments, and `arguments` is
    # the `{}` the provider put there. The executor rejects it either way; this says why.
    arguments_missing: bool = False


@dataclass
class TextDelta:
    text: str


@dataclass
class ReasoningDelta:
    text: str


@dataclass
class ToolCallDone:
    call: ToolCall


@dataclass
class Finish:
    reason: str
    usage: dict[str, int] = field(default_factory=dict)


LLMEvent = TextDelta | ReasoningDelta | ToolCallDone | Finish


class LLMError(RuntimeError):
    pass


@runtime_checkable
class LLMProvider(Protocol):
    name: str

    def stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        *,
        params: CallParams,
    ) -> AsyncIterator[LLMEvent]: ...

    async def complete_json[T: BaseModel](
        self, messages: list[dict[str, Any]], schema: type[T], *, params: CallParams
    ) -> T: ...
