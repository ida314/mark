"""Resume is the runtime keeping the promise a checkpoint made, and its failure modes are
both quiet ones.

A resume that repeats an `unsafe_write` sends the email twice and nothing in the journal
looks wrong afterwards. A resume that hands the model a 200-character preview and calls it
the conversation produces a turn that reads fluently and remembers a version of events that
never happened. Neither shows up as an error, so both are asserted here directly.

The properties that matter, and why each one is a test rather than a comment:

- **An interrupted `unsafe_write` is never re-run and never dropped.** It is closed as
  `uncertain`, with its arguments, and the ledger row moves to `orphaned` - not to `failed`,
  which would claim knowledge nobody has.
- **The evidence for how far the call got is preserved.** "Recorded and never started" and
  "started and never reported back" are different things to have to tell somebody, and
  collapsing them into one reassuring or one frightening sentence is a choice this layer
  does not get to make.
- **Orphans are found by folding the journal, not by reading the ledger.** The row is
  written after the event, so the one crash window that loses a row is exactly the window
  where an effect may have run.
- **A `read` can never become uncertain.** `memory_search` is the runtime's most-called tool
  and was ruled `read` at the Pass 3/4 boundary precisely so that resume does not prompt
  about it; a prompt the user cannot act on trains blind confirmation.
- **The journal's previews are never mistaken for the bodies.** The rehydration says how
  much is missing and where the rest lives.
"""

from __future__ import annotations

import pytest

from agentd.journal import resume as R
from agentd.journal.checkpoints import Checkpointer
from agentd.journal.ledger import EffectLedger
from agentd.journal.runtime import RunJournal
from agentd.journal.store import JournalStore
from agentd.journal.writer import JournalWriter


@pytest.fixture
def writer(tmp_path) -> JournalWriter:
    w = JournalWriter(JournalStore(tmp_path / "journal.db"))
    yield w
    w.close()


def _started(rj: RunJournal, turn_id: str = "t1", *, worker_id: str | None = None) -> None:
    rj.emit(
        "agent_started",
        {
            "session_id": "s1", "turn_id": turn_id, "role": "main", "actor": "main",
            "origin": "interactive", "channel": "cli", "autonomy": "act", "model": "m",
            "max_steps": 8, "input_chars": 2, "input_preview": "hi", "parent_turn_id": None,
        },
        worker_id=worker_id,
    )


def _finished(rj: RunJournal, turn_id: str = "t1", status: str = "completed") -> None:
    rj.emit(
        "agent_finished",
        {
            "turn_id": turn_id, "status": status, "steps": 1, "duration_ms": 5,
            "answer_chars": 4, "answer_preview": "done", "usage": {}, "usage_reported": False,
            "error": None,
        },
    )


def _message(rj: RunJournal, role: str = "user", text: str = "hi", step_id: str | None = None):
    from agentd.journal.events import preview

    rj.emit(
        "message_appended",
        {
            "role": role, "actor": role, "chars": len(text), "preview": preview(text),
            "trust": "trusted",
        },
        step_id=step_id,
    )


def _requested(
    rj: RunJournal, name: str, args: dict, *, step_id: str = "s1", call_id: str = "c1"
) -> None:
    rj.emit(
        "tool_requested",
        {"call_id": call_id, "name": name, "args": args, "visible": True, "known": True},
        step_id=step_id,
    )


def _interrupt(
    ledger: EffectLedger,
    *,
    run_id: str = "run-1",
    tool: str = "sends_mail",
    effect_class: str = "unsafe_write",
    args: dict | None = None,
    step_id: str = "s1",
    dispatched: bool = True,
):
    """An effecting call that was announced and never reported back. The crash, in one line."""
    effect = ledger.intend(
        run_id=run_id, step_id=step_id, tool=tool, effect_class=effect_class,
        args=args if args is not None else {"to": "dyd2008@nyu.edu"},
    )
    if dispatched:
        effect.dispatched()
    return effect


def _kill_after(writer: JournalWriter, run_id: str, seq: int) -> None:
    """Throw away everything the process had not committed by `seq`.

    A killed process loses a suffix and never a hole (`store.py`), so this is what the file
    looks like afterwards. The `effect` rows are left alone on purpose: they were committed
    in their own transactions and they survive, which is the whole reason the ledger can say
    anything about a call the journal only ever announced.
    """
    writer.flush()
    writer.store.query("DELETE FROM journal WHERE run_id = ? AND seq > ?", (run_id, seq))


