"""Picking a dead run back up, and refusing to do the one thing a resume must not do.

Session 5c. Two halves that are the same subject from opposite ends.

**The path.** The pass file's resume policy has one ordering rule and everything else
follows from it: replay the conversation wherever replaying it fits, and compress only when
it does not. A handoff is the lossy step in this runtime, and taking it when the lossless
step was available is the pass's third *Must not*. It is also the failure that looks like
success from every angle - a handoff is generated, a successor starts, the numbers are
healthy, and a conversation that would have come back whole came back as a summary.

**The guard.** Dylan's requirement B, filed at the Pass 4/5 boundary and inherited here
because 5b consumed neither `notice()` nor `closing_messages()` and said so. A crash can
leave an `unsafe_write` announced and never answered; the resumed turn is told so and told
not to re-run it, and *that sentence was the entire guard*. The re-issued call mints a new
idempotency key, because `step_id` is part of the key and the resumed turn is at a
different step - so nothing downstream recognises it as the same call. The model being
asked to comply is a local 27B.

The tests below are written against the specific ways each half fails quietly:

- a conversation that fits is compressed anyway, and nothing says it happened;
- a stale run is replayed because nobody checked the clock;
- a run with no stored handoff resumes with no handoff at all rather than generating one;
- the guard keys on the idempotency key, which is exactly the hole it exists to close;
- the guard fires on an `idempotent_write`, which trains the user to confirm blind.
"""

from __future__ import annotations

import pytest

from agentd.agent import rehydrate
from agentd.agent.loop import AgentLoop, Session
from agentd.db import repo_archive
from agentd.db.repo_archive import RawEvent
from agentd.journal.ledger import EffectLedger
from agentd.journal.runtime import RunJournal
from agentd.journal.store import JournalStore
from agentd.journal.writer import JournalWriter
from agentd.llm.fake import FakeProvider
from agentd.policy.approvals import AutoApprover, RerunGrants
from agentd.policy.engine import engine_from_config
from agentd.tools.base import Tool, ToolContext, ToolResult, obj
from agentd.tools.executor import ToolExecutor
from agentd.tools.registry import Registry

LONG = "the ingest window sweep, in detail. " * 400  # ~14,000 characters


@pytest.fixture
def writer(tmp_path) -> JournalWriter:
    w = JournalWriter(JournalStore(tmp_path / "cold.db"))
    yield w
    w.close()


def _started(rj: RunJournal, session_id: str, *, turn_id: str = "t1") -> None:
    rj.emit(
        "agent_started",
        {
            "session_id": session_id, "turn_id": turn_id, "role": "main", "actor": "main",
            "origin": "interactive", "channel": "cli", "autonomy": "assist", "model": "m",
            "max_steps": 8, "input_chars": 2, "input_preview": "hi", "parent_turn_id": None,
        },
    )


def _said(rj: RunJournal, role: str, text: str, *, step_id: str | None = None) -> None:
    rj.emit(
        "message_appended",
        {
            "role": role, "actor": "user" if role == "user" else "main",
            "chars": len(text), "preview": text[:200], "trust": "trusted",
            **({"tool_calls": 0} if role == "assistant" else {}),
        },
        step_id=step_id,
    )


async def _archive(session_id, kind: str, actor: str, content: str) -> None:
    await repo_archive.append_event(
        RawEvent(kind=kind, actor=actor, content=content, session_id=session_id)
    )


def _fake(*drafts) -> FakeProvider:
    return FakeProvider(json_results=list(drafts))


DRAFT = {
    "task": "Rewrite the sweep",
    "user_intent": "stop deleted rows surviving",
    "current_state": "two tests failing",
    "unresolved_questions": ["is 24h long enough"],
    "next_actions": ["run the connector tests"],
}


# --- which path, and why -----------------------------------------------------


