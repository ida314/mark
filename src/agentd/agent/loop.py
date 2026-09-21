"""The turn loop: one user message in, prose out, everything recorded in the journal.

What this loop hands back to its caller is the model's own output (`agent/stream.py`). What
it *records* - every tool call, every message, how the turn ended - goes to the run journal,
and anything that wants to watch that subscribes with `journal/feed.py`. Session 2c removed
the second copy that used to be yielded alongside.
"""

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
from ..journal import events as jevents
from ..journal.checkpoints import checkpoint_at
from ..journal.ledger import EffectLedger
from ..journal.runtime import RunJournal, get_writer
from ..journal.writer import JournalWriter
from ..llm.base import Finish, LLMError, ReasoningDelta, TextDelta, ToolCallDone
from ..llm.roles import get_provider, params_for
from ..obs import otel, telemetry
from ..policy.approvals import Approver, AutoApprover
from ..policy.engine import PolicyEngine, engine_from_config
from ..tools.base import ToolContext
from ..tools.executor import ToolExecutor
from ..tools.registry import Registry, get_registry
from . import budget
from . import context as ctxmod
from . import handoff as handoff_mod
from .stream import Answer, Delta, Notice, TurnStream

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
    # The handoff in force, once this conversation has run out of room to carry itself
    # (session 5b). While it is set, a turn is assembled from it plus the messages after its
    # watermark, and the part of the conversation it replaced is not read again. In-process
    # only: the durable copy is `handoff_object` on the `turn_end` checkpoint, and putting it
    # back on a resumed session is 5c's.
    handoff: handoff_mod.Handoff | None = None

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