def _disk(writer: JournalWriter) -> JournalStore:
    """What a second process would find in the file.

    `plan()` reads the journal off disk, because the process it is asking about is dead and
    its buffer went with it. A test that has just written events through this process has to
    put them there first - which is also why `resume()` flushes before it plans.
    """
    writer.flush()
    return writer.store


def _events(writer: JournalWriter, run_id: str, *types: str) -> list:
    writer.flush()
    return [e for e in writer.store.read(run_id) if not types or e.type in types]


# --- reconciliation, by effect class -----------------------------------------


def test_an_interrupted_unsafe_write_is_never_retried_and_never_forgotten(writer) -> None:
    """The pass's one hard rule. The call may have sent the mail; nobody can prove it either
    way, so it is reported with its arguments and left for a human, not quietly repeated."""
    rj = RunJournal(writer, "run-1")
    _started(rj)
    _requested(rj, "sends_mail", {"to": "dyd2008@nyu.edu", "subject": "the lease"})
    effect = _interrupt(EffectLedger(writer), args={"to": "dyd2008@nyu.edu", "subject": "the lease"})
    _kill_after(writer, "run-1", writer.store.last_seq("run-1"))

    plan = R.plan("run-1", store=writer.store)
    (orphan,) = plan.reconciliation.orphans
    assert orphan.disposition == R.UNCERTAIN
    assert orphan.evidence == R.MAY_HAVE_RUN
    assert orphan.tool == "sends_mail"
    assert orphan.arguments == {"to": "dyd2008@nyu.edu", "subject": "the lease"}
    assert plan.reconciliation.retryable == ()

    done = R.resume("run-1", writer=writer, reason="process restart")
    assert done.applied and done.orphaned_keys == (effect.key,)
    assert EffectLedger(writer).get(effect.key).state == "orphaned"
    (closure,) = _events(writer, "run-1", "effect_committed")
    assert closure.payload["status"] == "uncertain"
    assert closure.payload["effect_id"] == orphan.effect_id


def test_an_interrupted_idempotent_write_may_simply_be_re_executed(writer) -> None:
    """Two classes, two answers, from the same crash: `idempotent_write` converges on
    replay, so it needs no prompt and no ceremony - only a record that it was interrupted."""
    rj = RunJournal(writer, "run-1")
    _started(rj)
    _interrupt(EffectLedger(writer), tool="goal_upsert", effect_class="idempotent_write")
    _kill_after(writer, "run-1", writer.store.last_seq("run-1"))

    plan = R.plan("run-1", store=writer.store)
    (orphan,) = plan.reconciliation.retryable
    assert orphan.disposition == R.RETRY
    assert plan.reconciliation.uncertain == ()
    # It is still closed rather than left dangling: the first attempt's outcome is unknown
    # for ever, whatever the second attempt does.
    R.resume("run-1", writer=writer, reason="process restart")
    assert EffectLedger(writer).get(orphan.idempotency_key).state == "orphaned"


def test_re_executing_a_retryable_call_lands_on_the_row_it_already_had(writer) -> None:
    """"May be re-executed" has to be reachable, not only recorded. The idempotency key is
    `hash(run_id, step_id, tool_name, canonical_args)`, so the second attempt finds the same
    row, increments `attempt`, and leaves the first attempt's `uncertain` closure standing in
    the journal - which is correct, because nothing ever did learn how the first one ended."""
    rj = RunJournal(writer, "run-1")
    _started(rj)
    first = _interrupt(EffectLedger(writer), tool="goal_upsert", effect_class="idempotent_write")
    _kill_after(writer, "run-1", writer.store.last_seq("run-1"))
    R.resume("run-1", writer=writer, reason="process restart")

    again = _interrupt(EffectLedger(writer), tool="goal_upsert", effect_class="idempotent_write")
    again.committed(result_ref="action:2", result="ok")

    assert again.key == first.key
    row = EffectLedger(writer).get(first.key)
    assert row.attempt == 2 and row.state == "committed"
    statuses = [e.payload["status"] for e in _events(writer, "run-1", "effect_committed")]
    assert statuses == ["uncertain", "committed"]


