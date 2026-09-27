"""The turn loop: one user message in, prose out, everything recorded in the journal.

What this loop hands back to its caller is the model's own output (`agent/stream.py`). What
it *records* - every tool call, every message, how the turn ended - goes to the run journal,
and anything that wants to watch that subscribes with `journal/feed.py`. Session 2c removed
the second copy that used to be yielded alongside.
"""

from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator, Iterable
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from ..config import Config, get_config
from ..db import repo_archive, repo_ops
from ..db.repo_archive import RawEvent
from ..db.repo_ops import ActionRecord
from ..ids import utcnow, uuid7
from ..journal import events as jevents
from ..journal.checkpoints import Checkpointer, checkpoint_at
from ..journal.ledger import EffectLedger
from ..journal.runtime import RunJournal, get_writer
from ..journal.writer import JournalWriter
from ..llm.base import Finish, LLMError, ReasoningDelta, TextDelta, ToolCallDone
from ..llm.roles import get_provider, params_for
from ..memory.promotion import promote_scope
from ..obs import otel, telemetry
from ..policy.approvals import Approver, AutoApprover
from ..policy.engine import PolicyEngine, engine_from_config
from ..tools.base import ToolContext
from ..tools.builtin_handoff import LOOKUP
from ..tools.executor import ToolExecutor
from ..tools.registry import Registry, get_registry
from ..tools.surface import orchestrator_surface
from . import budget
from . import context as ctxmod
from . import handoff as handoff_mod
from .observations import RUNTIME_NOTE
from .stream import Answer, Delta, Notice, TurnStream
from .working_memory import ORCHESTRATOR, WorkingMemory, discard_for_turn

# The runtime's two mid-turn notes to the model, and why they are `user` messages carrying
# a marker rather than `system` messages.
#
# They were `system` messages until 2026-09-22. Appending one at the end of the list is
# what this backend's chat template rejects with HTTP 400 "System message must be at the
# beginning", and `sir` turns that 400 into a cancelled stream for every *other* caller on
# the model, which is the whole of `baseline-v2.md` finding v2-1: three suite rows lost
# 600 seconds and returned nothing, on a fault they did not cause. Verified both ways
# against vLLM on :8001 before the change - trailing `system` 400, trailing `user` 200.
#
# Position is why they are not folded into the leading system block instead: "stop calling
# tools now" and "that tool has been withdrawn" are about what just happened, and a 27B
# reads them where they are. The cost of the role is that a `user` message the user did not
# write is now in the list, so each one carries `RUNTIME_NOTE` and `agent/handoff.py`
# refuses anything carrying it - the marker does the work the role used to do for free.
FINAL_NUDGE = (
    f"{RUNTIME_NOTE} You have used your whole tool budget for this turn. Stop calling "
    "tools. Summarize what you found, what you changed, and what is still open."
)

# How many times one tool may reject the identical arguments before the loop takes it away
# for the rest of the turn. Argument validation is deterministic - the same arguments
# against the same schema fail the same way every time - so a repeat is not a retry, it is
# the turn being spent on a call that provably cannot run. Two attempts, because the first
# rejection is the one that carries the repair advice and the model deserves to act on it.
STUCK_LIMIT = 2
STUCK_NUDGE = (
    RUNTIME_NOTE + " `{name}` rejected the same arguments {attempts} times, so it has been "
    "withdrawn for the rest of this turn. Do not look for another way to call it. Carry on "
    "without it, and tell the user plainly what you were unable to do."
)
# Session 9c. The same withdrawal, for the other reason a repeat provably cannot work. A
# denial is a rule's answer about the action, not a complaint about the arguments, so a model
# that rephrases a denied call is appealing to something that has no discretion - and nothing
# in this loop used to tell it so. Measured on the daemon: a heartbeat with `max_steps = 4`
# spent its whole turn retrying one `open_loop_close` it was never going to be allowed.
#
# The counter behind it is keyed on the arguments as well as the rule (`tools/executor.py`),
# so this fires on a verbatim repeat only. A rule may match on an argument - `delegate` is
# denied for `agent="mail"` at `observe` and allowed for the rest - and withdrawing the tool
# on the first denial would take the other four roles away with it.
DENIED_NUDGE = (
    RUNTIME_NOTE + " `{name}` was refused by policy {attempts} times with the same arguments, "
    "so it has been withdrawn for the rest of this turn. A refusal is the rule's answer about "
    "the action, not a problem with how you phrased it: rewording will not change it. Carry on "
    "without it, and tell the user plainly what you were not allowed to do."
)