@dataclass
class _TurnRecord:
    """The journal's view of one turn: `agent_started` on the way in, `agent_finished` on
    every way out.

    A context manager rather than a pair of calls, because "every way out" includes the one
    nobody writes code for: a consumer that stops iterating the turn's events mid-stream
    gets `GeneratorExit` thrown at the suspended `yield`, and neither the telemetry record
    nor the `actions` row is written in that case (Pass 1, open question 4). A `with` block
    unwinds there too, so the run still ends with an `agent_finished` - `cancelled` rather
    than an event sequence that simply stops and never says why.

    `status` starts as None and every deliberate exit sets it. None at `__exit__` therefore
    means the turn ended without anything having decided how, which is a real state and is
    recorded as one; it is not defaulted to `completed`.
    """

    rj: RunJournal
    turn_id: str
    started: float
    # Carried so that the turn_end boundary asks the flag through one door
    # (`journal.checkpoints.enabled`) rather than reading config at a second place.
    cfg: Config | None = None
    status: str | None = None
    steps: int = 0
    answer: str = ""
    error: str | None = None
    usage: dict[str, int] = field(default_factory=lambda: {"input_tokens": 0, "output_tokens": 0})
    usage_reported: bool = False
    # The fullest this turn's context got, and whether any step of it came within the
    # handoff threshold of the ceiling. `context` stays None until a prompt is actually
    # assembled, so a turn that died before its first model call records "unmeasured"
    # rather than a 0 that reads as a turn which used no context.
    context: budget.ContextReading | None = None
    context_crossed: bool = False
    # Filled when this turn generated a handoff, so the `turn_end` boundary stores it. It is
    # the *next* checkpoint after the crossing, which is where the pass file puts it.
    handoff_object: dict[str, Any] | None = None

    @classmethod
    def start(
        cls, rj: RunJournal, *, turn_id: str, started: float, payload: dict,
        cfg: Config | None = None,
    ) -> _TurnRecord:
        rj.emit("agent_started", payload)
        return cls(rj=rj, turn_id=turn_id, started=started, cfg=cfg)

    def finish(
        self, *, status: str, steps: int, answer: str, usage: dict[str, int],
        usage_reported: bool, error: str | None = None,
    ) -> None:
        self.status, self.steps, self.answer = status, steps, answer
        self.usage, self.usage_reported, self.error = dict(usage), usage_reported, error

    def context_seen(self, reading: budget.ContextReading) -> bool:
        """Take one reading of the prompt about to be sent. True on this turn's first
        crossing, and only then, so a boundary fires once rather than once per step.

        The peak is kept rather than the latest: what `agent_finished` reports is the
        closest this turn came to its ceiling, and a turn that crossed and then shrank
        still crossed.
        """
        if self.context is None or reading.used_tokens > self.context.used_tokens:
            self.context = reading
        if not reading.crossed:
            return False
        first, self.context_crossed = not self.context_crossed, True
        return first

    def __enter__(self) -> _TurnRecord:
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        status, error = self.status, self.error
        if status is None:
            if exc_type is None or issubclass(exc_type, GeneratorExit):
                status = "cancelled"
            else:
                status, error = "failed", f"{exc_type.__name__}: {exc}"
        # An estimate, never a count: `context_basis` is to `context_tokens` what
        # `usage_reported` is to `usage`, and the router in front of this model has dropped
        # the usage chunk on every streamed turn since Pass 1, so there is no count to use.
        context = self.context or budget.unmeasured(self.cfg)
        # Synchronous by classification (journal.writer.SYNC_TYPES), so this carries the
        # whole turn's buffered tail to disk with it.
        self.rj.emit(
            "agent_finished",
            {
                "turn_id": self.turn_id,
                "status": status,
                "steps": self.steps,
                "duration_ms": int((time.perf_counter() - self.started) * 1000),
                "answer_chars": len(self.answer),
                "answer_preview": jevents.preview(self.answer),
                "usage": dict(self.usage),
                "usage_reported": self.usage_reported,
                "context_tokens": context.used_tokens,
                "context_ceiling_tokens": context.ceiling_tokens,
                "context_threshold_tokens": context.threshold_tokens,
                "context_crossed": self.context_crossed,
                "context_basis": context.basis,
                "error": error,
            },
        )
        # The turn_end boundary, after the turn's last event and inside the same unwind, so
        # that the four ways out of a turn all reach it - including the consumer that walked
        # away. It covers `agent_finished` because that event is already on disk by now.
        #
        # A worker's own turn ends here too and gets no checkpoint: its `worker_created` has
        # no `worker_finished` yet, so `checkpoint_at` reads the run as mid-worker and
        # declines. That is the architecture's "workers are the unit of atomicity", derived
        # rather than re-stated here.
        #
        # It can raise during an unwind, and session 4b decided deliberately to leave it
        # that way (4a's open question 3): Python chains the two, so a turn that was already
        # failing keeps its own error as `__context__`, and a boundary that cannot write is
        # the condition the next crash will be asked about. Swallowing it would make the one
        # checkpoint a resume needed the one nobody knew was missing.
        checkpoint_at(
            "turn_end", run_id=self.rj.run_id, writer=self.rj.writer, cfg=self.cfg,
            handoff_object=self.handoff_object,
        )
        return False


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
        journal: JournalWriter | None = None,
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
            # The effect ledger writes through *this loop's* journal, resolved lazily.
            # A sub-agent, and every test that hands a loop its own writer, would
            # otherwise announce its effects in one file and the rest of the run in
            # another - two records of one turn, neither complete.
            ledger=EffectLedger(self.journal_writer),
        )
        self.last_pack = None
        self._journal = journal

    def journal_writer(self) -> JournalWriter:
        """The writer this loop appends to: the process's, unless one was injected.

        Shared per process and per file rather than per loop. A sub-agent builds a second
        `AgentLoop` inside the caller's turn, and two writers on one file would race each
        other for `seq` positions in the run they are both writing.
        """
        return self._journal or get_writer(self.cfg)

    async def run_turn(
        self,
        session: Session,
        user_text: str,
        *,
        origin: str = "interactive",
        autonomy: str | None = None,
        record_user_message: bool = True,
        extra_system: str | None = None,
        run_id: str | None = None,
        worker_id: str | None = None,
        parent_turn_id: UUID | str | None = None,
    ) -> AsyncIterator[TurnStream]:
        """One turn. `run_id` defaults to this turn, which is what makes a top-level turn a
        run; a worker is handed its caller's `run_id` and a `worker_id`, so its events are
        part of the run that created it rather than a second run nothing links to."""
        autonomy = autonomy or session.autonomy
        turn_id = uuid7()
        started = time.perf_counter()
        usage_total = {"input_tokens": 0, "output_tokens": 0}
        # Pass 1 baseline measurement. Reads state this loop already keeps and feeds
        # nothing back into it; see obs/telemetry.py.
        model = params_for(self.role, self.cfg).model or ""
        tele = telemetry.TurnTelemetry.start(
            cfg=self.cfg, request_id=str(turn_id), session_id=str(session.id),
            role=self.role, actor=self.actor, origin=origin, channel=session.channel,
            autonomy=autonomy, model=model,
        )
        run_id = run_id or str(turn_id)
        rj = RunJournal(self.journal_writer(), run_id, worker_id=worker_id)
        rec = _TurnRecord.start(
            rj,
            cfg=self.cfg,
            turn_id=str(turn_id),
            started=started,
            payload={
                "session_id": str(session.id), "turn_id": str(turn_id),
                "role": self.role, "actor": self.actor, "origin": origin,
                "channel": session.channel, "autonomy": autonomy, "model": model,
                "max_steps": self.cfg.agent.max_steps,
                "input_chars": len(user_text),
                "input_preview": jevents.preview(user_text),
                "parent_turn_id": str(parent_turn_id) if parent_turn_id else None,
            },
        )

        with otel.span(
            "agent.turn", {"agentd.autonomy": autonomy, "agentd.origin": origin}
        ), rec:
            trace_ids = otel.current_ids()
            if record_user_message:
                await repo_archive.append_event(
                    RawEvent(
                        kind="user_message", actor="user", content=user_text,
                        session_id=session.id, turn_id=turn_id,
                    )
                )
            rj.emit(
                "message_appended",
                {
                    "role": "user", "actor": "user", "chars": len(user_text),
                    "preview": jevents.preview(user_text), "trust": "trusted",
                },
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
            #
            # With a handoff in force this is the fresh orchestrator the pass file describes:
            # the same system instructions and the same retrieved memory, plus the handoff
            # object, plus only the conversation after the handoff's watermark. The old
            # context is not copied in beside its own summary - `after_id` is what makes that
            # structural rather than a rule somebody has to remember.
            carried_over = session.handoff
            history = await ctxmod.history_messages(
                session.id, budget_tokens=self.cfg.agent.history_tokens,
                summary=session.summary,
                after_id=carried_over.watermark if carried_over else None,
            )
            messages = ctxmod.build_messages(
                self.cfg, autonomy=autonomy, context_block=context_block,
                history=history, user_text=user_text,
                handoff_block=handoff_mod.render(carried_over) if carried_over else "",
            )
            if extra_system:
                messages.insert(1, {"role": "system", "content": extra_system})
            # The system block and the retrieved memory are not appended to the archive -
            # they are rebuilt every turn - so this event is the journal's only record that
            # the model was given instructions at all, and how much of the budget they took.
            system_chars = sum(
                len(m.get("content") or "") for m in messages if m.get("role") == "system"
            )
            rj.emit(
                "message_appended",
                {
                    "role": "system", "actor": self.actor, "chars": system_chars,
                    "preview": jevents.preview(messages[0].get("content") if messages else ""),
                    "trust": "trusted",
                },
            )

            tctx = ToolContext(
                session_id=session.id, turn_id=turn_id, actor=self.actor, origin=origin,
                # The run a tool call belongs to, so that a delegated sub-agent's events
                # land in this run rather than opening one of their own.
                run_id=run_id,
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
                sid = rj.step_id(steps)
                # Mutated rather than rebuilt, exactly as `tainted` and `private` are: the
                # context is shared with every tool call this step makes, and Pass 3 needs
                # the step a call belonged to in order to key its idempotency hash.
                tctx.step_id = sid
                last_step = step == self.cfg.agent.max_steps - 1
                step_tools = None if last_step else tool_schemas
                if last_step:
                    messages.append({"role": "system", "content": FINAL_NUDGE})
                    rj.emit(
                        "message_appended",
                        {
                            "role": "system", "actor": self.actor, "chars": len(FINAL_NUDGE),
                            "preview": jevents.preview(FINAL_NUDGE), "trust": "trusted",
                        },
                        step_id=sid,
                    )

                text_parts: list[str] = []
                calls = []
                tele.context_sample(messages)
                # How much room is left, read by the runtime from the prompt it is about to
                # send. The orchestrator is never asked this and is never told the answer:
                # nothing here appends a message, and the model sees the same list either
                # way. On the first crossing of a turn the boundary is marked as a `handoff`
                # checkpoint - the trigger session 4a accepted and left with no producer -
                # so the moment the runtime decided the context was nearly spent is in the
                # journal rather than only in this process. A worker's crossing marks
                # nothing: `checkpoint_at` reads the run as mid-worker and declines, which is
                # "workers are the unit of atomicity" derived rather than special-cased here.
                if rec.context_seen(budget.read_messages(messages, cfg=self.cfg)):
                    checkpoint_at(
                        "handoff", run_id=run_id, writer=rj.writer, cfg=self.cfg
                    )
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
                                yield Delta(text=event.text)
                            elif isinstance(event, ReasoningDelta):
                                yield Delta(text=event.text, thinking=True)
                            elif isinstance(event, ToolCallDone):
                                calls.append(event.call)
                            elif isinstance(event, Finish):
                                finish_reason = event.reason
                                tele.usage_seen(event.usage)
                                usage_total["input_tokens"] += event.usage.get("input_tokens", 0)
                                usage_total["output_tokens"] += event.usage.get("output_tokens", 0)
                except LLMError as exc:
                    # No notice on the stream: the failure and its message are the journal's
                    # `agent_finished(status="failed", error=...)`, which a frontend reads
                    # from the feed like everything else. Yielding it here as well is how
                    # the two paths this session deleted came to disagree.
                    await self._record_turn(
                        turn_id, session, origin, autonomy, "error", started,
                        usage_total, refs, trace_ids, str(exc),
                    )
                    tele.finish(
                        status="failed", steps=steps, usage=usage_total, answer="",
                        error=str(exc), trace_ids=trace_ids,
                    )
                    rec.finish(
                        status="failed", steps=steps, answer="", usage=usage_total,
                        usage_reported=tele.usage_reported, error=str(exc),
                    )
                    yield Answer(turn_id=str(turn_id), text="")
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

                rj.emit(
                    "message_appended",
                    {
                        "role": "assistant", "actor": self.actor,
                        "chars": len(assistant_text),
                        "preview": jevents.preview(assistant_text),
                        "trust": "trusted", "tool_calls": len(calls),
                    },
                    step_id=sid,
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
                    # Read before the re-add below, which would otherwise make every call
                    # look like it was to a tool the model had been shown.
                    was_visible = call.name in exposed
                    known = call.name in self.registry.tools
                    rj.emit(
                        "tool_requested",
                        {
                            "call_id": call.id, "name": call.name, "args": args_preview,
                            "visible": was_visible, "known": known,
                        },
                        step_id=sid,
                    )
                    call_started = time.perf_counter()
                    if call.name not in exposed and call.name in self.registry.tools:
                        exposed[call.name] = self.registry.tools[call.name]
                    rj.emit(
                        "tool_started",
                        {"call_id": call.id, "name": call.name},
                        step_id=sid,
                    )
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
                    call_ms = int((time.perf_counter() - call_started) * 1000)
                    if result.ok:
                        rj.emit(
                            "tool_finished",
                            {
                                "call_id": call.id, "name": call.name,
                                "duration_ms": call_ms,
                                "result_chars": len(result.content),
                                "trust": result.trust,
                                "summary": jevents.preview(result.content),
                                "private": private_call,
                                "has_undo": result.undo is not None,
                            },
                            step_id=sid,
                        )
                    else:
                        # One terminal event per call, whatever went wrong. A denial, a
                        # rejected argument list and a handler that raised are all failures
                        # of the call; which one it was is a flag, not a separate type.
                        rj.emit(
                            "tool_failed",
                            {
                                "call_id": call.id, "name": call.name,
                                "duration_ms": call_ms,
                                "error": jevents.preview(result.content),
                                "denied": denied,
                                "invalid_args": bool(result.data.get("invalid_args")),
                                "attempt": int(result.data.get("attempt", 0)),
                                "rule": result.data.get("rule"),
                                "queued_id": result.data.get("queued_id"),
                            },
                            step_id=sid,
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

                # After the batch, never between an assistant's tool calls and their results.
                for note in stuck:
                    messages.append({"role": "system", "content": note})
                    rj.emit(
                        "message_appended",
                        {
                            "role": "system", "actor": self.actor, "chars": len(note),
                            "preview": jevents.preview(note), "trust": "trusted",
                        },
                        step_id=sid,
                    )

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
            status = "abandoned" if steps >= self.cfg.agent.max_steps else "completed"
            tele.finish(
                status=status, steps=steps, usage=usage_total, answer=answer,
                trace_ids=trace_ids,
            )
            # The handoff decision, taken on what this conversation will carry into the
            # next turn rather than on how full this prompt got. Session 5a measured the
            # whole prompt and handed the difference forward: a turn with twelve full-size
            # tool results fills the window and leaves nothing behind it, and compressing a
            # conversation for that would take the lossy path where the lossless one fits.
            # What carries is exactly the archive's own user and assistant rows, which is
            # what `history_messages` will re-read next time.
            carried = budget.carried(
                [
                    *history,
                    {"role": "user", "content": user_text},
                    {"role": "assistant", "content": answer},
                ],
                cfg=self.cfg,
            )
            if carried.crossed:
                handoff = await self._hand_off(
                    session=session, rj=rj, run_id=run_id, reading=carried,
                    messages=[*messages, {"role": "assistant", "content": answer}],
                    previous=carried_over, refs=refs,
                )
                if handoff is not None:
                    rec.handoff_object = handoff.as_dict()
                    session.handoff = handoff
            rec.finish(
                status=status, steps=steps, answer=answer, usage=usage_total,
                usage_reported=tele.usage_reported,
            )
            yield Answer(turn_id=str(turn_id), text=answer)

    async def _hand_off(
        self,
        *,
        session: Session,
        rj: RunJournal,
        run_id: str,
        reading: budget.ContextReading,
        messages: list[dict[str, Any]],
        previous: handoff_mod.Handoff | None,
        refs: dict[str, Any],
    ) -> handoff_mod.Handoff | None:
        """Compress this conversation into a handoff object, or record that it could not be.

        Returns None on failure and never raises into the turn. That is not a swallowed
        error: `handoff_finished(status="failed")` carries the reason, and the turn the user
        is in the middle of is not the place to surface a failure of the machinery that was
        preparing for the *next* one. What it must never do instead is produce an object
        anyway - the pass file's second *Must not* - so there is no partial handoff here and
        no fallback to copying the old context.
        """
        handoff_id = str(uuid7())
        rj.emit(
            "handoff_started",
            {
                "handoff_id": handoff_id,
                "reason": handoff_mod.REASON_THRESHOLD,
                "messages": len(messages),
                "context_tokens": reading.used_tokens,
                "ceiling_tokens": reading.ceiling_tokens,
            },
        )
        started = time.perf_counter()
        sizes = await repo_archive.recent_message_sizes(session.id)
        watermark, kept = handoff_mod.carry_window(
            sizes,
            keep=self.cfg.handoff.carry_messages,
            budget_tokens=self.cfg.handoff.carry_tokens,
        )

        dropped = sizes[kept:]

        def done(status: str, *, chars: int, error: str | None) -> None:
            rj.emit(
                "handoff_finished",
                {
                    "handoff_id": handoff_id,
                    "status": status,
                    "summary_chars": chars,
                    # What the successor will see, and what only the object now holds. On a
                    # failure nothing is dropped, because nothing replaced it.
                    "kept_messages": kept if status == "ok" else len(sizes),
                    "dropped_messages": len(dropped) if status == "ok" else 0,
                    "duration_ms": int((time.perf_counter() - started) * 1000),
                    "error": error,
                    # The successor is the next turn of this session, which has no run id
                    # until it starts. Null is the honest value and not a missing one; 5c
                    # fills it where a resume really does open a new run.
                    "successor_run_id": None,
                },
            )

        try:
            handoff, _source, _ms = await handoff_mod.generate(
                messages,
                run_id=run_id,
                session_id=str(session.id),
                cfg=self.cfg,
                provider=self.provider,
                previous=previous,
                watermark=watermark,
                reading=reading,
                dropped=(len(dropped), sum(chars for _id, chars in dropped)),
                # Derived, never asked of the model. An open worker at a turn boundary is
                # impossible today - a worker is awaited inside the step that created it -
                # so this is empty by construction until Pass 6, and a model-written list
                # here would be the only source of a sub-agent that never existed.
                active_subagents=(),
                important_memory_refs=[str(r) for r in refs.get("items", [])],
            )
        except handoff_mod.HandoffError as exc:
            done("failed", chars=0, error=str(exc))
            return None
        done("ok", chars=len(handoff_mod.render(handoff)), error=None)
        return handoff

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


async def one_shot(
    prompt: str, *, autonomy: str = "assist", channel: str = "cli", approver: Approver | None = None
) -> str:
    """Single turn, no REPL. Used by `agent ask`, watchers and tests."""
    session = await Session.create(channel=channel, autonomy=autonomy)
    loop = AgentLoop(approver=approver)
    answer = ""
    async for event in loop.run_turn(session, prompt, autonomy=autonomy):
        if isinstance(event, Answer):
            answer = event.text
    await repo_archive.end_session(session.id)
    return answer


__all__ = ["AgentLoop", "Session", "one_shot", "utcnow"]
