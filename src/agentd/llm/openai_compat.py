"""OpenAI-compatible provider (SIR router / vLLM / anything speaking the same API)."""

from __future__ import annotations

import json
import re
from collections.abc import AsyncIterator
from typing import Any

from openai import AsyncOpenAI
from pydantic import BaseModel, ValidationError

from ..config import Config, get_config
from .base import (
    CallParams,
    Finish,
    LLMError,
    LLMEvent,
    ReasoningDelta,
    TextDelta,
    ToolCall,
    ToolCallDone,
)

# Some stacks emit tool calls as text when the server-side parser is missing or
# mis-configured. We recover them rather than silently losing the call.
_XML_CALL = re.compile(r"<tool_call>\s*(?P<body>\{.*?\})\s*</tool_call>", re.DOTALL)


def parse_text_tool_calls(text: str) -> list[ToolCall]:
    out: list[ToolCall] = []
    for i, m in enumerate(_XML_CALL.finditer(text)):
        try:
            obj = json.loads(m.group("body"))
        except json.JSONDecodeError:
            continue
        name = obj.get("name")
        if not name:
            continue
        args = obj.get("arguments", {})
        out.append(
            ToolCall(
                id=f"text_{i}",
                name=name,
                arguments=args if isinstance(args, str) else json.dumps(args),
            )
        )
    return out


def strip_text_tool_calls(text: str) -> str:
    return _XML_CALL.sub("", text).strip()