async def test_a_recent_conversation_that_still_fits_is_replayed_rather_than_summarised(
    cfg, writer
):
    """The pass file's third *Must not*, as the default rather than as a check. A run killed
    two minutes into a four-message conversation comes back whole: nothing is lost, no model
    call is spent, and no successor has to be told what it no longer has."""
    session = await Session.create("test")
    rj = RunJournal(writer, "run-1")
    _started(rj, str(session.id))
    await _archive(session.id, "user_message", "user", "where is the tool cap")
    await _archive(session.id, "assistant_message", "main", "registry.py:20")

    writer.flush()
    plan = await rehydrate.restart("run-1", store=writer.store, cfg=cfg, now_s=30.0)
    assert plan.path == rehydrate.LOSSLESS
    assert plan.reason is None
    assert plan.handoff is None
    assert [m["content"] for m in plan.history] == [
        "where is the tool cap", "registry.py:20"
    ]


async def test_a_conversation_too_large_to_replay_is_compressed_and_says_which_it_was(
    cfg, writer
):
    """The other branch, and the field that distinguishes them. "Compressed" with no reason
    beside it is a decision indistinguishable from a coin toss when somebody reads it back
    in a month."""
    session = await Session.create("test")
    rj = RunJournal(writer, "run-1")
    _started(rj, str(session.id))
    for _ in range(4):
        await _archive(session.id, "user_message", "user", LONG)

    small = cfg.model_copy(
        update={"handoff": cfg.handoff.model_copy(update={"resume_budget_tokens": 500})}
    )
    writer.flush()
    plan = await rehydrate.restart(
        "run-1", store=writer.store, cfg=small, provider=_fake(DRAFT), now_s=30.0
    )
    assert plan.path == rehydrate.COMPRESSED
    assert plan.reason == rehydrate.TOO_LARGE
    assert plan.handoff is not None


async def test_a_stale_run_is_compressed_even_though_it_would_have_fitted(cfg, writer):
    """`warm_window_s`. A conversation picked up the next morning is usually being picked up
    about something else, and replaying it verbatim spends the new turn's context on it."""
    session = await Session.create("test")
    rj = RunJournal(writer, "run-1")
    _started(rj, str(session.id))
    await _archive(session.id, "user_message", "user", "short")

    writer.flush()
    plan = await rehydrate.restart(
        "run-1", store=writer.store, cfg=cfg, provider=_fake(DRAFT),
        now_s=cfg.handoff.warm_window_s + 1,
    )
    assert plan.path == rehydrate.COMPRESSED
    assert plan.reason == rehydrate.TOO_OLD


async def test_a_stored_handoff_is_used_rather_than_a_second_one_being_paid_for(cfg, writer):
    """A checkpoint that carries one is the cheap answer, and the generator is a model call.
    Reaching for the model when the object is already on disk is the acceleration structure
    being ignored by the thing it exists to accelerate."""
    from agentd.agent import handoff as handoff_mod

    session = await Session.create("test")
    stored = handoff_mod.build(
        handoff_mod.HandoffDraft(**DRAFT),
        run_id="run-1", session_id=str(session.id),
        reason=handoff_mod.REASON_THRESHOLD, watermark=None, source={},
    ).as_dict()

    rj = RunJournal(writer, "run-1")
    _started(rj, str(session.id))
    await _archive(session.id, "user_message", "user", LONG * 3)
    writer.flush()
    from agentd.journal.checkpoints import Checkpointer

    Checkpointer(writer, cfg=cfg.model_copy(
        update={"checkpoints": cfg.checkpoints.model_copy(update={"enabled": True})}
    )).write("run-1", trigger="turn_end", handoff_object=stored)

    provider = _fake()  # would return an empty draft, which cannot validate
    writer.flush()
    plan = await rehydrate.restart(
        "run-1", store=writer.store, cfg=cfg, provider=provider, now_s=30.0
    )
    assert plan.handoff_source == rehydrate.FROM_CHECKPOINT
    assert plan.handoff is not None
    assert plan.handoff.task == DRAFT["task"]
    assert plan.generated_ms is None
    # ...and the conversation is *not* replayed, although it is recent and still fits. This
    # assertion is here because the first version of this test failed on it: a conversation
    # crosses the threshold at 16,000 estimated tokens and the resume budget is 24,000, so
    # a run that had just handed off would have been replayed whole, throwing away a
    # handoff somebody paid a model call for and re-crossing at the end of the first
    # resumed turn.
    assert plan.path == rehydrate.COMPRESSED
    assert plan.reason == rehydrate.ALREADY_COMPRESSED


