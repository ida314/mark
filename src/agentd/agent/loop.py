"""The turn loop: one user message in, streamed events out, everything archived."""

from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from ..config import Config, get_config
from ..db import repo_archive, repo_ops
from ..db.repo_archive import RawEvent
from ..db.repo_ops import ActionRecord
from ..ids import utcnow, uuid7
from ..llm.base import Finish, LLMError, ReasoningDelta, TextDelta, ToolCallDone
from ..llm.roles import get_provider, params_for
from ..obs import otel
from ..policy.approvals import Approver, AutoApprover
from ..policy.engine import PolicyEngine, engine_from_config
from ..tools.base import ToolContext
from ..tools.executor import ToolExecutor
from ..tools.registry import Registry, get_registry
from . import context as ctxmod
from .events import (
    Notice,
    TextChunk,
    ThinkingChunk,
    ToolFinished,
    ToolStarted,
    TurnFinished,
    UIEvent,
)

FINAL_NUDGE = (
    "You have used your whole tool budget for this turn. Stop calling tools. "
    "Summarize what you found, what you changed, and what is still open."
)


@dataclass
class Session:
    """A conversation. Durable in Postgres; this is just the in-process handle."""

    id: UUID
    channel: str = "cli"
    autonomy: str = "assist"
    summary: str | None = None
    tools_used: set[str] = field(default_factory=set)
    tainted: bool = False

    @classmethod
    async def create(cls, channel: str = "cli", autonomy: str = "assist") -> Session:
        sid = await repo_archive.create_session(channel=channel)
        return cls(id=sid, channel=channel, autonomy=autonomy)

    @classmethod
    async def resume(cls, session_id: UUID, autonomy: str = "assist") -> Session:
        row = await repo_archive.get_session(session_id)
        if row is None:
            raise ValueError(f"No such session: {session_id}")
        return cls(
            id=row["id"], channel=row["channel"], autonomy=autonomy, summary=row.get("summary")
        )


