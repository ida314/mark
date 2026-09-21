"""What survives when a conversation runs out of room, and what is not allowed to.

A handoff is the one lossy step in this runtime. Everything else - the journal, the effect
ledger, checkpoints, fork - is built so that nothing is lost or so that the loss is visible.
Here the conversation really does go away, and the object left in its place is all the
successor gets. So the failures worth testing are not "does it produce something", they are
the three ways it can produce something and still be wrong:

* **A handoff that looks complete and cannot be acted on.** The pass file draws the line at
  `next_actions` and `unresolved_questions`: missing either is a *failed* handoff, not a
  partial one. An empty list is missing. This codebase's recurring bug is a real value
  degrading into a plausible empty one, and a validator that accepted `[]` here would be
  that bug in the one place where the evidence it replaced is already gone.
* **A handoff that summarises the runtime's own words.** Dylan's requirement at the Pass 4/5
  boundary: messages with `synthetic=True` are never summarized. Text the runtime wrote
  about an interrupted call, folded into a summary, becomes a claim the user never made and
  no tool ever returned - and once the conversation is gone there is nothing left to check
  it against. The same applies to `FINAL_NUDGE` and `STUCK_NUDGE`, which are the runtime
  talking to the model and are not flagged at all.
* **A handoff taken when nothing needed handing off.** Session 5a measured the whole
  assembled prompt and handed the consequence forward: a turn with twelve full-size tool
  results fills the window with material that is gone by the next turn. Compressing a
  two-message conversation because of it is the lossy path taken where the lossless one fits,
  which is the pass file's third *Must not*.
"""

from __future__ import annotations

import json

import pytest

from agentd.agent import budget
from agentd.agent import handoff as handoff_mod
from agentd.agent.handoff import Handoff, HandoffDraft, HandoffError, HandoffInvalid
from agentd.agent.loop import FINAL_NUDGE, AgentLoop, Session
from agentd.agent.observations import RUNTIME_PREFIX, ClosingMessage
from agentd.db import repo_archive
from agentd.db.repo_archive import RawEvent
from agentd.llm.base import CallParams, LLMError
from agentd.llm.fake import FakeProvider
from agentd.policy.approvals import AutoApprover
from agentd.policy.engine import engine_from_config
from agentd.tools.base import Tool, ToolResult, obj
from agentd.tools.registry import Registry

# ~2,600 characters is ~810 estimated tokens, the same unit `tests/test_context_budget.py`
# uses. Twenty of them is a conversation that crosses the threshold and has not yet had
# anything dropped from it.
LONG_MESSAGE = "the quick brown fox jumps over the lazy dog. " * 59


def draft(**over) -> HandoffDraft:
    """A draft that would pass, with named fields knocked out one at a time."""
    body = {
        "task": "Rewrite the ingest window sweep",
        "user_intent": "stop deleted rows from surviving a narrow window",
        "current_state": "sweep rewritten, two tests still failing",
        "decisions_made": ["sweep by window, not by row"],
        "constraints": ["do not touch the migration ladder"],
        "completed_actions": ["read connectors/sweep.py", "rewrote the window query"],
        "relevant_evidence": ["sweep.py:88 keeps rows outside the window"],
        "unresolved_questions": ["is a 24h window long enough for the calendar feed?"],
        "next_actions": ["run the connector tests", "report which two still fail"],
    }
    body.update(over)
    return HandoffDraft(**body)


def built(**over) -> Handoff:
    return handoff_mod.build(
        draft(**over),
        run_id="run-1",
        session_id="s1",
        reason=handoff_mod.REASON_THRESHOLD,
        watermark=41,
        source={"messages_read": 9},
        important_memory_refs=["F:01a0"],
    )


def _loop(cfg, provider, *tools: Tool) -> AgentLoop:
    registry = Registry()
    if tools:
        registry.add(*tools)
    return AgentLoop(
        cfg=cfg, registry=registry, engine=engine_from_config(cfg),
        approver=AutoApprover(True), provider=provider,
    )


async def _run(loop: AgentLoop, session: Session, text: str = "hello") -> str:
    answer = ""
    async for event in loop.run_turn(session, text):
        answer = getattr(event, "text", answer)
    return answer


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