async def test_a_run_with_no_stored_handoff_gets_one_generated_from_the_journal(cfg, writer):
    """The pass file, verbatim: "If a cold resume needs a handoff object and none exists,
    generate one from the journal with a cheap model call before starting the new
    orchestrator." Checkpoints ship off, so this is the ordinary case and not the exotic
    one."""
    session = await Session.create("test")
    rj = RunJournal(writer, "run-1")
    _started(rj, str(session.id))
    await _archive(session.id, "user_message", "user", LONG)
    await _archive(session.id, "tool_result", "tool:fs_read", "TOP_K = 8")

    writer.flush()
    plan = await rehydrate.restart(
        "run-1", store=writer.store, cfg=cfg, provider=_fake(DRAFT),
        now_s=cfg.handoff.warm_window_s + 1,
    )
    assert plan.handoff_source == rehydrate.FROM_JOURNAL
    assert plan.handoff is not None
    assert plan.handoff.reason == "cold_resume"
    assert plan.handoff.dropped_manifest, "a generated handoff still says what it dropped"
    assert plan.source["archived_by_role"]["tool"] == 1


async def test_the_generator_is_shown_bodies_from_the_archive_not_previews(cfg, writer):
    """Session 4b's refusal, kept. The journal holds 200 characters of a 14,000-character
    message; handing that to a model as the message is how text nobody said becomes text
    somebody said."""
    session = await Session.create("test")
    rj = RunJournal(writer, "run-1")
    _started(rj, str(session.id))
    _said(rj, "user", LONG)
    await _archive(session.id, "user_message", "user", LONG)

    provider = _fake(DRAFT)
    writer.flush()
    await rehydrate.restart(
        "run-1", store=writer.store, cfg=cfg, provider=provider,
        now_s=cfg.handoff.warm_window_s + 1,
    )
    sent = "\n".join(
        m.get("content") or "" for call in provider.calls for m in call["messages"]
    )
    # 1,500 characters, which is `handoff.excerpt_chars` - the generator's own bound on one
    # message, and seven times what the journal holds. What matters is which of the two it
    # came from: the marker says the *generator* truncated it, not that the body was never
    # kept.
    assert LONG[:1400] in sent
    assert "not shown to the handoff generator" in sent
    assert "preview only" not in sent


async def test_prose_the_archive_never_held_is_offered_as_a_preview_and_counted(cfg, writer):
    """The stated cost of Dylan's ruling: archiving mid-turn prose "fixes nothing for runs
    already journaled". A run recorded before `assistant_step` existed has only previews of
    what the model said, and the honest move is to show them *as previews* and say how many
    - not to quietly present 200 characters as a message."""
    session = await Session.create("test")
    rj = RunJournal(writer, "run-1")
    _started(rj, str(session.id))
    _said(rj, "assistant", "I read registry.py and the cap is on line 20", step_id="p1")
    await _archive(session.id, "user_message", "user", "where is the cap")

    provider = _fake(DRAFT)
    writer.flush()
    plan = await rehydrate.restart(
        "run-1", store=writer.store, cfg=cfg, provider=provider,
        now_s=cfg.handoff.warm_window_s + 1,
    )
    assert plan.source["preview_only"] == 1
    sent = "\n".join(
        m.get("content") or "" for call in provider.calls for m in call["messages"]
    )
    assert "preview only" in sent


