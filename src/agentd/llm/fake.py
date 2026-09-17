"""Scripted provider used by the tests: deterministic turns, no network."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

from pydantic import BaseModel

from .base import CallParams, Finish, LLMEvent, TextDelta, ToolCall, ToolCallDone


class FakeProvider:
    """Plays back a list of scripted turns.

    Each turn is either a string (assistant text) or a list of (name, args) tool calls.
    JSON completions are served from `json_results` in order.
    """

    name = "fake"

    def __init__(
        self,
        turns: list[str | list[tuple[str, dict[str, Any]]]] | None = None,
        json_results: list[BaseModel | dict] | None = None,
    ) -> None:
        self.turns = list(turns or [])
        self.json_results = list(json_results or [])
        self.calls: list[dict[str, Any]] = []  # what the loop sent us

    async def stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        *,
        params: CallParams,
    ) -> AsyncIterator[LLMEvent]:
        self.calls.append({"messages": messages, "tools": [t["function"]["name"] for t in tools or []]})
        turn = self.turns.pop(0) if self.turns else "done"
        if isinstance(turn, str):
            for piece in turn.split(" "):
                yield TextDelta(text=piece + " ")
            yield Finish(reason="stop", usage={"input_tokens": 10, "output_tokens": 5})
            return
        for i, (name, args) in enumerate(turn):
            yield ToolCallDone(
                call=ToolCall(id=f"fake_{i}", name=name, arguments=json.dumps(args))
            )
        yield Finish(reason="tool_calls", usage={"input_tokens": 10, "output_tokens": 5})

    async def complete_json[T: BaseModel](
        self, messages: list[dict[str, Any]], schema: type[T], *, params: CallParams
    ) -> T:
        self.calls.append({"messages": messages, "json_schema": schema.__name__})
        if not self.json_results:
            return schema.model_validate({})
        result = self.json_results.pop(0)
        if isinstance(result, BaseModel):
            return schema.model_validate(result.model_dump())
        return schema.model_validate(result)
