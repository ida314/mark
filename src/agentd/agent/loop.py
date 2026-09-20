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
from ..obs import otel, telemetry
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

# How many times one tool may reject the identical arguments before the loop takes it away
# for the rest of the turn. Argument validation is deterministic - the same arguments
# against the same schema fail the same way every time - so a repeat is not a retry, it is
# the turn being spent on a call that provably cannot run. Two attempts, because the first
# rejection is the one that carries the repair advice and the model deserves to act on it.
STUCK_LIMIT = 2
STUCK_NUDGE = (
    "`{name}` rejected the same arguments {attempts} times, so it has been withdrawn for "
    "the rest of this turn. Do not look for another way to call it. Carry on without it, "
    "and tell the user plainly what you were unable to do."
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
    # Whether the user's own private data has been read into this conversation. Sticky for
    # the life of the session: the mail is out of context by the next turn, but what the
    # model concluded from it is not, so lifting the interlock at the turn boundary would
    # protect the wrong thing.
    private: bool = False

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
            id=row["id"], channel=row["channel"], autonomy=autonomy, summary=row.get("summary"),
            # Without this, `agent chat --resume` would silently hand back a session whose
            # interlock had been earned and then forgotten. The archive already knows: every
            # private tool result was written with a marker, so no migration is needed.
            private=await repo_archive.session_read_private(session_id),
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
        # Pass 1 baseline measurement. Reads state this loop already keeps and feeds
        # nothing back into it; see obs/telemetry.py.
        tele = telemetry.TurnTelemetry.start(
            cfg=self.cfg, request_id=str(turn_id), session_id=str(session.id),
            role=self.role, actor=self.actor, origin=origin, channel=session.channel,
            autonomy=autonomy, model=params_for(self.role, self.cfg).model or "",
        )

        with otel.span("agent.turn", {"agentd.autonomy": autonomy, "agentd.origin": origin}):
            trace_ids = otel.current_ids()
            if record_user_message:
                await repo_archive.append_event(
                    RawEvent(
                        kind="user_message", actor="user", content=user_text,
                        session_id=session.id, turn_id=turn_id,
                    )
                )

            # Deterministic, channel-agnostic: `agent ask`, one-shot and daemon turns all
            # pass through here, not just `agent chat`. See `memory/cues.py` for the
            # safety property this can and cannot buy.
            from ..memory.cues import detect_correction

            correction_cue = detect_correction(user_text)

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
            tele.tools_offered(list(exposed), registry_size=len(self.registry.enabled()))

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
                # Both flags are seeded from the session, not reset per turn. Forgetting
                # `private` here is invisible to any single-turn test: the interlock works
                # inside the turn that read the mail and is gone by the next message, which
                # is the turn an injected instruction would actually use.
                autonomy=autonomy, tainted=session.tainted, private=session.private,
            )
            if correction_cue is not None:
                tctx.extra["correction_cue"] = correction_cue

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
                tele.context_sample(messages)
                finish_reason: str | None = None
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
                                finish_reason = event.reason
                                tele.usage_seen(event.usage)
                                usage_total["input_tokens"] += event.usage.get("input_tokens", 0)
                                usage_total["output_tokens"] += event.usage.get("output_tokens", 0)
                except LLMError as exc:
                    yield Notice(text=f"Model call failed: {exc}", level="error")
                    await self._record_turn(
                        turn_id, session, origin, autonomy, "error", started,
                        usage_total, refs, trace_ids, str(exc),
                    )
                    tele.finish(
                        status="failed", steps=steps, usage=usage_total, answer="",
                        error=str(exc), trace_ids=trace_ids,
                    )
                    yield TurnFinished(
                        turn_id=str(turn_id), text="", steps=steps, usage=usage_total
                    )
                    return

                tele.llm_call(time.perf_counter() - llm_started, finish_reason)
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

                stuck: list[str] = []
                for call in calls:
                    args_preview = _safe_args(call.arguments)
                    yield ToolStarted(name=call.name, args=args_preview)
                    # Read before the re-add below, which would otherwise make every call
                    # look like it was to a tool the model had been shown.
                    was_visible = call.name in exposed
                    known = call.name in self.registry.tools
                    call_started = time.perf_counter()
                    if call.name not in exposed and call.name in self.registry.tools:
                        exposed[call.name] = self.registry.tools[call.name]
                    result = await self.executor.run(
                        call.name, call.arguments, tctx, parent_id=turn_id
                    )
                    session.tools_used.add(call.name)
                    if result.trust == "untrusted":
                        session.tainted = True
                        tctx.tainted = True
                    called = self.registry.tools.get(call.name)
                    private_call = bool(called and called.private_output)
                    if private_call:
                        # Mutating the shared context means the very next call in this same
                        # batch is already judged against the interlock, exactly as taint is.
                        session.private = True
                        tctx.private = True
                    denied = bool(result.data.get("denied"))
                    tele.tool_call(
                        telemetry.CallRecord(
                            step=steps, name=call.name, ok=result.ok, denied=denied,
                            invalid_args=bool(result.data.get("invalid_args")),
                            known=known, visible=was_visible,
                            duration_ms=int((time.perf_counter() - call_started) * 1000),
                        )
                    )
                    yield ToolFinished(
                        name=call.name, ok=result.ok,
                        summary=_summarize(result.content), denied=denied,
                    )
                    await repo_archive.append_event(
                        RawEvent(
                            kind="tool_result", actor=f"tool:{call.name}",
                            content=result.content[:20000], trust=result.trust,
                            session_id=session.id, turn_id=turn_id,
                            payload={
                                "args": args_preview, "ok": result.ok,
                                # What `Session.resume` reads back.
                                **({"private": True} if private_call else {}),
                            },
                        )
                    )
                    messages.append(
                        {"role": "tool", "tool_call_id": call.id, "content": result.content}
                    )
                    for candidate in result.candidates:
                        await _store_candidate(candidate, session, turn_id, self.actor)

                    attempts = int(result.data.get("attempt", 0))
                    if attempts >= STUCK_LIMIT and call.name in exposed:
                        del exposed[call.name]
                        tool_schemas = [
                            schema
                            for schema in tool_schemas
                            if schema["function"]["name"] != call.name
                        ]
                        stuck.append(
                            STUCK_NUDGE.format(name=call.name, attempts=attempts)
                        )
                        yield Notice(
                            text=(
                                f"({call.name} kept being called with arguments it rejects; "
                                "withdrawn for this turn)"
                            ),
                            level="warn",
                        )

                # After the batch, never between an assistant's tool calls and their results.
                for note in stuck:
                    messages.append({"role": "system", "content": note})

                # Tools added mid-turn by tool_search become visible on the next step.
                revealed = tctx.extra.pop("added_tools", [])
                for name in revealed:
                    t = self.registry.get(name)
                    if t and name not in exposed:
                        exposed[name] = t
                        tool_schemas.append(t.openai_schema())
                tele.tools_revealed(revealed)

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
            # "abandoned" is the step budget running out with the model still calling tools:
            # the last step is forced tool-free and carries FINAL_NUDGE, so what comes back
            # is a summary of unfinished work, not an answer. Same reading as a sub-agent's
            # `budget_exhausted`.
            tele.finish(
                status="abandoned" if steps >= self.cfg.agent.max_steps else "completed",
                steps=steps, usage=usage_total, answer=answer, trace_ids=trace_ids,
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