async def test_a_run_whose_generator_fails_resumes_without_a_handoff_rather_than_not_at_all(
    cfg, writer
):
    """A resume that cannot summarise a dead run should say so and carry on. Refusing to
    continue the run because an accelerator could not be built is a worse answer than
    continuing it with less."""
    session = await Session.create("test")
    rj = RunJournal(writer, "run-1")
    _started(rj, str(session.id))
    await _archive(session.id, "user_message", "user", "hello")

    writer.flush()
    plan = await rehydrate.restart(
        "run-1", store=writer.store, cfg=cfg, provider=_fake({}),
        now_s=cfg.handoff.warm_window_s + 1,
    )
    assert plan.handoff is None
    assert plan.handoff_source == rehydrate.NO_HANDOFF
    assert "generation_failed" in plan.source


async def test_a_resume_carries_the_interrupted_calls_out_to_the_model(cfg, writer):
    """The message list a successor is handed answers every tool call it contains, and the
    answer says the outcome is unknown. `observations` owns the words; what is asserted
    here is that this path actually consumes them - 5b did not, and said so."""
    session = await Session.create("test")
    rj = RunJournal(writer, "run-1")
    _started(rj, str(session.id))
    _said(rj, "assistant", "fetching", step_id="p1")
    rj.emit(
        "tool_requested",
        {"call_id": "c1", "name": "web_fetch", "args": {"url": "https://example.com/a"},
         "visible": True, "known": True},
        step_id="p1",
    )
    rj.emit("tool_started", {"call_id": "c1", "name": "web_fetch"}, step_id="p1")
    EffectLedger(writer).intend(
        run_id="run-1", step_id="p1", tool="web_fetch", effect_class="unsafe_write",
        args={"url": "https://example.com/a"},
    ).dispatched()
    await _archive(session.id, "user_message", "user", "fetch it")

    writer.flush()
    plan = await rehydrate.restart("run-1", store=writer.store, cfg=cfg, now_s=30.0)
    assert [m.tool for m in plan.closing] == ["web_fetch"]
    assert all(m.synthetic for m in plan.closing)
    assert "https://example.com/a" in plan.notice


# --- requirement B: the rerun guard ------------------------------------------


def _sender() -> Tool:
    async def handler(args, ctx):
        return ToolResult(content="sent")

    return Tool(
        name="sends_mail", description="sends one email", parameters=obj(to={}),
        handler=handler, effect_class="unsafe_write", risk="read",
    )


def _lister() -> Tool:
    async def handler(args, ctx):
        return ToolResult(content="listed")

    return Tool(
        name="upserts_a_goal", description="converges", parameters=obj(to={}),
        handler=handler, effect_class="idempotent_write", risk="read",
    )


def _executor(cfg, writer, *tools: Tool, grants: RerunGrants | None = None) -> ToolExecutor:
    return ToolExecutor(
        {t.name: t for t in tools},
        engine_from_config(cfg),
        AutoApprover(True),
        rerun_grants=grants,
        ledger=EffectLedger(writer),
    )


def _orphan(writer, *, tool: str, args: dict, run_id: str = "run-1", step_id: str = "p1"):
    """One call that was announced, never answered, and closed as uncertain on resume."""
    ledger = EffectLedger(writer)
    effect = ledger.intend(
        run_id=run_id, step_id=step_id, tool=tool, effect_class="unsafe_write", args=args
    )
    effect.dispatched()
    ledger.orphan(
        run_id=run_id, step_id=step_id, effect_id=effect.effect_id, key=effect.key,
        attempt=effect.attempt, note="orphaned on resume",
    )


async def test_an_unsafe_write_that_may_already_have_happened_is_not_run_again(cfg, writer):
    """The whole of requirement B in one assertion. Before this, the only thing standing
    between a resumed 27B and a second email was a sentence in its prompt."""
    _orphan(writer, tool="sends_mail", args={"to": "dylan@example.com"})
    executor = _executor(cfg, writer, _sender())
    ctx = ToolContext(run_id="run-1", step_id="p7", session_id=None)

    result = await executor.run("sends_mail", {"to": "dylan@example.com"}, ctx)
    assert not result.ok
    assert result.data["rerun_refused"] is True
    assert "may already have taken effect" in result.content