def test_a_read_can_never_be_surfaced_as_uncertain(writer) -> None:
    """`memory_search` is the runtime's most-called tool and was ruled `read` at the Pass 3/4
    boundary for this exact reason: a resume that asked the user to confirm memory lookups
    would teach them to confirm without looking, which is what destroys the value of asking
    about the calls that matter."""
    rj = RunJournal(writer, "run-1")
    _started(rj)
    _requested(rj, "memory_search", {"query": "what do I know"})
    # A read gets no ledger row and no effect events at all, so there is nothing for a crash
    # to leave behind. Asserted rather than assumed, because the whole guarantee rests on it.
    assert not R.plan("run-1", store=_disk(writer)).reconciliation.orphans
    assert _events(writer, "run-1", "effect_intended") == []
    assert R.DISPOSITIONS["read"] == R.RETRY


def test_an_effect_class_outside_the_vocabulary_is_refused_rather_than_bucketed(writer) -> None:
    """A bucket is half a grouping key. An unknown class defaulting to "retry" re-runs an
    unsafe call and defaulting to "uncertain" prompts about a free one; both are decided by
    whichever branch was written first, which is not a decision."""
    with pytest.raises(R.ResumeError) as exc:
        R.disposition("mostly_harmless")
    assert "no reconciliation rule" in str(exc.value)


# --- what the evidence says --------------------------------------------------


def test_a_call_that_was_never_started_says_so_and_is_still_not_retried(writer) -> None:
    """`dispatched()` commits before the handler is awaited, so a row still at `intended` is
    evidence the call never ran. It does not change the disposition - an unsafe write is
    never auto-retried - but it is the difference between "I may have sent it" and "I never
    got as far as trying", and only one of those is worth waking somebody for."""
    rj = RunJournal(writer, "run-1")
    _started(rj)
    effect = _interrupt(EffectLedger(writer), dispatched=False)
    _kill_after(writer, "run-1", writer.store.last_seq("run-1"))

    (orphan,) = R.plan("run-1", store=writer.store).reconciliation.orphans
    assert orphan.evidence == R.NEVER_DISPATCHED
    assert orphan.disposition == R.UNCERTAIN

    R.resume("run-1", writer=writer, reason="process restart")
    (closure,) = _events(writer, "run-1", "effect_committed")
    assert closure.payload["error"] == R.NOTES[R.NEVER_DISPATCHED]
    assert EffectLedger(writer).get(effect.key).state == "orphaned"


def test_an_effect_whose_ledger_row_never_landed_is_unknown_rather_than_safe(writer) -> None:
    """The ledger is written after the journal, so a crash in between leaves an announced
    call with no row. Reading the `effect` table for open rows would miss it entirely - and
    it is the case most likely to have run."""
    rj = RunJournal(writer, "run-1")
    _started(rj)
    effect = _interrupt(EffectLedger(writer))
    writer.store.query("DELETE FROM effect WHERE idempotency_key = ?", (effect.key,))
    _kill_after(writer, "run-1", writer.store.last_seq("run-1"))

    (orphan,) = R.plan("run-1", store=writer.store).reconciliation.orphans
    assert orphan.ledger_state is None
    assert orphan.evidence == R.UNKNOWN
    assert orphan.disposition == R.UNCERTAIN

    done = R.resume("run-1", writer=writer, reason="process restart")
    # Reported as a row that was not there, rather than counted as one that was moved.
    assert done.orphaned_keys == () and done.rowless_effects == (effect.key,)
    (closure,) = _events(writer, "run-1", "effect_committed")
    assert closure.payload["error"] == R.NOTES[R.UNKNOWN]


def test_an_orphan_is_closed_without_inventing_a_duration(writer) -> None:
    """An interrupted call was never timed. `duration_ms: 0` would record it as a call that
    returned instantly - a measurement that never happened, which is this codebase's
    characteristic bug in the field whose whole job is to hold an unknown."""
    rj = RunJournal(writer, "run-1")
    _started(rj)
    _interrupt(EffectLedger(writer))
    _kill_after(writer, "run-1", writer.store.last_seq("run-1"))
    R.resume("run-1", writer=writer, reason="process restart")

    (closure,) = _events(writer, "run-1", "effect_committed")
    assert closure.payload["duration_ms"] is None
    assert closure.payload["result_digest"] is None