# Session 9c. What a turn that has spent its budget may still do, and the note that offers it.
#
# `max_steps`, `FINAL_NUDGE` and `STUCK_LIMIT` all used to have exactly one ending between
# them - stop. Ending is the right floor and the wrong ceiling: the four moves §21 gives an
# orchestrator holding a `blocked` worker are just as available to a turn that has run out of
# room, and a runtime that only ends the turn throws them away. So one bounded escape, offered
# once, in which the only tool on the table is `delegate` - a narrower brief is the one move
# that can still finish the work inside a turn with no budget left - followed by the tool-free
# summary step the turn would have had anyway.
#
# `delegate` and nothing else, on purpose. A turn that could reach for any tool here has not
# had its budget spent, it has had it raised, and the next limit would be argued with in the
# same way. A worker gets no escape at all in practice: no role holds `delegate`, so the
# intersection below is empty and its last step is the tool-free one, exactly as before.
ESCAPE_TOOLS: frozenset[str] = frozenset({"delegate"})
ESCAPE_NUDGE = (
    f"{RUNTIME_NOTE} Your tool budget for this turn is spent and you have one move left. "
    "You may call `delegate` once, with a brief narrow enough to be finished in one go - name "
    "the exact file, command or question. Otherwise do not call a tool: answer with what you "
    "have, and say plainly what is still missing and what you would need to get it."
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
    # watermark, and the part of the conversation it replaced is not read again. The durable
    # copy is `handoff_object` on the `turn_end` checkpoint, and session 5c's `resume` puts
    # it back - so a handoff now survives a process, and not only a conversation.
    handoff: handoff_mod.Handoff | None = None

    @classmethod
    async def create(cls, channel: str = "cli", autonomy: str = "assist") -> Session:
        sid = await repo_archive.create_session(channel=channel)
        return cls(id=sid, channel=channel, autonomy=autonomy)

    @classmethod
    async def resume(
        cls, session_id: UUID, autonomy: str = "assist", *, cfg: Config | None = None
    ) -> Session:
        row = await repo_archive.get_session(session_id)
        if row is None:
            raise ValueError(f"No such session: {session_id}")
        return cls(
            id=row["id"], channel=row["channel"], autonomy=autonomy, summary=row.get("summary"),
            # Without this, `agent chat --resume` would silently hand back a session whose
            # interlock had been earned and then forgotten. The archive already knows: every
            # private tool result was written with a marker, so no migration is needed.
            private=await repo_archive.session_read_private(session_id),
            handoff=_stored_handoff(session_id, cfg=cfg),
        )


def _stored_handoff(session_id: UUID, *, cfg: Config | None = None) -> handoff_mod.Handoff | None:
    """The handoff this conversation handed off under, read back off a checkpoint.

    None on three different roads, and all three are honest: the conversation never handed
    off, `[checkpoints] enabled` is off so nothing was stored (session 5b's open question 5,
    still open - the object then survives a conversation and not a process), or the stored
    object was written under rules this build cannot read. The third is the only one worth
    a word, and it gets one rather than a silent `None`: a resumed session that quietly
    dropped a handoff would start replaying the whole archive again, which is the
    compression undone by a restart with every number still looking healthy.

    Never raises into a resume. A conversation that can be continued without its handoff is
    a conversation that costs more context than it should; one that cannot be continued at
    all because reading an accelerator failed is worse.
    """
    cfg = cfg or get_config()
    try:
        stored = Checkpointer.reading(get_writer(cfg).store).latest_handoff_for_session(
            str(session_id)
        )
        return handoff_mod.Handoff.from_dict(stored) if stored else None
    except handoff_mod.HandoffError:
        return None
    except Exception:  # a journal that cannot be read must not stop a conversation
        return None


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
        # Session 7b. The working bucket is execution state, and this is where the task
        # scope it belongs to ends. Only for a turn that is not a worker's: a worker's own
        # scope is discarded by `agent/subagents.py` once its result is journaled, which is
        # after its turn has already unwound through here.
        #
        # Before the checkpoint rather than after it, so that the snapshot of a finished run
        # says the true thing - the run kept nothing - instead of carrying scratch state
        # that the very next event throws away. A crash between the two leaves notes a
        # resume can still fold, which is the safe direction: the alternative loses them
        # first and discovers the run was not over.
        if self.rj.worker_id is None:
            discard_for_turn(self.rj, "run_completed")
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
        tool_subset: Iterable[str] | None = None,
    ) -> None:
        self.cfg = cfg or get_config()
        self.registry = registry or get_registry()
        # This agent's tool surface: the names it may run, by any route. A sub-agent has
        # had one since Pass 6 in the shape of `Registry.subset`; this is the same notion
        # for the orchestrator, which holds the whole registry and so could not express it.
        #
        # `None` here means the caller named no surface, which is what an orchestrator does:
        # the ceiling is then its registry minus the families that have moved to a durable
        # role. See the `tool_subset` property, which resolves that on every read rather than
        # snapshotting it.
        self._tool_subset: set[str] | None = (
            set(tool_subset) if tool_subset is not None else None
        )
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

    @property
    def tool_subset(self) -> set[str]:
        """The names this agent may run, resolved now and not at construction.

        A snapshot taken in `__init__` would be wrong: a caller may register a tool after
        building its loop - `loop.registry.add(probe)` is what several callers and several
        tests do - and that tool would be locked out of its own turn. So the ceiling is read
        off the registry on every access, and session 8a's narrowing is a *subtraction* from
        it rather than a fixed list, which is what keeps that property true.

        Naming no subset means this loop is an orchestrator, and an orchestrator's surface is
        everything its registry holds minus the families that have moved to a durable role
        (`tools/surface.py`). That is the default on purpose: the five places that build an
        orchestrator - `cli/chat.py`, `cli/app.py`, `daemon/{telegram,scheduler,heartbeat}.py`
        - and `one_shot` below all pass nothing, and a sixth added later gets the narrowing
        without anyone remembering to ask for it. A worker states its surface outright in
        `run_subagent`, because a worker's surface is its own subset and subtracting the moved
        families from *it* would take the coder's tools away from the role they moved to.
        """
        if self._tool_subset is not None:
            return self._tool_subset
        return orchestrator_surface(self.registry.tools)

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

            # The conversation so far, read *before* the current message is archived.
            # `build_messages` appends `user_text` as the final turn, so a window that had
            # already absorbed it would state the user's message twice - which is what every
            # prompt did until this ordering was fixed.
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
                user_text, session_used=session.tools_used, cfg=self.cfg,
                permitted=self.tool_subset,
            )
            # Session 5d. The manifest lookup is on this turn's list exactly when there is
            # a manifest to look things up in, and off it otherwise - both directions, so
            # that "is it offered" is decided here and not by whether the user's wording
            # happened to embed near its description. A successor with nothing dropped is
            # not a successor; offering it the tool would be one more call to discover is
            # useless.
            tools = self._with_lookup(tools, carried_over)
            lookup_offered = any(t.name == LOOKUP for t in tools)
            tool_schemas = [t.openai_schema() for t in tools]
            exposed = {t.name: t for t in tools}
            tele.tools_offered(list(exposed), registry_size=len(self.registry.enabled()))

            # 3. Build the messages. The history window was read at the top of the turn,
            # before the current message was archived, so `user_text` is stated once.
            messages = ctxmod.build_messages(
                self.cfg, autonomy=autonomy, context_block=context_block,
                history=history, user_text=user_text,
                # The successor is told to fetch a ref only when it has been given
                # something that can fetch one. Telling a model to call a tool it does not
                # have is a step spent on a call that cannot exist.
                handoff_block=(
                    handoff_mod.render(
                        carried_over, lookup_tool=LOOKUP if lookup_offered else None
                    )
                    if carried_over
                    else ""
                ),
                # A worker's role prompt. It belongs to the one leading system message and
                # not to a second one: Qwen3's chat template answers a system message in any
                # other position with `HTTP 400 System message must be at the beginning`,
                # which is how every delegated turn on this machine failed before the model
                # was ever asked anything.
                extra_system=extra_system or "",
            )
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
                # Carried on the context because two things downstream need it and neither
                # can see this loop: `tool_search`, which must not advertise outside the
                # surface, and the executor, which refuses outside it.
                #
                # Always a concrete set, resolved for this turn, and never `None` even when
                # nothing was narrowed. `None` is reserved for a caller that has no surface
                # at all - a replayed approval, an MCP call - and handing it down from here
                # would switch the executor's last line off for the one caller that has a
                # surface to enforce: a worker, whose subset is its registry and which
                # passes no explicit `tool_subset`.
                tool_subset=self.tool_subset,
            )
            # The approver this loop was built with, so that a tool which starts a second
            # loop - `delegate` - runs its worker against the same one. `builtin_delegate`
            # falls back to a `QueueApprover` when this key is absent, and nothing else
            # ever set it: under `agent chat` every delegated worker was being built with
            # an approver that cannot prompt, so each of its writes came back denied.
            tctx.extra["approver"] = self.approver
            # Session 7b. This turn's working-memory handle, built from the run journal so
            # that its scope is fixed by the run and the worker - a worker's `AgentLoop`
            # builds its own and has no argument with which to name its caller's. Handed
            # through `extra` for the reason the handoff object is: it belongs to this turn,
            # and a tool that held one across turns would be writing into a scope that ended.
            tctx.extra["working_memory"] = WorkingMemory.for_turn(rj)
            if carried_over is not None:
                # The lookup's whole authorisation check: a ref is fetchable only if the
                # handoff in force lists it. Handed through the context rather than bound
                # into the tool, because the object changes every time the conversation
                # hands off again and a tool holding a stale one would resolve refs
                # against a manifest nobody was shown.
                tctx.extra["handoff"] = carried_over
            if correction_cue is not None:
                tctx.extra["correction_cue"] = correction_cue

            # 4. Step until the model stops calling tools.
            final_text: list[str] = []
            steps = 0
            # Session 9c. Whether this turn stopped because it chose to, rather than because
            # the runtime took its tools away. Read at the bottom for the terminal status:
            # "abandoned" should mean the budget ran out with work still pending, not that
            # the budget was fully used. Only a step that was *offered* tools can set it -
            # the forced tool-free step calls no tool by construction, and counting that as
            # a choice would make every turn look deliberate.
            ended_by_choice = False
            # Never at `max_steps = 1`: the escape step sits one before the tool-free one, so
            # granting it to a one-step budget would take the model's only working step away
            # and offer it `delegate` instead. A budget that small is a caller saying "one
            # call, then answer", and the escape has nothing to add to it.
            escape = 1 if self.cfg.agent.escape_step and self.cfg.agent.max_steps >= 2 else 0
            budget_steps = self.cfg.agent.max_steps + escape
            for step in range(budget_steps):
                steps = step + 1
                sid = rj.step_id(steps)
                # Mutated rather than rebuilt, exactly as `tainted` and `private` are: the
                # context is shared with every tool call this step makes, and Pass 3 needs
                # the step a call belonged to in order to key its idempotency hash.
                tctx.step_id = sid
                last_step = step == budget_steps - 1
                # The escape step: the model's tool budget is gone, and what is left is the
                # one move that can still finish the work. Narrowed by intersection rather
                # than by name, so a caller whose surface does not hold `delegate` - every
                # worker - simply gets an empty set and the step behaves as a tool-free one.
                escape_step = bool(escape) and step == budget_steps - 2
                if last_step:
                    step_tools = None
                elif escape_step:
                    step_tools = [
                        s for s in tool_schemas if s["function"]["name"] in ESCAPE_TOOLS
                    ] or None
                else:
                    step_tools = tool_schemas
                nudge = FINAL_NUDGE if last_step else (ESCAPE_NUDGE if escape_step else None)
                if nudge is not None:
                    messages.append({"role": "user", "content": nudge})
                    rj.emit(
                        "message_appended",
                        {
                            # The role that was sent, and the actor who wrote it. Both are
                            # needed to read this row correctly: `user` is what went on the
                            # wire, and `actor` is what says the user did not type it.
                            "role": "user", "actor": self.actor, "chars": len(nudge),
                            "preview": jevents.preview(nudge), "trust": "trusted",
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
                    # The run boundary fires on a failed turn too. The notes are real
                    # work whatever the model call did at the end, and the scope is
                    # discarded either way as this record unwinds.
                    await self._promote(rj, session)
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
                if assistant_text:
                    # The model's own words, archived as they arrive rather than only as
                    # part of the joined answer at the end of a turn that finished. Dylan's
                    # ruling at the Pass 4/5 boundary, taken with the cost that was written
                    # into it: **it fixes nothing for runs already journaled.** What it
                    # fixes from here is the case 5c needs - a turn that died at step 9
                    # leaves the journal holding 200-character previews of what it said,
                    # and session 4b refused to hand previews to a model as bodies. Now the
                    # bodies are in the archive and the refusal costs nothing.
                    #
                    # A separate kind, not `assistant_message`. `recent_messages` selects
                    # user and assistant *messages* to rebuild a prompt's history, and a
                    # kind it does not select cannot reach a later prompt - which is the
                    # whole point: a turn that completes archives its prose twice, once per
                    # step here and once joined below, and exactly one of those is ever
                    # replayed. Two rows in an append-only archive is cheap; the same text
                    # twice in a prompt is the bug this runtime shipped for five passes.
                    await repo_archive.append_event(
                        RawEvent(
                            kind="assistant_step", actor=self.actor,
                            content=assistant_text, session_id=session.id, turn_id=turn_id,
                            payload={"step": steps, "tool_calls": len(calls)},
                        )
                    )

                if not calls:
                    # Tools were on the table and the model did not reach for one. That is an
                    # ending it chose, whether it happened at step 2 or on the escape step.
                    ended_by_choice = step_tools is not None
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
                    # Door 4, kept on purpose. `select` is an embedding lookup and it
                    # misses; a model naming a tool it is entitled to use is recovering
                    # from that miss, not overstepping, and the recovery is worth more than
                    # the tidiness of refusing it. `visible: false, known: true` above is
                    # how often that happens, which is the number Pass 8a wants.
                    #
                    # What it may not do is widen the surface. In-subset only: outside it,
                    # the name is refused by the executor and must not be left sitting in
                    # `exposed`, where the next step would be shown a schema for a tool
                    # that cannot run.
                    if (
                        call.name not in exposed
                        and call.name in self.registry.tools
                        and call.name in self.tool_subset
                    ):
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
                    # Session 8c. Two ways the user's own data gets into this turn, and
                    # both have to raise the same flag. `private_output` is the static one:
                    # this tool, run here, returns the mailbox. The second is a delegation
                    # to a role that holds such a tool - the worker's own `session.private`
                    # died with the worker, and what came back is its answer, which is the
                    # mail. Declared on the result rather than on `delegate` itself because
                    # `delegate` is private for one value of one argument and not for the
                    # others; see `tools/builtin_delegate.py`.
                    private_call = bool(called and called.private_output) or bool(
                        result.data.get("private")
                    )
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
                        # Session 9c. One withdrawal, two reasons, and the note says which:
                        # a rejected argument list is something the model can repair and a
                        # policy refusal is not, so telling it "these arguments were refused"
                        # would send it back to rephrase a call that has already been ruled
                        # on. `denied` is the same flag read above.
                        template = DENIED_NUDGE if denied else STUCK_NUDGE
                        stuck.append(
                            template.format(name=call.name, attempts=attempts)
                        )

                # After the batch, never between an assistant's tool calls and their results.
                for note in stuck:
                    messages.append({"role": "user", "content": note})
                    rj.emit(
                        "message_appended",
                        {
                            "role": "user", "actor": self.actor, "chars": len(note),
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
            # `uncertain` with the runtime's note on it (`agent/results.py`).
            #
            # Session 9c replaced `steps >= max_steps` with this. The old test read the
            # budget being *used up* as work being unfinished, which is not the same thing
            # and was measurably wrong at the edge: the daemon heartbeat runs at
            # `max_steps = 4`, used all four on both of its successful runs, and could
            # therefore never report anything but `abandoned` - so the agenda filed its
            # answers as summaries of unfinished work by construction. A turn that was
            # offered tools and declined them has finished, at whatever step.
            status = "completed" if ended_by_choice else "abandoned"
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
            # Session 7c. The run boundary: the pass file's "task or run boundaries, never
            # opportunistically mid-task", at the only place in a turn where the work is
            # over and the scope still exists.
            await self._promote(rj, session)
            rec.finish(
                status=status, steps=steps, answer=answer, usage=usage_total,
                usage_reported=tele.usage_reported,
            )
            yield Answer(turn_id=str(turn_id), text=answer)

    async def _promote(self, rj: RunJournal, session: Session) -> None:
        """The run boundary: classify this turn's working memory and write what it keeps.

        Session 7c. Called immediately before `rec.finish` on both ways a turn ends, so it
        is inside the turn's own unwind and ahead of `agent_finished`, the discard in
        `_TurnRecord.__exit__` and the `turn_end` checkpoint. That ordering is the point:
        the scope is still readable, and the snapshot that follows accounts for what the
        promotion wrote.

        Only for a turn that is not a worker's. A worker's notes are promoted at its own
        task boundary in `agent/subagents.py`, after its result is journaled; promoting them
        here as well would classify one note twice, and the second batch would be writing
        from a scope its own caller is about to discard.

        The two failures this can actually have are both handled inside `promote_scope` and
        both leave a record: a classifier that will not answer is `promotion_batch` with
        `classifier="failed"` and the message, and a store that will not take the write is a
        ledger row at `failed` with the promotion left *pending* in the journal for a later
        boundary or a resume to finish. Neither loses a note. What is deliberately not
        wrapped here is the journal itself - an append that fails is the one failure this
        codebase makes loud, and a promotion nothing recorded is exactly the state the rest
        of this file exists to make impossible.
        """
        if rj.worker_id is not None:
            return
        await promote_scope(
            rj,
            scope=ORCHESTRATOR,
            session_id=session.id,
            boundary="run",
            cfg=self.cfg,
            # This turn's provider, not the process's. A loop built around a scripted or a
            # role-specific model must classify its own notes with it.
            provider=self.provider,
        )

    def _with_lookup(
        self, tools: list[Any], handoff: handoff_mod.Handoff | None
    ) -> list[Any]:
        """This turn's tools, with the manifest lookup added or taken away.

        Total in both directions on purpose. `select` may return it because it is
        `always_on`, and a turn with no handoff must not have it; a turn with one must,
        whatever `select` thought of the user's wording.
        """
        if handoff is None or not handoff.dropped_manifest:
            return [t for t in tools if t.name != LOOKUP]
        if any(t.name == LOOKUP for t in tools):
            return tools
        # Door 2. A handoff is written by one session and read by the next, so this is the
        # one route on which a decision made *earlier* reaches into this run's surface: a
        # manifest produced while a tool was still on it must not put the tool back. The
        # subset is this run's, the manifest is the last one's, and this run's wins.
        if LOOKUP not in self.tool_subset:
            return tools
        found = self.registry.get(LOOKUP)
        return [*tools, found] if found is not None else tools

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

        # The manifest (session 5c). Everything this session holds that the successor will
        # not see, with a ref against the archive's own identity column. Two different
        # reasons for two kinds of item, which `manifest_rows` draws the line for: a
        # conversation message is gone because it fell below the watermark, a tool result
        # is gone because tool results are never replayed into a later prompt at all - and
        # the second is most of what a turn that spent its step budget actually learned.
        # Everything currently archived, *not* the watermark. The watermark is where the
        # conversation is cut; it is not where the tool output is cut, because tool output
        # is never carried at all. Passing the watermark here looked right and silently
        # dropped the most recent turn's tool results from the manifest - the archive lays
        # a turn down as user message, then results, then the answer, so a watermark below
        # the last user message puts every result that turn produced above it. Those are
        # exactly the rows a successor most needs named.
        upto = await repo_archive.max_event_id(session.id)
        items = handoff_mod.manifest(
            await repo_archive.manifest_rows(
                session.id,
                upto_id=upto,
                watermark=watermark,
                excerpt_chars=self.cfg.handoff.manifest_excerpt_chars,
                limit=self.cfg.handoff.manifest_items,
            )
        )

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
                dropped_manifest=items,
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