def _bulky_tool(chars: int = 8000) -> Tool:
    async def handler(args, ctx):
        return ToolResult(content="x" * chars)

    return Tool(
        name="reads_a_file", description="returns a lot", parameters=obj(path={}),
        handler=handler, effect_class="read", risk="read",
    )


# --- the validator -----------------------------------------------------------


def test_a_handoff_with_no_next_actions_is_a_failure_rather_than_a_partial_one() -> None:
    """The pass file's line, and the shape this codebase keeps producing. A successor handed
    nine good fields and an empty `next_actions` has been told what the work was and not what
    to do, which is worse than being told the handoff failed - it looks like an answer."""
    with pytest.raises(HandoffInvalid) as raised:
        built(next_actions=[])
    assert any("next_actions" in p for p in raised.value.problems)


def test_a_handoff_with_no_unresolved_questions_is_a_failure_rather_than_a_partial_one(
) -> None:
    with pytest.raises(HandoffInvalid) as raised:
        built(unresolved_questions=[])
    assert any("unresolved_questions" in p for p in raised.value.problems)


def test_a_list_of_blank_strings_is_the_same_as_no_list_at_all() -> None:
    """The near miss. A model that fills the field with `[""]` has satisfied a length check
    and said nothing, and "it is present" is exactly the kind of evidence this module is not
    allowed to accept."""
    with pytest.raises(HandoffInvalid):
        built(next_actions=["", "   "])


@pytest.mark.parametrize("field", handoff_mod.REQUIRED_TEXT)
def test_a_handoff_that_cannot_say_what_the_work_is_is_refused(field: str) -> None:
    """`current_state` is the one that matters most and the easiest to leave blank: a
    successor that knows the task and not where it got to will start it again."""
    with pytest.raises(HandoffInvalid) as raised:
        built(**{field: "  "})
    assert any(field in p for p in raised.value.problems)


def test_every_problem_is_named_so_the_generator_can_be_told_what_was_wrong() -> None:
    """A repair pass that cannot say what failed is a re-roll, and a re-roll against a local
    27B is how the same empty field comes back twice."""
    found = handoff_mod.problems(draft(next_actions=[], current_state=""))
    assert len(found) == 2
    assert all(len(p) > 20 for p in found)


def test_a_complete_handoff_carries_every_field_the_pass_file_names() -> None:
    stored = built().as_dict()
    for name in handoff_mod.FIELDS:
        assert name in stored
    assert stored["handoff_schema"] == handoff_mod.SCHEMA_VERSION


def test_a_stored_handoff_is_only_read_back_under_the_rules_it_was_written_under() -> None:
    """5a's open question 2, answered in this object rather than inherited: five
    `agent_finished` rows stopped validating when a session added required fields, and
    nothing could tell which rules a given row was written under."""
    original = built()
    stored = original.as_dict()
    assert Handoff.from_dict(stored) == original
    stored["handoff_schema"] = 99
    with pytest.raises(HandoffError, match="handoff_schema"):
        Handoff.from_dict(stored)


# --- what the generator may read ---------------------------------------------


def test_the_runtimes_own_words_about_a_call_are_never_handed_to_the_summariser() -> None:
    """Dylan's requirement at the Pass 4/5 boundary, enforced by the flag rather than by the
    prefix string. `ClosingMessage` is the runtime saying "this call was interrupted and the
    outcome is unknown"; summarised, that becomes "the tool reported it could not complete",
    which is a claim no tool made and which nothing downstream can check once the
    conversation it came from is gone."""
    closing = ClosingMessage(
        tool_call_id="c1", tool="fs_write", status="uncertain",
        content=f"{RUNTIME_PREFIX} This call was interrupted by the process exiting.",
    )
    assert closing.synthetic is True
    messages = [
        {"role": "user", "content": "write the file"},
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "fs_write", "arguments": "{}"}}
        ]},
        {"role": "tool", "tool_call_id": "c1", "content": closing.content},
    ]

    src = handoff_mod.source(messages, synthetic_ids=[closing.tool_call_id])

    assert src.synthetic_excluded == 1
    assert all(RUNTIME_PREFIX not in (m.get("content") or "") for m in src.messages)
    assert RUNTIME_PREFIX not in handoff_mod.transcript(
        src, excerpt_chars=1000, total_chars=10000
    )