# --- naming the call the user has to decide about ----------------------------


def test_an_interrupted_fetch_can_still_say_which_url_it_was(writer) -> None:
    """Binding requirement from the Pass 3/4 boundary. The ledger holds an args *hash* and
    its `result_ref` is NULL until a terminal state - which an orphan never reached - so the
    arguments come from `tool_requested` or they do not exist. "Confirm this fetch?" with no
    URL is the prompt that trains blind confirmation."""
    rj = RunJournal(writer, "run-1")
    _started(rj)
    url = "https://example.com/unsubscribe?token=9f2a"
    _requested(rj, "web_fetch", {"url": url})
    _interrupt(EffectLedger(writer), tool="web_fetch", args={"url": url})
    _kill_after(writer, "run-1", writer.store.last_seq("run-1"))

    (orphan,) = R.plan("run-1", store=writer.store).reconciliation.uncertain
    assert orphan.arguments == {"url": url}
    assert url in orphan.subject


def test_several_interrupted_fetches_are_one_group_and_not_one_prompt_each(writer) -> None:
    """The other binding requirement, and the same reasoning: four prompts are four chances
    to say yes without reading. One group, every URL in it, in the order they were made."""
    rj = RunJournal(writer, "run-1")
    _started(rj)
    ledger = EffectLedger(writer)
    urls = [f"https://example.com/{n}" for n in ("one", "two", "three")]
    for i, url in enumerate(urls, start=1):
        _requested(rj, "web_fetch", {"url": url}, step_id=f"s{i}", call_id=f"c{i}")
        _interrupt(ledger, tool="web_fetch", args={"url": url}, step_id=f"s{i}")
    _requested(rj, "sends_mail", {"to": "dyd2008@nyu.edu"}, step_id="s4", call_id="c4")
    _interrupt(ledger, tool="sends_mail", step_id="s4")
    _kill_after(writer, "run-1", writer.store.last_seq("run-1"))

    groups = R.plan("run-1", store=writer.store).reconciliation.groups
    assert [g.tool for g in groups] == ["web_fetch", "sends_mail"]
    fetches = groups[0]
    assert len(fetches.orphans) == 3
    assert [o.arguments["url"] for o in fetches.orphans] == urls
    assert all(url in subject for url, subject in zip(urls, fetches.subjects, strict=True))


def test_two_calls_of_one_tool_in_one_step_do_not_borrow_each_others_arguments(writer) -> None:
    """A model asking for two fetches in one step is one assistant message and two calls, run
    one after the other. The nearest request before the intent is the one that caused it;
    taking the first would report the URL of the fetch that finished perfectly well, which is
    a confident answer to the wrong question.

    `call_id` is not the key it looks like: it comes from the model, and the live turn in
    session 4b's notes had two calls in two steps both announcing themselves as `fake_0`.
    """
    rj = RunJournal(writer, "run-1")
    _started(rj)
    ledger = EffectLedger(writer)
    _requested(rj, "web_fetch", {"url": "https://example.com/first"}, call_id="c1")
    done = _interrupt(ledger, tool="web_fetch", args={"url": "https://example.com/first"})
    done.committed(result_ref="action:1", result="ok")
    _requested(rj, "web_fetch", {"url": "https://example.com/second"}, call_id="c2")
    _interrupt(ledger, tool="web_fetch", args={"url": "https://example.com/second"})
    _kill_after(writer, "run-1", writer.store.last_seq("run-1"))

    (orphan,) = R.plan("run-1", store=writer.store).reconciliation.orphans
    assert orphan.arguments == {"url": "https://example.com/second"}
    assert "second" in orphan.subject


def test_an_orphan_takes_no_arguments_from_a_neighbouring_call_of_the_same_tool(writer) -> None:
    """The step is half the join, and it is the half that stops the report being confidently
    wrong. An effect announced in a step that holds no `tool_requested` - an approved call
    replayed into a live run, anything that intends an effect without going through the loop -
    must show nothing rather than the URL of the call in the step before, which is a fetch
    that finished perfectly well and is the wrong thing to ask somebody about."""
    rj = RunJournal(writer, "run-1")
    _started(rj)
    _requested(rj, "web_fetch", {"url": "https://example.com/first"}, step_id="s1", call_id="c1")
    ledger = EffectLedger(writer)
    done = _interrupt(ledger, tool="web_fetch", args={"url": "https://example.com/first"})
    done.committed(result_ref="action:1", result="ok")
    _interrupt(ledger, tool="web_fetch", args={"url": "https://example.com/second"}, step_id="s2")
    _kill_after(writer, "run-1", writer.store.last_seq("run-1"))

    (orphan,) = R.plan("run-1", store=writer.store).reconciliation.orphans
    assert orphan.step_id == "s2"
    assert orphan.arguments is None and orphan.subject is None