class OpenAICompatProvider:
    """Streaming chat completions with tool-call assembly."""

    def __init__(self, cfg: Config | None = None, name: str = "local") -> None:
        self.cfg = cfg or get_config()
        self.name = name
        self.client = AsyncOpenAI(
            base_url=self.cfg.llm.base_url,
            api_key=self.cfg.llm.api_key,
            timeout=self.cfg.llm.timeout_s,
            max_retries=1,
        )

    async def _with_retries(self, make_request, *, attempts: int = 4):
        """Ride out backend restarts.

        The local inference server is managed by a residency scheduler that stops and
        reloads model backends on demand, so a connection error usually means "wait,
        it is coming back", not "this request is bad".
        """
        import asyncio

        from openai import APIConnectionError, APITimeoutError, InternalServerError

        delay = 2.0
        last: Exception | None = None
        for attempt in range(attempts):
            try:
                return await make_request()
            except (APIConnectionError, APITimeoutError, InternalServerError) as exc:
                last = exc
                if attempt == attempts - 1:
                    break
                await asyncio.sleep(delay)
                delay = min(delay * 3, 60.0)
        raise last  # type: ignore[misc]

    def _request_kwargs(self, params: CallParams) -> dict[str, Any]:
        extra: dict[str, Any] = {"chat_template_kwargs": {"enable_thinking": params.thinking}}
        return {
            "model": params.model or self.cfg.llm.model,
            "temperature": params.temperature,
            "top_p": params.top_p,
            "max_tokens": params.max_tokens,
            "extra_body": extra,
        }

    async def stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        *,
        params: CallParams,
    ) -> AsyncIterator[LLMEvent]:
        kwargs = self._request_kwargs(params)
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"
        try:
            stream = await self._with_retries(
                lambda: self.client.chat.completions.create(
                    messages=messages, stream=True, stream_options={"include_usage": True},
                    **kwargs,
                )
            )
        except Exception as exc:  # network, 4xx, model not loaded
            raise LLMError(f"{self.name}: chat request failed: {exc}") from exc

        # tool calls arrive as fragments keyed by index
        frags: dict[int, dict[str, str]] = {}
        text_buf: list[str] = []
        finish_reason = "stop"
        usage: dict[str, int] = {}

        try:
            async for chunk in stream:
                if getattr(chunk, "usage", None):
                    usage = {
                        "input_tokens": chunk.usage.prompt_tokens or 0,
                        "output_tokens": chunk.usage.completion_tokens or 0,
                    }
                if not chunk.choices:
                    continue
                choice = chunk.choices[0]
                delta = choice.delta
                if choice.finish_reason:
                    finish_reason = choice.finish_reason

                reasoning = getattr(delta, "reasoning_content", None) or getattr(
                    delta, "reasoning", None
                )
                if reasoning:
                    yield ReasoningDelta(text=reasoning)
                if delta.content:
                    text_buf.append(delta.content)
                    yield TextDelta(text=delta.content)
                for tc in delta.tool_calls or []:
                    slot = frags.setdefault(tc.index, {"id": "", "name": "", "arguments": ""})
                    if tc.id:
                        slot["id"] = tc.id
                    if tc.function and tc.function.name:
                        slot["name"] = tc.function.name
                    if tc.function and tc.function.arguments:
                        slot["arguments"] += tc.function.arguments
        except Exception as exc:
            raise LLMError(f"{self.name}: stream failed: {exc}") from exc

        emitted = 0
        for idx in sorted(frags):
            slot = frags[idx]
            if not slot["name"]:
                continue
            yield ToolCallDone(
                call=ToolCall(
                    id=slot["id"] or f"call_{idx}",
                    name=slot["name"],
                    arguments=slot["arguments"] or "{}",
                )
            )
            emitted += 1

        # Fallback: the server dropped structured tool calls but left them in the text.
        if emitted == 0:
            for call in parse_text_tool_calls("".join(text_buf)):
                yield ToolCallDone(call=call)
                emitted += 1
            if emitted:
                finish_reason = "tool_calls"

        yield Finish(reason=finish_reason, usage=usage)

    async def complete_json[T: BaseModel](
        self, messages: list[dict[str, Any]], schema: type[T], *, params: CallParams
    ) -> T:
        """Guided JSON with one repair attempt showing the model its own validation error."""
        kwargs = self._request_kwargs(params)
        json_schema = schema.model_json_schema()
        # strict=True is what makes vLLM actually constrain decoding to the schema;
        # without it the model prefixes prose and the parse fails.
        kwargs["response_format"] = {
            "type": "json_schema",
            "json_schema": {"name": schema.__name__, "schema": json_schema, "strict": True},
        }
        attempt_messages = list(messages)
        last_error = ""
        for attempt in range(2):
            try:
                resp = await self._with_retries(
                    lambda msgs=attempt_messages: self.client.chat.completions.create(
                        messages=msgs, **kwargs
                    )
                )
            except Exception as exc:
                raise LLMError(f"{self.name}: json request failed: {exc}") from exc
            raw = resp.choices[0].message.content or ""
            try:
                return schema.model_validate_json(_extract_json(raw))
            except (ValidationError, ValueError) as exc:
                last_error = str(exc)
                if attempt == 0:
                    attempt_messages = [
                        *messages,
                        {"role": "assistant", "content": raw},
                        {
                            "role": "user",
                            "content": (
                                "That did not validate against the schema:\n"
                                f"{last_error}\nReturn corrected JSON only."
                            ),
                        },
                    ]
        raise LLMError(f"{self.name}: could not get valid JSON: {last_error}")

    async def probe_tool_calls(self) -> tuple[bool, str]:
        """Does this endpoint actually return tool calls? Used by `agent doctor`."""
        tools = [
            {
                "type": "function",
                "function": {
                    "name": "get_time",
                    "description": "Get the current time in a timezone",
                    "parameters": {
                        "type": "object",
                        "properties": {"tz": {"type": "string"}},
                        "required": ["tz"],
                    },
                },
            }
        ]
        messages = [{"role": "user", "content": "What time is it in Tokyo? Use the tool."}]
        params = CallParams(temperature=0.0, max_tokens=256)
        try:
            async for event in self.stream(messages, tools, params=params):
                if isinstance(event, ToolCallDone):
                    return True, f"{event.call.name}({event.call.arguments})"
            return False, "endpoint returned no tool_calls (SIR passthrough fix not deployed?)"
        except LLMError as exc:
            return False, str(exc)


def _extract_json(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.MULTILINE).strip()
    start = min((i for i in (text.find("{"), text.find("[")) if i != -1), default=-1)
    if start > 0:
        text = text[start:]
    return text