def test_a_synthetic_message_is_refused_by_its_flag_and_not_by_its_wording() -> None:
    """The flag is the mechanism. A message that says it is synthetic and carries no runtime
    prefix is still refused, because the prefix is a second signal for a person reading a
    transcript and not the rule."""
    src = handoff_mod.source(
        [
            {"role": "user", "content": "real"},
            {"role": "tool", "tool_call_id": "c9", "content": "no prefix here",
             "synthetic": True},
        ]
    )
    assert src.synthetic_excluded == 1
    assert src.read == 1


def test_runtime_text_that_lost_its_flag_is_still_refused_and_counted() -> None:
    """Belt and braces, and the count is in the stored object rather than in a log: a caller
    that built a message list with runtime text in it and no flag is a bug, and it is one
    that would otherwise be invisible in exactly the record that replaced the evidence."""
    src = handoff_mod.source(
        [{"role": "tool", "tool_call_id": "c1", "content": f"{RUNTIME_PREFIX} nope"}]
    )
    assert src.unflagged_runtime_text == 1
    assert src.as_dict()["unflagged_runtime_text"] == 1
    assert src.messages == ()


def test_the_runtimes_notes_to_the_model_are_never_summarised_as_conversation() -> None:
    """`FINAL_NUDGE` and `STUCK_NUDGE` carry no `synthetic` flag and never will - they are
    not tool results. They are the runtime talking to the model, and a handoff that read them
    would report "the user said to stop calling tools"."""
    src = handoff_mod.source(
        [
            {"role": "system", "content": "you are an agent"},
            {"role": "user", "content": "find the file"},
            {"role": "system", "content": FINAL_NUDGE},
        ]
    )
    assert src.system_excluded == 2
    assert [m["content"] for m in src.messages] == ["find the file"]


def test_a_tool_result_is_labelled_with_the_tool_that_produced_it() -> None:
    """So that a summariser cannot attribute a result to the user or to the model. The name
    comes from the assistant message that made the call, which is the only place it is."""
    body = handoff_mod.transcript(
        handoff_mod.source(
            [
                {"role": "assistant", "content": None, "tool_calls": [
                    {"id": "c1", "type": "function",
                     "function": {"name": "web_fetch", "arguments": "{}"}}
                ]},
                {"role": "tool", "tool_call_id": "c1", "content": "page body"},
            ]
        ),
        excerpt_chars=100, total_chars=10000,
    )
    assert "tool web_fetch: page body" in body
    assert "assistant (called web_fetch)" in body


def test_an_oversized_transcript_drops_its_oldest_messages_and_says_how_many() -> None:
    src = handoff_mod.source([{"role": "user", "content": f"m{i} " + "x" * 400} for i in range(20)])
    body = handoff_mod.transcript(src, excerpt_chars=1000, total_chars=2000)
    assert "earlier messages were not shown to the handoff generator" in body
    assert "m19" in body and "m0 " not in body


# --- generation --------------------------------------------------------------


async def test_a_generator_that_leaves_a_required_field_empty_is_told_and_asked_again(
    cfg,
) -> None:
    """One repair, at this module's level rather than pydantic's. A cross-field rule on the
    schema would spend `complete_json`'s own repair attempt and then raise `LLMError` into
    the middle of somebody's turn."""
    provider = FakeProvider(json_results=[draft(next_actions=[]), draft()])

    handoff, src, _ms = await handoff_mod.generate(
        [{"role": "user", "content": "do the thing"}],
        run_id="run-1", cfg=cfg, provider=provider,
    )

    assert handoff.next_actions
    assert src.read == 1
    asked = [c for c in provider.calls if "json_schema" in c]
    assert len(asked) == 2
    assert "next_actions" in asked[1]["messages"][-1]["content"]


async def test_a_second_empty_draft_fails_the_handoff_rather_than_storing_a_thin_one(
    cfg,
) -> None:
    provider = FakeProvider(json_results=[draft(next_actions=[]), draft(next_actions=[])])

    with pytest.raises(HandoffInvalid):
        await handoff_mod.generate(
            [{"role": "user", "content": "do the thing"}],
            run_id="run-1", cfg=cfg, provider=provider,
        )