def test_a_call_that_never_went_through_the_loop_has_absent_arguments_not_empty_ones(
    writer,
) -> None:
    """A queued approval replayed after its turn, or an MCP caller: neither emits
    `tool_requested`, so the arguments genuinely are not recorded anywhere. `{}` would read
    as "called with no arguments", which is a different and answerable statement."""
    rj = RunJournal(writer, "run-1")
    _started(rj)
    _interrupt(EffectLedger(writer))
    _kill_after(writer, "run-1", writer.store.last_seq("run-1"))

    (orphan,) = R.plan("run-1", store=writer.store).reconciliation.orphans
    assert orphan.arguments is None
    assert orphan.arguments_seq is None
    assert orphan.subject is None


# --- rehydration -------------------------------------------------------------


def test_the_message_list_comes_back_in_order_with_its_tool_calls_attached(writer) -> None:
    """The mechanical path: no handoff, no summary, no model call - just the journal folded
    back into the shape the turn had. The tool call arguments are exact, because
    `tool_requested.args` is the parsed call rather than a preview of it."""
    rj = RunJournal(writer, "run-1")
    _started(rj)
    _message(rj, "user", "read me the lease")
    _message(rj, "system", "you are an agent")
    _message(rj, "assistant", "", step_id="s1")
    _requested(rj, "fs_read", {"path": "/tmp/lease.txt"})
    rj.emit(
        "tool_finished",
        {
            "call_id": "c1", "name": "fs_read", "duration_ms": 3, "result_chars": 12,
            "trust": "trusted", "summary": "the lease...",
        },
        step_id="s1",
    )
    _message(rj, "assistant", "It runs to June.", step_id="s2")

    rehydrated = R.plan("run-1", store=_disk(writer)).rehydration
    assert [m.role for m in rehydrated.messages] == [
        "user", "system", "assistant", "tool", "assistant",
    ]
    assert [c.name for c in rehydrated.messages[2].calls] == ["fs_read"]
    assert rehydrated.messages[2].calls[0].arguments == {"path": "/tmp/lease.txt"}
    assert rehydrated.messages[3].tool_call_id == "c1"
    assert rehydrated.replayed_events == 7
    assert rehydrated.turn_id == "t1" and rehydrated.archive.session_id == "s1"


def test_a_workers_transcript_does_not_come_back_in_the_orchestrators_message_list(
    writer,
) -> None:
    """A worker's events live in its caller's run, tagged with `worker_id`. "Worker
    transcripts never enter orchestrator context outside debug mode" is a rule about what a
    resumed turn may be told, and a rehydration that quietly re-injected them would break it
    in the one place nobody would think to look."""
    rj = RunJournal(writer, "run-1")
    _started(rj)
    _message(rj, "user", "find the thing")
    worker = rj.for_worker("w-1")
    _started(worker, turn_id="worker-turn", worker_id="w-1")
    _message(worker, "assistant", "the worker's own reasoning", step_id="w-1.s1")

    rehydrated = R.plan("run-1", store=_disk(writer)).rehydration
    assert [m.preview for m in rehydrated.messages] == ["find the thing"]
    assert rehydrated.worker_events == 2
    assert rehydrated.turn_id == "t1"


def test_the_journal_says_how_much_of_a_message_it_is_not_holding(writer) -> None:
    """The journal keeps 200 characters with the whitespace collapsed; the bodies are in the
    archive. A rehydration that handed those previews to a model as the conversation would
    produce a turn that reads perfectly and remembers something nobody said."""
    rj = RunJournal(writer, "run-1")
    _started(rj)
    long_text = "the lease says " + "x" * 500
    _message(rj, "user", long_text)

    rehydrated = R.plan("run-1", store=_disk(writer)).rehydration
    (message,) = rehydrated.messages
    assert message.chars == len(long_text)
    assert len(message.preview) < message.chars
    assert message.truncated and rehydrated.truncated == (message,)
    # And where the rest of it is, so the caller that continues the run has somewhere to go.
    assert rehydrated.archive == R.ArchiveRef(session_id="s1", turn_id="t1")


