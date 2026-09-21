"""The runtime's own reading of how much room the orchestrator has left.

Why this matters rather than what it does. A handoff exists because a conversation runs out
of context, and something has to notice. Two ways of noticing are worthless here and both
look healthy while they are:

* **Measured against the wrong ceiling.** `llm.max_context_tokens` is 262144 and this
  runtime cannot assemble a prompt near it, so "8000 remaining" against the model window is
  a threshold that never fires - and a monitor that never fires passes every test, because
  a quiet run and a broken monitor produce the same journal.
* **Measured with a number nobody produced.** The router in front of this model drops the
  streaming `usage` chunk, so provider token counts have been 0 on every streamed turn since
  Pass 1. A threshold built on them would read every conversation as empty. What is used
  instead is an estimate, and `context_basis` is the field that refuses to let it be read as
  a count.

The third thing these tests hold down is a *Must not* from the pass file: the orchestrator
does not decide when to hand off. Nothing the runtime learns here is allowed to reach the
model, which is why one of these tests asserts about the prompt rather than about the code.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import pytest
from pydantic import ValidationError

from agentd.agent import budget
from agentd.agent import context as ctxmod
from agentd.agent.loop import AgentLoop, Session
from agentd.config import Config, HandoffConfig
from agentd.db import repo_archive
from agentd.db.repo_archive import RawEvent
from agentd.llm.base import CallParams, Finish, LLMEvent
from agentd.llm.fake import FakeProvider
from agentd.tools.registry import Registry

# ~2,600 characters is ~810 estimated tokens. Twenty of them is a conversation of about
# 16,000 tokens - long, but well inside the 24,000 the history packer will still carry.
LONG_MESSAGE = "the quick brown fox jumps over the lazy dog. " * 59


class SilentProvider(FakeProvider):
    """The provider this system actually has: no usage chunk, ever.

    `FakeProvider` reports usage, which no streamed turn against the real router does. A
    test of token accounting that only ever ran against the optimistic fake would be
    measuring a backend nobody has.
    """

    async def stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        *,
        params: CallParams,
    ) -> AsyncIterator[LLMEvent]:
        async for event in super().stream(messages, tools, params=params):
            yield Finish(reason=event.reason, usage={}) if isinstance(event, Finish) else event


def _loop(cfg: Config, provider: FakeProvider) -> AgentLoop:
    return AgentLoop(cfg=cfg, registry=Registry(), provider=provider)


async def _run(loop: AgentLoop, session: Session, text: str = "hello") -> None:
    async for _ in loop.run_turn(session, text):
        pass


async def _seed_conversation(session_id, messages: int = 20) -> None:
    for i in range(messages):
        await repo_archive.append_event(
            RawEvent(
                kind="user_message" if i % 2 == 0 else "assistant_message",
                actor="user" if i % 2 == 0 else "main",
                content=LONG_MESSAGE,
                session_id=session_id,
            )
        )


def _finished(journaled) -> dict:
    return journaled("agent_finished")[-1].payload


# --- which ceiling, and how the number was obtained --------------------------


def test_remaining_room_is_measured_against_the_budget_this_runtime_enforces(cfg) -> None:
    """Not the model window, which nothing in this system can reach.

    `agent.history_tokens` is the only ceiling the runtime enforces on what carries forward
    - `context.history_messages` silently drops the oldest turns once it is spent - so it is
    the one a handoff threshold is about. 262144 is not a limit; it is a number in a config
    file that no assembled prompt has ever approached.
    """
    assert cfg.llm.max_context_tokens == 262144
    assert budget.ceiling(cfg) == (cfg.agent.history_tokens, budget.SOURCE_HISTORY)
    # And the threshold is meaningful against it: a third of the ceiling still free.
    assert 0 < budget.threshold(cfg) < cfg.agent.history_tokens


def test_the_model_window_wins_when_it_is_the_smaller_of_the_two(cfg) -> None:
    """A ceiling nobody could actually send is not one to report against."""
    narrow = cfg.model_copy(deep=True)
    narrow.llm.max_context_tokens = 5000
    assert budget.ceiling(narrow) == (5000, budget.SOURCE_MODEL_WINDOW)


def test_a_configured_ceiling_says_that_is_where_it_came_from(cfg) -> None:
    chosen = cfg.model_copy(deep=True)
    chosen.handoff.ceiling_tokens = 12000
    assert budget.ceiling(chosen) == (12000, budget.SOURCE_CONFIGURED)


def test_a_prompt_over_the_ceiling_is_negative_room_rather_than_none_left(cfg) -> None:
    """A single turn can put 30k of tool output in front of a 24k budget. Clamping the
    overshoot to zero would report the worst case as merely full."""
    reading = budget.read_messages([{"role": "user", "content": "x" * 96_000}], cfg=cfg)
    assert reading.used_tokens == 30_000
    assert reading.remaining_tokens == cfg.agent.history_tokens - 30_000 < 0
    assert reading.crossed is True


def test_a_threshold_at_or_above_its_ceiling_is_refused_when_the_config_loads(cfg) -> None:
    """It would be crossed on the first step of every conversation, which looks exactly
    like a monitor that works."""
    with pytest.raises(ValidationError, match="must be below the context ceiling"):
        Config(handoff=HandoffConfig(threshold_tokens=cfg.agent.history_tokens))


# --- what a turn records -----------------------------------------------------


async def test_a_turn_records_how_full_its_context_got(cfg, journaled) -> None:
    await _run(_loop(cfg, FakeProvider(turns=["done"])), await Session.create("test"))

    payload = _finished(journaled)
    assert payload["context_tokens"] > 0
    assert payload["context_ceiling_tokens"] == cfg.agent.history_tokens
    assert payload["context_threshold_tokens"] == cfg.handoff.threshold_tokens
    assert payload["context_crossed"] is False
    assert payload["context_basis"] == budget.BASIS_ESTIMATE


async def test_context_is_still_accounted_when_the_provider_reports_no_usage(
    cfg, journaled
) -> None:
    """The state this runtime is actually in. `usage` is 0 and unreported on every streamed
    turn; the context reading has to stand on its own or nothing watches the window."""
    await _run(_loop(cfg, SilentProvider(turns=["done"])), await Session.create("test"))

    payload = _finished(journaled)
    assert payload["usage_reported"] is False
    assert payload["usage"] == {"input_tokens": 0, "output_tokens": 0}
    assert payload["context_tokens"] > 0
    assert payload["context_basis"] == budget.BASIS_ESTIMATE


async def test_an_estimated_context_is_never_recorded_as_a_measured_one(cfg, journaled) -> None:
    """`context_basis` is to `context_tokens` what `usage_reported` is to `usage`. Nothing
    in this session produces a counted number, so nothing in the journal may claim one."""
    await _run(_loop(cfg, FakeProvider(turns=["done"])), await Session.create("test"))

    assert _finished(journaled)["context_basis"] != budget.BASIS_PROVIDER


async def test_a_turn_that_never_reaches_the_model_is_unmeasured_rather_than_zero(
    cfg, journaled, monkeypatch
) -> None:
    """A 0 that means "no prompt was ever built" and a 0 that means "the prompt was empty"
    are different statements, and this codebase's recurring bug is the one that collapses
    them."""

    async def boom(*args, **kwargs):
        raise RuntimeError("archive unavailable")

    monkeypatch.setattr(ctxmod, "history_messages", boom)
    with pytest.raises(RuntimeError):
        await _run(_loop(cfg, FakeProvider(turns=["done"])), await Session.create("test"))

    payload = _finished(journaled)
    assert payload["status"] == "failed"
    assert payload["context_basis"] == budget.BASIS_UNMEASURED
    assert payload["context_tokens"] == 0
    assert payload["context_crossed"] is False


async def test_the_journal_and_the_telemetry_record_agree_about_how_full_a_turn_was(
    cfg, journaled
) -> None:
    """Two records of one number are two numbers that can disagree, so both come from one
    estimator over one message list."""
    await _run(_loop(cfg, FakeProvider(turns=["done"])), await Session.create("test"))

    line = (cfg.paths.logs / "telemetry.jsonl").read_text().strip().splitlines()[-1]
    assert json.loads(line)["context_tokens"]["peak"] == _finished(journaled)["context_tokens"]


# --- the crossing ------------------------------------------------------------


async def test_a_long_conversation_crosses_the_threshold_with_room_left_to_act(
    cfg, journaled
) -> None:
    """The pass's exit criterion: it fires in a forced-long run, and it fires early enough
    that a handoff can still be produced without working at the edge of the window."""
    session = await Session.create("test")
    await _seed_conversation(session.id)

    await _run(_loop(cfg, FakeProvider(turns=["done"])), session, "and what about this?")

    payload = _finished(journaled)
    assert payload["context_crossed"] is True
    # Room left: the crossing is a warning, not a report of an overflow that already
    # happened, and history_messages has not yet begun dropping the oldest turns.
    remaining = payload["context_ceiling_tokens"] - payload["context_tokens"]
    assert 0 < remaining <= payload["context_threshold_tokens"]
    history = await ctxmod.history_messages(session.id, budget_tokens=cfg.agent.history_tokens)
    # The twenty seeded messages plus this turn's own user message and answer: nothing has
    # been dropped yet, which is the point of firing here rather than at the ceiling.
    assert len(history) == 22


async def test_the_model_is_never_asked_to_watch_its_own_context(cfg) -> None:
    """The pass file's first *Must not*: the orchestrator does not decide when to hand off.
    A crossing changes nothing about what the model is sent."""
    session = await Session.create("test")
    await _seed_conversation(session.id)
    provider = FakeProvider(turns=["done"])

    await _run(_loop(cfg, provider), session, "and what about this?")

    sent = [m for call in provider.calls for m in call["messages"]]
    assert sum(1 for m in sent if m["role"] == "system") == 1  # the assembled block, alone
    body = " ".join(m.get("content") or "" for m in sent).lower()
    for word in ("handoff", "hand off", "context limit", "remaining tokens", "threshold"):
        assert word not in body


async def test_a_crossing_marks_the_handoff_boundary_once_per_turn(cfg, journaled) -> None:
    """The `handoff` checkpoint trigger has existed since session 4a with no producer. A
    crossing is what produces it - once, on the first crossing, not once per step."""
    cfg.checkpoints.enabled = True
    session = await Session.create("test")
    await _seed_conversation(session.id)
    provider = FakeProvider(turns=[[("time_now", {})], [("time_now", {})], "done"])

    from agentd.tools import builtin_memory

    loop = AgentLoop(cfg=cfg, registry=Registry(), provider=provider)
    loop.registry.add(*(t for t in builtin_memory.TOOLS if t.name == "time_now"))
    await _run(loop, session, "and what about this?")

    triggers = [e.payload["trigger"] for e in journaled("checkpoint_written")]
    assert triggers.count("handoff") == 1
    assert _finished(journaled)["steps"] >= 3


async def test_the_crossing_is_recorded_even_though_checkpoints_ship_switched_off(
    cfg, journaled
) -> None:
    """The flag decides whether a snapshot is taken, not whether the runtime admits it ran
    out of room: `agent_finished` carries the reading either way."""
    assert cfg.checkpoints.enabled is False
    session = await Session.create("test")
    await _seed_conversation(session.id)

    await _run(_loop(cfg, FakeProvider(turns=["done"])), session, "and what about this?")

    assert journaled("checkpoint_written") == []
    assert _finished(journaled)["context_crossed"] is True


def test_a_reading_exactly_at_the_threshold_counts_as_crossed(cfg) -> None:
    """"8000 remaining" is the line, not the first number past it. Equality has to be
    decided somewhere, and a threshold that fires one token late is a threshold whose
    stated value is not the one it uses."""
    at_the_line = budget.ContextReading(
        used_tokens=cfg.agent.history_tokens - cfg.handoff.threshold_tokens,
        ceiling_tokens=cfg.agent.history_tokens,
        threshold_tokens=cfg.handoff.threshold_tokens,
    )
    assert at_the_line.remaining_tokens == cfg.handoff.threshold_tokens
    assert at_the_line.crossed is True
    one_token_roomier = budget.ContextReading(
        used_tokens=at_the_line.used_tokens - 1,
        ceiling_tokens=at_the_line.ceiling_tokens,
        threshold_tokens=at_the_line.threshold_tokens,
    )
    assert one_token_roomier.crossed is False


def test_a_turn_reports_the_fullest_its_context_got_not_its_last_reading(cfg) -> None:
    """Today a turn's message list only grows, so the peak and the last reading are the
    same number and this is redundant. It is here because the day something trims mid-turn,
    "how close did this turn come to its ceiling" must not quietly become "how much was
    left over at the end"."""
    from agentd.agent.loop import _TurnRecord
    from agentd.journal.runtime import RunJournal, get_writer

    rec = _TurnRecord(
        rj=RunJournal(get_writer(cfg), "run-peak"), turn_id="t", started=0.0, cfg=cfg
    )

    def reading(used: int) -> budget.ContextReading:
        return budget.ContextReading(used, cfg.agent.history_tokens, cfg.handoff.threshold_tokens)

    assert rec.context_seen(reading(1_000)) is False
    assert rec.context_seen(reading(20_000)) is True  # crossed
    assert rec.context_seen(reading(1_200)) is False  # crossed already; smaller prompt
    assert rec.context.used_tokens == 20_000
    assert rec.context_crossed is True