class AgentLoop:
    def __init__(
        self,
        *,
        cfg: Config | None = None,
        registry: Registry | None = None,
        engine: PolicyEngine | None = None,
        approver: Approver | None = None,
        provider=None,
        role: str = "main",
        actor: str = "main",
    ) -> None:
        self.cfg = cfg or get_config()
        self.registry = registry or get_registry()
        self.engine = engine or engine_from_config(self.cfg)
        self.approver = approver or AutoApprover(approve=False)
        self.provider = provider or get_provider(self.cfg)
        self.role = role
        self.actor = actor
        self.executor = ToolExecutor(
            self.registry.tools,
            self.engine,
            self.approver,
            max_result_chars=self.cfg.agent.tool_result_max_chars,
        )
        self.last_pack = None

    async def run_turn(
        self,
        session: Session,
        user_text: str,
        *,
        origin: str = "interactive",
        autonomy: str | None = None,
        record_user_message: bool = True,
        extra_system: str | None = None,
    ) -> AsyncIterator[UIEvent]:
        autonomy = autonomy or session.autonomy
        turn_id = uuid7()
        started = time.perf_counter()
        usage_total = {"input_tokens": 0, "output_tokens": 0}

        with otel.span("agent.turn", {"agentd.autonomy": autonomy, "agentd.origin": origin}):
            trace_ids = otel.current_ids()
            if record_user_message:
                await repo_archive.append_event(
                    RawEvent(
                        kind="user_message", actor="user", content=user_text,
                        session_id=session.id, turn_id=turn_id,
                    )
                )

            # 1. What do we know that bears on this?
            context_block = ""
            refs: dict[str, Any] = {}
            try:
                from ..memory.retrieval import pack

                with otel.span("memory.retrieve"):
                    result = await pack(
                        user_text,
                        budget_tokens=self.cfg.agent.context_budget_tokens,
                        mode="fast",
                        session_id=session.id,
                        turn_id=turn_id,
                    )
                context_block = result.text
                refs = {"items": [i.ref for i in result.items]}
                self.last_pack = result
            except Exception as exc:  # memory must never block a conversation
                yield Notice(text=f"(memory retrieval unavailable: {exc})", level="warn")

            # 2. Which tools should this turn even see?
            tools = await self.registry.select(
                user_text, session_used=session.tools_used, cfg=self.cfg
            )
            tool_schemas = [t.openai_schema() for t in tools]
            exposed = {t.name: t for t in tools}

            # 3. Build the messages.
            history = await ctxmod.history_messages(
                session.id, budget_tokens=self.cfg.agent.history_tokens, summary=session.summary
            )
            messages = ctxmod.build_messages(
                self.cfg, autonomy=autonomy, context_block=context_block,
                history=history, user_text=user_text,
            )
            if extra_system:
                messages.insert(1, {"role": "system", "content": extra_system})

            tctx = ToolContext(
                session_id=session.id, turn_id=turn_id, actor=self.actor, origin=origin,
                autonomy=autonomy, tainted=session.tainted,
            )

            # 4. Step until the model stops calling tools.
            final_text: list[str] = []
            steps = 0
            for step in range(self.cfg.agent.max_steps):
                steps = step + 1
                last_step = step == self.cfg.agent.max_steps - 1
                step_tools = None if last_step else tool_schemas
                if last_step:
                    messages.append({"role": "system", "content": FINAL_NUDGE})

                text_parts: list[str] = []
                calls = []
                llm_started = time.perf_counter()
                params = params_for(self.role, self.cfg)
                try:
                    with otel.span(
                        "llm.chat", otel.gen_ai_attrs(self.role, params.model or "")
                    ):
                        async for event in self.provider.stream(
                            messages, step_tools, params=params
                        ):
                            if isinstance(event, TextDelta):
                                text_parts.append(event.text)
                                yield TextChunk(text=event.text)
                            elif isinstance(event, ReasoningDelta):
                                yield ThinkingChunk(text=event.text)
                            elif isinstance(event, ToolCallDone):
                                calls.append(event.call)
                            elif isinstance(event, Finish):
                                usage_total["input_tokens"] += event.usage.get("input_tokens", 0)
                                usage_total["output_tokens"] += event.usage.get("output_tokens", 0)
                except LLMError as exc:
                    yield Notice(text=f"Model call failed: {exc}", level="error")
                    await self._record_turn(
                        turn_id, session, origin, autonomy, "error", started,
                        usage_total, refs, trace_ids, str(exc),
                    )
                    yield TurnFinished(
                        turn_id=str(turn_id), text="", steps=steps, usage=usage_total
                    )
                    return

                assistant_text = "".join(text_parts).strip()
                await repo_ops.write_action(
                    ActionRecord(
                        actor=self.actor, kind="llm_call", name=params.model or "model",
                        status="ok", session_id=session.id, turn_id=turn_id,
                        output={"text": assistant_text[:500], "tool_calls": [c.name for c in calls]},
                        duration_ms=int((time.perf_counter() - llm_started) * 1000),
                        tokens_in=usage_total["input_tokens"],
                        tokens_out=usage_total["output_tokens"],
                        **trace_ids,
                    )
                )

                if not calls:
                    final_text.append(assistant_text)
                    break

                messages.append(
                    {
                        "role": "assistant",
                        "content": assistant_text or None,
                        "tool_calls": [
                            {
                                "id": c.id,
                                "type": "function",
                                "function": {"name": c.name, "arguments": c.arguments},
                            }
                            for c in calls
                        ],
                    }
                )
                if assistant_text:
                    final_text.append(assistant_text)

                for call in calls:
                    args_preview = _safe_args(call.arguments)
                    yield ToolStarted(name=call.name, args=args_preview)
                    if call.name not in exposed and call.name in self.registry.tools:
                        exposed[call.name] = self.registry.tools[call.name]
                    result = await self.executor.run(call.name, call.arguments, tctx)
                    session.tools_used.add(call.name)
                    if result.trust == "untrusted":
                        session.tainted = True
                        tctx.tainted = True
                    denied = bool(result.data.get("denied"))
                    yield ToolFinished(
                        name=call.name, ok=result.ok,
                        summary=_summarize(result.content), denied=denied,
                    )
                    await repo_archive.append_event(
                        RawEvent(
                            kind="tool_result", actor=f"tool:{call.name}",
                            content=result.content[:20000], trust=result.trust,
                            session_id=session.id, turn_id=turn_id,
                            payload={"args": args_preview, "ok": result.ok},
                        )
                    )
                    messages.append(
                        {"role": "tool", "tool_call_id": call.id, "content": result.content}
                    )
                    for candidate in result.candidates:
                        await _store_candidate(candidate, session, turn_id, self.actor)

                # Tools added mid-turn by tool_search become visible on the next step.
                for name in tctx.extra.pop("added_tools", []):
                    t = self.registry.get(name)
                    if t and name not in exposed:
                        exposed[name] = t
                        tool_schemas.append(t.openai_schema())

            answer = "\n".join(t for t in final_text if t).strip()
            if answer:
                await repo_archive.append_event(
                    RawEvent(
                        kind="assistant_message", actor=self.actor, content=answer,
                        session_id=session.id, turn_id=turn_id,
                    )
                )
            await repo_archive.touch_session(session.id)
            await self._record_turn(
                turn_id, session, origin, autonomy, "ok", started, usage_total, refs, trace_ids
            )
            yield TurnFinished(
                turn_id=str(turn_id), text=answer, steps=steps, usage=usage_total
            )

    async def _record_turn(
        self, turn_id, session, origin, autonomy, status, started, usage, refs, trace_ids,
        error: str | None = None,
    ) -> None:
        await repo_ops.write_action(
            ActionRecord(
                id=turn_id, actor=self.actor, kind="turn", name="turn", status=status,
                session_id=session.id, turn_id=turn_id,
                policy={"autonomy": autonomy, "origin": origin, "tainted": session.tainted},
                refs=refs, error=error,
                duration_ms=int((time.perf_counter() - started) * 1000),
                tokens_in=usage["input_tokens"], tokens_out=usage["output_tokens"],
                **trace_ids,
            )
        )