# --- the five boundaries -----------------------------------------------------


@pytest.mark.parametrize(
    "trigger,expected_state",
    [
        ("turn_end", R.COMPLETE),
        ("worker_finished", R.INTERRUPTED),
        ("pre_effect", R.INTERRUPTED),
        ("handoff", R.INTERRUPTED),
        ("manual", R.INTERRUPTED),
    ],
)
def test_a_process_killed_at_a_boundary_resumes_from_that_boundary(
    writer, trigger: str, expected_state: str
) -> None:
    """The pass's exit criterion for this session. Each of the five triggers is a moment the
    runtime claimed it could pick the run up from, so each one is killed at and picked up.

    `turn_end` is the odd one and the reason the parametrisation carries an expectation
    rather than one assertion for all five: that run is *finished*. Resuming it correctly
    means doing nothing, and writing `run_resumed` after `agent_finished` would leave a
    record of a resume that had nothing to resume.
    """
    rj = RunJournal(writer, "run-1")
    _started(rj)
    _message(rj, "user", "do the thing")
    if trigger == "worker_finished":
        rj.emit(
            "worker_created",
            {
                "worker_id": "w-1", "name": "researcher", "role": "subagent",
                "autonomy": "act", "max_steps": 5, "tools": [], "task_chars": 4,
                "task_preview": "find it", "parent_step_id": "s1",
            },
            worker_id="w-1",
        )
        rj.emit(
            "worker_finished",
            {
                "worker_id": "w-1", "name": "researcher", "status": "ok", "summary_chars": 1,
                "tainted": False, "artifacts": 0, "citations": 0, "candidates": 0,
                "tokens": 0, "duration_ms": 1,
            },
            worker_id="w-1",
        )
    if trigger == "pre_effect":
        _requested(rj, "sends_mail", {"to": "dyd2008@nyu.edu"})
    if trigger == "turn_end":
        _finished(rj)
    checkpoint = Checkpointer(writer).write("run-1", trigger=trigger)
    _kill_after(writer, "run-1", checkpoint.event_seq)

    plan = R.plan("run-1", store=writer.store)
    assert plan.state == expected_state
    assert plan.checkpoint.checkpoint_id == checkpoint.checkpoint_id
    assert plan.from_seq == checkpoint.event_seq
    # Whatever the boundary, the fold covers the whole surviving prefix: the checkpoint says
    # where the run got to, and the journal is still what the state is rebuilt from.
    assert plan.rehydration.replayed_events == checkpoint.event_seq
    assert plan.rehydration.messages[0].preview == "do the thing"

    done = R.resume("run-1", writer=writer, reason="process restart")
    assert done.applied is (expected_state != R.COMPLETE)
    announced = _events(writer, "run-1", "run_resumed")
    if not done.applied:
        assert announced == []
        return
    (event,) = announced
    assert event.payload["checkpoint_id"] == checkpoint.checkpoint_id
    assert event.payload["from_seq"] == checkpoint.event_seq
    assert event.seq == checkpoint.event_seq + 1  # the next event after the one that survived
    assert event.payload["uncertain_effects"] == []


def test_a_kill_at_the_pre_effect_boundary_leaves_nothing_uncertain(writer) -> None:
    """Why the snapshot is taken *before* the intent is even announced: a process that dies
    between the two has not run the tool, and the resume can say so without hedging."""
    rj = RunJournal(writer, "run-1")
    _started(rj)
    _requested(rj, "sends_mail", {"to": "dyd2008@nyu.edu"})
    checkpoint = Checkpointer(writer).write("run-1", trigger="pre_effect")
    _interrupt(EffectLedger(writer))  # the intent this run would have gone on to record
    _kill_after(writer, "run-1", checkpoint.event_seq)

    plan = R.plan("run-1", store=writer.store)
    assert plan.reconciliation.orphans == ()
    assert plan.state == R.INTERRUPTED