async def test_a_model_call_that_fails_is_a_failed_handoff_and_not_an_empty_one(cfg) -> None:
    class Broken(FakeProvider):
        async def complete_json(self, messages, schema, *, params: CallParams):
            raise LLMError("backend went away mid-swap")

    with pytest.raises(HandoffError, match="backend went away"):
        await handoff_mod.generate(
            [{"role": "user", "content": "do the thing"}],
            run_id="run-1", cfg=cfg, provider=Broken(),
        )


async def test_memory_refs_come_from_the_runtime_rather_than_from_the_model(cfg) -> None:
    """The retrieval pack knows its own refs exactly. A model-written `[F:01a0]` in a field
    the successor will look things up from is the laundering channel in a smaller font, so
    the field is not in the schema the model is given at all."""
    assert "important_memory_refs" not in HandoffDraft.model_fields
    assert "active_subagents" not in HandoffDraft.model_fields

    handoff, _src, _ms = await handoff_mod.generate(
        [{"role": "user", "content": "do the thing"}],
        run_id="run-1", cfg=cfg, provider=FakeProvider(json_results=[draft()]),
        important_memory_refs=["F:01a0", "C:7f31"],
    )

    assert handoff.important_memory_refs == ("F:01a0", "C:7f31")
    assert handoff.active_subagents == ()


async def test_a_second_handoff_is_told_to_carry_the_first_one_forward(cfg) -> None:
    """The successor never sees the previous handoff separately, so if the generator is not
    given it the chain loses everything before the last watermark - silently, and one
    handoff at a time."""
    first = built()
    provider = FakeProvider(json_results=[draft()])

    handoff, _src, _ms = await handoff_mod.generate(
        [{"role": "user", "content": "and now this"}],
        run_id="run-2", cfg=cfg, provider=provider, previous=first,
    )

    prompt = " ".join(m["content"] for m in provider.calls[0]["messages"])
    assert first.task in prompt
    assert "sweep by window, not by row" in prompt
    assert handoff.source["supersedes"] == first.handoff_id


# --- the decision: which reading a handoff is taken on -----------------------


def test_the_two_readings_answer_different_questions(cfg) -> None:
    """5a's open question 1, decided here. The prompt reading counts a tool result that is
    gone by the next turn; the carried reading counts what the archive will replay."""
    messages = [
        {"role": "system", "content": "s" * 3_200},
        {"role": "user", "content": "u" * 3_200},
        {"role": "tool", "tool_call_id": "c1", "content": "t" * 64_000},
        {"role": "assistant", "content": "a" * 3_200},
    ]

    assert budget.read_messages(messages, cfg=cfg).used_tokens == 23_000
    assert budget.carried(messages, cfg=cfg).used_tokens == 2_000
    assert budget.read_messages(messages, cfg=cfg).crossed is True
    assert budget.carried(messages, cfg=cfg).crossed is False


async def test_a_tool_heavy_turn_does_not_hand_off_a_two_message_conversation(
    cfg, journaled
) -> None:
    """Baseline task B22's shape: breadth against the step budget, on a conversation with
    nothing in it. The prompt crosses - 5a records that and marks the boundary, which is
    true about the prompt - and no handoff is generated, because the material that filled
    the window does not carry and the next turn starts roomy without anyone compressing
    anything."""
    cfg.checkpoints.enabled = True
    provider = FakeProvider(
        turns=[[("reads_a_file", {"path": f"f{i}"}) for i in range(8)], "here is the table"],
        json_results=[draft()],
    )

    await _run(_loop(cfg, provider, _bulky_tool()), await Session.create("test"), "list the tools")

    finished = journaled("agent_finished")[-1].payload
    assert finished["context_crossed"] is True
    assert journaled("handoff_started") == []
    triggers = [e.payload["trigger"] for e in journaled("checkpoint_written")]
    assert "handoff" in triggers
    assert all(c["handoff_object"] is None for c in _checkpoints(cfg))