async def test_the_guard_matches_the_same_call_at_a_different_step(cfg, writer):
    """The reason the guard exists at all, asserted directly. `idempotency_key` hashes
    `step_id`, so a resumed turn re-issuing the identical call produces a key that matches
    nothing - keying this guard the same way would reproduce the hole it closes. The orphan
    above is at step `p1`; the call here is at `p7`."""
    _orphan(writer, tool="sends_mail", args={"to": "dylan@example.com"}, step_id="p1")
    executor = _executor(cfg, writer, _sender())

    same = await executor.run(
        "sends_mail", {"to": "dylan@example.com"},
        ToolContext(run_id="run-1", step_id="p7"),
    )
    assert not same.ok

    other = await executor.run(
        "sends_mail", {"to": "someone.else@example.com"},
        ToolContext(run_id="run-1", step_id="p7"),
    )
    assert other.ok, "a different call is ordinary work, not a duplicate"


async def test_the_user_saying_so_is_the_only_way_past_the_guard(cfg, writer):
    """And it is a person, not the model and not the runtime. `RerunGrants` is populated by
    `agent journal continue --allow-rerun`, which somebody types."""
    _orphan(writer, tool="sends_mail", args={"to": "dylan@example.com"})
    grants = RerunGrants()
    grants.allow_tool(run_id="run-1", tool="sends_mail")
    executor = _executor(cfg, writer, _sender(), grants=grants)

    result = await executor.run(
        "sends_mail", {"to": "dylan@example.com"},
        ToolContext(run_id="run-1", step_id="p7"),
    )
    assert result.ok
    assert result.content == "sent"


async def test_a_grant_is_scoped_to_the_run_the_doubt_belongs_to(cfg, writer):
    _orphan(writer, tool="sends_mail", args={"to": "dylan@example.com"})
    grants = RerunGrants()
    grants.allow_tool(run_id="some-other-run", tool="sends_mail")
    executor = _executor(cfg, writer, _sender(), grants=grants)

    result = await executor.run(
        "sends_mail", {"to": "dylan@example.com"},
        ToolContext(run_id="run-1", step_id="p7"),
    )
    assert not result.ok


async def test_a_later_run_doing_the_same_thing_is_ordinary_work(cfg, writer):
    """The doubt belongs to one run. Refusing everywhere would mean one crashed send
    poisoned that address for the life of the ledger."""
    _orphan(writer, tool="sends_mail", args={"to": "dylan@example.com"}, run_id="run-1")
    executor = _executor(cfg, writer, _sender())

    result = await executor.run(
        "sends_mail", {"to": "dylan@example.com"},
        ToolContext(run_id="run-2", step_id="p1"),
    )
    assert result.ok


async def test_an_idempotent_write_is_not_refused_because_re_running_it_converges(
    cfg, writer
):
    """The reconciliation table says re-execute, and it converges. A refusal here would be
    a prompt about a danger that is not there - which is how the signal stops meaning
    anything for the calls where it does."""
    ledger = EffectLedger(writer)
    effect = ledger.intend(
        run_id="run-1", step_id="p1", tool="upserts_a_goal", effect_class="idempotent_write",
        args={"to": "x"},
    )
    effect.dispatched()
    ledger.orphan(
        run_id="run-1", step_id="p1", effect_id=effect.effect_id, key=effect.key,
        attempt=effect.attempt, note="orphaned on resume",
    )
    executor = _executor(cfg, writer, _lister())

    result = await executor.run(
        "upserts_a_goal", {"to": "x"}, ToolContext(run_id="run-1", step_id="p7")
    )
    assert result.ok


async def test_a_call_that_resolved_normally_does_not_block_a_later_identical_one(
    cfg, writer
):
    """Only an *unresolved* uncertain call blocks. A committed one is a finished action, and
    doing it again is a decision the user and the policy already have machinery for."""
    ledger = EffectLedger(writer)
    effect = ledger.intend(
        run_id="run-1", step_id="p1", tool="sends_mail", effect_class="unsafe_write",
        args={"to": "dylan@example.com"},
    )
    effect.dispatched()
    effect.committed(result_ref="action:1")
    executor = _executor(cfg, writer, _sender())

    result = await executor.run(
        "sends_mail", {"to": "dylan@example.com"},
        ToolContext(run_id="run-1", step_id="p7"),
    )
    assert result.ok