# --- announcing it -----------------------------------------------------------


def test_a_run_from_before_checkpoints_existed_resumes_by_folding_the_journal(writer) -> None:
    """`[checkpoints] enabled` is off in the shipped config, so every run in the live journal
    today has no snapshot behind it. A resume path that needed one would be a resume path
    that works only on runs recorded after somebody switched a flag."""
    rj = RunJournal(writer, "run-1")
    _started(rj)
    _message(rj, "user", "do the thing")
    _interrupt(EffectLedger(writer))
    _kill_after(writer, "run-1", writer.store.last_seq("run-1"))

    plan = R.plan("run-1", store=writer.store)
    assert plan.checkpoint is None
    done = R.resume("run-1", writer=writer, reason="process restart")
    (event,) = _events(writer, "run-1", "run_resumed")
    # Null, and required to be present: "folded from the start because there was no
    # checkpoint" is a statement about this resume, not a key somebody forgot.
    assert event.payload["checkpoint_id"] is None
    assert event.payload["replayed_events"] == done.plan.rehydration.replayed_events
    assert len(event.payload["uncertain_effects"]) == 1


def test_resuming_twice_does_not_orphan_the_same_call_twice(writer) -> None:
    """A resume is itself something a crash can interrupt. The second pass must see the
    effect as closed - `uncertain` is terminal - or every restart adds another line saying
    the same email might have gone out."""
    rj = RunJournal(writer, "run-1")
    _started(rj)
    effect = _interrupt(EffectLedger(writer))
    _kill_after(writer, "run-1", writer.store.last_seq("run-1"))

    R.resume("run-1", writer=writer, reason="process restart")
    second = R.resume("run-1", writer=writer, reason="process restart again")

    assert second.plan.reconciliation.orphans == ()
    assert len(_events(writer, "run-1", "effect_committed")) == 1
    assert len(_events(writer, "run-1", "run_resumed")) == 2  # the run itself is still open
    assert EffectLedger(writer).get(effect.key).state == "orphaned"


def test_a_finished_run_with_nothing_open_is_left_alone(writer) -> None:
    """Resuming correctly sometimes means writing nothing at all."""
    rj = RunJournal(writer, "run-1")
    _started(rj)
    _message(rj, "user", "do the thing")
    _finished(rj)

    done = R.resume("run-1", writer=writer, reason="process restart")
    assert done.applied is False and done.event_seq is None
    assert _events(writer, "run-1", "run_resumed") == []
    assert done.plan.state == R.COMPLETE


def test_a_detached_run_is_reconciled_even_though_there_is_no_turn_to_continue(writer) -> None:
    """Session 3b keys a queued approval replayed after its turn under
    `detached:<action_id>`: two effect events and no `agent_started`. A checkpoint refuses
    such a run, because there is no orchestrator to name - but it is exactly the shape that
    can hold an unsafe write nobody ever heard back about, so reconciliation must not."""
    _interrupt(EffectLedger(writer), run_id="detached:abc")

    plan = R.plan("detached:abc", store=writer.store)
    assert plan.state == R.NO_ORCHESTRATOR
    assert plan.rehydration.archive is None and plan.rehydration.turn_id is None
    assert len(plan.reconciliation.uncertain) == 1
    assert R.resume("detached:abc", writer=writer, reason="startup sweep").applied


def test_a_run_nobody_has_ever_written_is_an_error_rather_than_an_empty_plan(writer) -> None:
    """An empty plan for a run id that does not exist is a typo returning "nothing to do"."""
    with pytest.raises(R.NoSuchRun):
        R.plan("never-happened", store=writer.store)


def test_planning_a_resume_writes_nothing(writer) -> None:
    """`plan()` is what a dry run and the CLI show before anybody has agreed to anything. A
    reconciliation that moved a ledger row while merely being *looked* at would make the
    report and the thing it reports on two different runs."""
    rj = RunJournal(writer, "run-1")
    _started(rj)
    effect = _interrupt(EffectLedger(writer))
    _kill_after(writer, "run-1", writer.store.last_seq("run-1"))
    before = writer.store.count("run-1")

    R.plan("run-1", store=writer.store)
    R.plan("run-1", store=writer.store)

    assert writer.store.count("run-1") == before
    assert EffectLedger(writer).get(effect.key).state == "started"