async def test_a_conversation_too_long_to_carry_hands_off(cfg, journaled) -> None:
    """Baseline task B23's shape: the pressure is across turns, where `history_messages`
    would otherwise start dropping the oldest of them without telling anyone."""
    cfg.checkpoints.enabled = True
    session = await Session.create("test")
    await _seed_conversation(session.id)
    provider = FakeProvider(turns=["done"], json_results=[draft()])

    await _run(_loop(cfg, provider), session, "and what about this?")

    started = journaled("handoff_started")
    done = journaled("handoff_finished")
    assert len(started) == len(done) == 1
    assert started[0].payload["reason"] == handoff_mod.REASON_THRESHOLD
    assert started[0].payload["context_tokens"] > 0
    assert done[0].payload["status"] == "ok"
    assert done[0].payload["summary_chars"] > 0
    assert done[0].payload["kept_messages"] == cfg.handoff.carry_messages
    assert done[0].payload["dropped_messages"] > 0
    assert session.handoff is not None


# --- where it is stored ------------------------------------------------------


def _checkpoints(cfg) -> list[dict]:
    from agentd.journal.runtime import get_writer

    writer = get_writer(cfg)
    writer.flush()
    return writer.store.query("SELECT * FROM checkpoint ORDER BY event_seq")


async def test_the_handoff_is_stored_on_the_next_checkpoint_after_the_crossing(
    cfg, journaled
) -> None:
    """The slot Pass 4 left inert. The `handoff` checkpoint that 5a marks is the moment the
    runtime decided, and it carries nothing; the object exists only once the turn has
    finished, which is the `turn_end` boundary immediately after it."""
    cfg.checkpoints.enabled = True
    session = await Session.create("test")
    await _seed_conversation(session.id)

    await _run(
        _loop(cfg, FakeProvider(turns=["done"], json_results=[draft()])),
        session, "and what about this?",
    )

    rows = _checkpoints(cfg)
    assert [r["trigger"] for r in rows] == ["handoff", "turn_end"]
    assert rows[0]["handoff_object"] is None
    assert '"next_actions"' in rows[1]["handoff_object"]
    stored = Handoff.from_dict(json.loads(rows[1]["handoff_object"]))
    assert stored.next_actions == session.handoff.next_actions
    assert stored.watermark == session.handoff.watermark


async def test_a_failed_generation_leaves_no_object_and_does_not_break_the_turn(
    cfg, journaled
) -> None:
    """The failure has to be visible and has to be survivable. `handoff_object` stays NULL,
    which is what "no handoff" looks like; the `handoff_finished(status="failed")` in front
    of it is what tells a fold the difference between that and nobody having tried."""
    cfg.checkpoints.enabled = True
    session = await Session.create("test")
    await _seed_conversation(session.id)
    # No `json_results`: the fake returns an empty draft, twice, which is a generator that
    # produced something plausible and unusable.
    provider = FakeProvider(turns=["done"])

    answer = await _run(_loop(cfg, provider), session, "and what about this?")

    assert answer == "done"
    assert journaled("agent_finished")[-1].payload["status"] == "completed"
    failed = journaled("handoff_finished")[0].payload
    assert failed["status"] == "failed"
    assert "next_actions" in failed["error"]
    assert session.handoff is None
    assert all(r["handoff_object"] is None for r in _checkpoints(cfg))


# --- the successor -----------------------------------------------------------


async def test_the_successor_is_started_from_the_handoff_and_not_from_the_old_conversation(
    cfg,
) -> None:
    """The pass file's second *Must not*, asserted against the prompt rather than the code:
    the turn after a handoff must not contain the conversation the handoff replaced. What it
    does contain is the handoff, the last few messages, and nothing else."""
    session = await Session.create("test")
    await _seed_conversation(session.id)
    provider = FakeProvider(turns=["done", "still here"], json_results=[draft()])
    loop = _loop(cfg, provider)

    await _run(loop, session, "and what about this?")
    assert session.handoff is not None
    await _run(loop, session, "carry on")

    successor = [c for c in provider.calls if "json_schema" not in c][-1]["messages"]
    system = successor[0]["content"]
    assert handoff_mod.RENDER_HEADING.splitlines()[0] in system
    assert "Rewrite the ingest window sweep" in system
    assert "run the connector tests" in system
    # The system block, the four messages after the watermark, and this turn's own user
    # message twice - the loop archives it before it reads the history back, which predates
    # this session and is why `history` and `user_text` overlap on every turn. The twenty
    # seeded messages are gone: they are what the handoff replaced.
    assert len(successor) == 1 + cfg.handoff.carry_messages + 2
    body = " ".join(m.get("content") or "" for m in successor)
    assert body.count(LONG_MESSAGE) <= cfg.handoff.carry_messages