async def test_a_refused_rerun_never_announces_an_effect_it_did_not_make(cfg, writer):
    """The refusal sits after approval and before the intent. An `effect_intended` for a
    call that was then refused is a promise on disk that nothing kept, and the next resume
    would reconcile it as a second orphan."""
    _orphan(writer, tool="sends_mail", args={"to": "dylan@example.com"})
    before = len(EffectLedger(writer).entries("run-1"))
    executor = _executor(cfg, writer, _sender())

    await executor.run(
        "sends_mail", {"to": "dylan@example.com"},
        ToolContext(run_id="run-1", step_id="p7"),
    )
    assert len(EffectLedger(writer).entries("run-1")) == before


# --- a handoff that survives the process -------------------------------------


async def test_a_resumed_session_starts_from_the_handoff_it_handed_off_under(cfg, tmp_path):
    """5b's deferral: `Session.handoff` was in-process, so a restart silently undid the
    compression and the conversation came back whole from the archive - with every number
    still looking healthy. The object is on a checkpoint of the turn that generated it,
    which is an *earlier run* than the one the next turn will open, so the lookup is by
    session rather than by run."""
    on = cfg.model_copy(
        update={
            "checkpoints": cfg.checkpoints.model_copy(update={"enabled": True}),
            "handoff": cfg.handoff.model_copy(
                update={"ceiling_tokens": 1000, "threshold_tokens": 999}
            ),
        }
    )
    provider = FakeProvider(turns=["The cap is 20."], json_results=[DRAFT])
    loop = AgentLoop(
        cfg=on, registry=Registry(), engine=engine_from_config(cfg),
        approver=AutoApprover(True), provider=provider,
    )
    session = await Session.create("test")
    async for _ in loop.run_turn(session, "where is the cap"):
        pass
    assert session.handoff is not None, "the fixture needs a handoff to have been stored"

    revived = await Session.resume(session.id, cfg=on)
    assert revived.handoff is not None
    assert revived.handoff.handoff_id == session.handoff.handoff_id


async def test_a_session_that_never_handed_off_comes_back_with_nothing_in_force(cfg):
    session = await Session.create("test")
    revived = await Session.resume(session.id, cfg=cfg)
    assert revived.handoff is None


async def test_a_dead_runs_prose_is_archived_but_never_replayed_into_a_later_prompt(cfg):
    """The `assistant_step` row. It exists so the cold-resume generator has bodies rather
    than previews, and `recent_messages` does not select it - so a turn that completes
    archives its prose twice and exactly one of those copies can ever reach a prompt. Two
    rows in an append-only archive is cheap; the same text twice in a prompt is the bug
    this runtime shipped for five passes."""

    async def handler(args, ctx):
        return ToolResult(content="TOP_K = 8")

    reader = Tool(
        name="reads_a_file", description="reads", parameters=obj(path={}),
        handler=handler, effect_class="read", risk="read",
    )
    registry = Registry()
    registry.add(reader)
    provider = FakeProvider(
        turns=[[("reads_a_file", {"path": "x"})], "The cap is 20."]
    )
    loop = AgentLoop(
        cfg=cfg, registry=registry, engine=engine_from_config(cfg),
        approver=AutoApprover(True), provider=provider,
    )
    session = await Session.create("test")
    async for _ in loop.run_turn(session, "where is the cap"):
        pass

    kinds = [
        r["kind"] for r in await repo_archive.events_for_session(session.id, limit=50)
    ]
    assert "assistant_step" in kinds

    replayed = await repo_archive.recent_messages(session.id)
    assert [r["kind"] for r in replayed] == ["user_message", "assistant_message"]