async def _store_candidate(candidate: dict, session: Session, turn_id: UUID, actor: str) -> None:
    from ..db import repo_memory

    await repo_memory.insert_candidate(
        statement=candidate.get("statement", ""),
        proposed_by=candidate.get("proposed_by", actor),
        kind=candidate.get("kind", "fact"),
        confidence=float(candidate.get("confidence", 0.6)),
        structured=candidate.get("structured", {}),
        evidence=candidate.get("evidence", []),
        source_trust=candidate.get("source_trust", "trusted"),
        session_id=session.id,
        turn_id=turn_id,
    )


def _safe_args(raw: str) -> dict[str, Any]:
    try:
        parsed = json.loads(raw or "{}")
        return parsed if isinstance(parsed, dict) else {"_": parsed}
    except json.JSONDecodeError:
        return {"_raw": (raw or "")[:200]}


def _summarize(content: str, limit: int = 160) -> str:
    flat = " ".join(content.split())
    return flat[:limit] + ("…" if len(flat) > limit else "")


async def one_shot(
    prompt: str, *, autonomy: str = "assist", channel: str = "cli", approver: Approver | None = None
) -> str:
    """Single turn, no REPL. Used by `agent ask`, watchers and tests."""
    session = await Session.create(channel=channel, autonomy=autonomy)
    loop = AgentLoop(approver=approver)
    answer = ""
    async for event in loop.run_turn(session, prompt, autonomy=autonomy):
        if isinstance(event, TurnFinished):
            answer = event.text
    await repo_archive.end_session(session.id)
    return answer


__all__ = ["AgentLoop", "Session", "one_shot", "utcnow"]