async def test_a_successor_is_told_the_handoff_is_the_runtimes_summary_not_the_user(
    cfg,
) -> None:
    """The block is a compression written by a model about a conversation. A successor that
    read it as the user's own words would quote it back as something they said."""
    block = handoff_mod.render(built())
    assert "written by the runtime, not said by the user" in block
    assert "not available to you" in block


async def test_the_successor_stops_re_reading_what_the_handoff_already_states(cfg) -> None:
    """The watermark is what makes that structural. Without it the fresh orchestrator would
    get the handoff *and* the conversation it summarises, which is the old context copied in
    beside its own summary."""
    session = await Session.create("test")
    await _seed_conversation(session.id, messages=10)
    sizes = await repo_archive.recent_message_sizes(session.id)
    watermark, kept = handoff_mod.carry_window(sizes, keep=4, budget_tokens=4_000)

    from agentd.agent import context as ctxmod

    everything = await ctxmod.history_messages(session.id, budget_tokens=cfg.agent.history_tokens)
    after = await ctxmod.history_messages(
        session.id, budget_tokens=cfg.agent.history_tokens, after_id=watermark
    )
    assert len(everything) == 10
    assert len(after) == kept == 4


async def test_a_conversation_shorter_than_the_carry_window_has_no_watermark(cfg) -> None:
    """None is an answer about this conversation, not a missing value: there is nothing
    above which to draw a line, so the successor sees all of it."""
    session = await Session.create("test")
    await _seed_conversation(session.id, messages=2)
    sizes = await repo_archive.recent_message_sizes(session.id)
    assert handoff_mod.carry_window(sizes, keep=4, budget_tokens=4_000) == (None, 2)


def test_the_carry_window_is_bounded_by_tokens_before_it_is_bounded_by_count() -> None:
    """The failure this exists to stop: a handoff that costs a model call and frees nothing.
    Four messages of a 42,000-character paste is a successor inheriting most of what the
    handoff was supposed to replace, and every number about it still looks healthy."""
    huge = [(90, 200), (89, 44_000), (88, 44_000), (87, 200)]
    assert handoff_mod.carry_window(huge, keep=4, budget_tokens=2_000) == (89, 1)
    small = [(90, 200), (89, 200), (88, 200), (87, 200), (86, 200)]
    assert handoff_mod.carry_window(small, keep=4, budget_tokens=2_000) == (86, 4)


def test_a_message_too_large_to_carry_is_left_to_the_handoff_object() -> None:
    """Not carried anyway "because it is the last thing said". The object is what holds it,
    and a successor that starts at the threshold has been handed the problem, not a fix."""
    assert handoff_mod.carry_window([(9, 80_000), (8, 10)], keep=4, budget_tokens=2_000) == (9, 0)


def test_the_successor_is_told_how_much_was_taken_away_and_not_only_that_some_was() -> None:
    """The first live run of baseline task B23 obeyed the general warning when asked what the
    user had *said* and ignored it when asked about the material they had pasted - it answered
    a question about 42,000 characters it no longer had, confidently and wrongly. A quantity
    is what a general warning is not: 5 messages, 38,000 characters, gone."""
    handoff = handoff_mod.build(
        draft(),
        run_id="run-1", session_id="s1", reason=handoff_mod.REASON_THRESHOLD,
        watermark=41, source={"dropped_messages": 5, "dropped_chars": 38_000},
    )
    block = handoff_mod.render(handoff)
    assert "5 earlier messages (38,000 characters)" in block
    assert "say that you no longer have it" in block


def test_a_handoff_that_dropped_nothing_does_not_claim_it_dropped_something() -> None:
    """A conversation shorter than the carry window keeps all of it. Rendering "0 earlier
    messages were replaced" would be the runtime describing a loss that did not happen."""
    assert "were replaced by this summary" not in handoff_mod.render(built())
