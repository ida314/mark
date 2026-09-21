"""A fork rewinds what was said. It must not pretend to rewind what was done.

Forking is the one recovery path where the honest answer and the comfortable answer come
apart. The comfortable one is to put the conversation back and say nothing: the transcript
reads as though the last three steps never happened, and the three files they wrote are
still on disk, the notification is still delivered, and the next turn reasons from a history
that disagrees with the world. Automatic reversal is this pass's *Must not* precisely because
it cannot be done in general - you cannot unsend - so the disclosure is the feature, and
these are its failure modes:

- **A fork that touches the parent.** `state = fold(reduce, journal, initial)` stops being
  true about a run the moment somebody else can write into it. Asserted event by event.
- **A disclosure that undercounts.** An effect committed after the fork point and left out of
  the disclosure is work the user is never told about, in the one message whose whole job is
  to tell them. The counts here are checked against the journal, not against the prose.
- **A disclosure that overclaims.** An effect that may have happened, reported as something
  that did, is the duplicate-prevention vocabulary thrown away at the last step.
- **A plausible summary.** "3 files in src/auth/" is only worth printing if the three paths
  are the paths the calls recorded and `src/auth/` is where they really are. The line is
  derived from the arguments or it is not printed.
- **The identifying argument falling off a truncated line**, which is session 4c's bug in a
  new renderer.
"""

from __future__ import annotations

import pytest

from agentd.agent import observations as obs
from agentd.journal import fork as F
from agentd.journal import resume as R
from agentd.journal.checkpoints import Checkpointer
from agentd.journal.ledger import EffectLedger
from agentd.journal.render import render_event
from agentd.journal.runtime import RunJournal
from agentd.journal.store import JournalStore
from agentd.journal.writer import JournalWriter

PARENT = "run-1"


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


def _effect(
    writer: JournalWriter,
    *,
    tool: str,
    args: dict,
    effect_class: str = "unsafe_write",
    step_id: str = "s1",
    settle: str | None = "committed",
    run_id: str = PARENT,
):
    """One effecting call, announced and then settled however the caller says.

    `settle=None` leaves it open, which is what a crash leaves behind.
    """
    effect = EffectLedger(writer).intend(
        run_id=run_id, step_id=step_id, tool=tool, effect_class=effect_class, args=args
    )
    effect.dispatched()
    if settle == "committed":
        effect.committed(result_ref=f"action:{effect.effect_id[:6]}", result="ok")
    elif settle == "failed":
        effect.failed("no", result_ref=f"action:{effect.effect_id[:6]}")
    return effect


def _disk(writer: JournalWriter) -> JournalStore:
    writer.flush()
    return writer.store


def _plan(writer: JournalWriter, at_seq: int, run_id: str = PARENT) -> F.ForkPlan:
    return F.plan_fork(run_id, at_seq=at_seq, store=_disk(writer))


def _run(writer: JournalWriter, run_id: str) -> list:
    writer.flush()
    return writer.store.read(run_id)


# --- the parent is not touched -----------------------------------------------


def test_forking_a_run_leaves_the_run_it_forked_from_exactly_as_it_was(writer) -> None:
    """The governing invariant is only true while a run's history belongs to that run. A
    fork that appended so much as a marker to the parent would make the parent's own fold
    depend on something that happened to somebody else."""
    rj = RunJournal(writer, PARENT)
    _started(rj)
    _message(rj, "user", "write the notes")
    _requested(rj, "fs_write", {"path": "/home/dylan/notes/a.md", "content": "x"})
    effect = _effect(writer, tool="fs_write", args={"path": "/home/dylan/notes/a.md"})
    before = [(e.seq, e.type, e.payload) for e in _run(writer, PARENT)]
    ledger_before = EffectLedger(writer).get(effect.key).state

    done = F.fork(PARENT, at_seq=2, writer=writer, reason="rewind")

    assert [(e.seq, e.type, e.payload) for e in _run(writer, PARENT)] == before
    assert EffectLedger(writer).get(effect.key).state == ledger_before
    assert done.run_id != PARENT
    # And no checkpoint was taken of either run: a fork is not a boundary.
    assert Checkpointer(writer).entries() == []


def test_a_forked_run_says_where_it_came_from_in_its_first_event(writer) -> None:
    """The lineage lives in the child, because the child is the only run that needs it. A
    run whose first event is `run_forked` can always be traced back; the parent cannot be
    made to carry that without its history growing every time somebody rewinds."""
    rj = RunJournal(writer, PARENT)
    _started(rj)
    _message(rj, "user", "hello")
    done = F.fork(PARENT, at_seq=2, writer=writer, reason="user rewound")

    (event,) = _run(writer, done.run_id)
    assert event.seq == 1 and event.type == "run_forked"
    assert event.payload["parent_run_id"] == PARENT
    assert event.payload["forked_from_seq"] == 2
    assert event.payload["reason"] == "user rewound"
    # Null rather than absent, in both slots: "no checkpoint stood behind this fork" and
    # "this did not come out of a handoff" are statements Pass 5 will need to distinguish
    # from a caller that forgot the key.
    assert event.payload["checkpoint_id"] is None
    assert event.payload["handoff_id"] is None
    assert done.forked_from_seq == 2 and done.parent_run_id == PARENT
    assert render_event(event).text == f"· forked from run {PARENT} at seq 2"


def test_a_fork_records_the_checkpoint_it_could_have_started_from_and_not_a_later_one(
    writer,
) -> None:
    """A checkpoint that covers events the fork is leaving behind knows more than the fork
    point does. `covers_seq` is the field that says so, and it is the one filtered on."""
    rj = RunJournal(writer, PARENT)
    _started(rj)
    _message(rj, "user", "one")
    early = Checkpointer(writer).write(PARENT, trigger="manual")
    _message(rj, "assistant", "two")
    late = Checkpointer(writer).write(PARENT, trigger="manual")

    assert late.covers_seq > early.event_seq > early.covers_seq
    # At the later snapshot's own position, the later snapshot is the right one - and it is
    # only reachable by filtering on `covers_seq`, because its announcement sits after it.
    assert _plan(writer, late.covers_seq).checkpoint.checkpoint_id == late.checkpoint_id
    # One event earlier, the later snapshot already knows more than the fork point does.
    assert _plan(writer, late.covers_seq - 1).checkpoint.checkpoint_id == early.checkpoint_id


def test_a_forked_run_does_not_inherit_the_parents_effect_ledger(writer) -> None:
    """"No cross-run result caching" is the *Must not*, and the idempotency key is what
    would have implemented it by accident. A call the child repeats has a different key, so
    it will really happen again - which is the honest behaviour, and the reason the
    disclosure has to be accurate before anybody decides to repeat one."""
    rj = RunJournal(writer, PARENT)
    _started(rj)
    _message(rj, "user", "send it")
    parent_effect = _effect(writer, tool="fs_write", args={"path": "/tmp/a", "content": "x"})
    done = F.fork(PARENT, at_seq=2, writer=writer, reason="rewind")

    child_effect = EffectLedger(writer).intend(
        run_id=done.run_id, step_id="s1", tool="fs_write",
        effect_class="unsafe_write", args={"path": "/tmp/a", "content": "x"},
    )
    assert child_effect.key != parent_effect.key
    assert EffectLedger(writer).get(parent_effect.key).run_id == PARENT


# --- what the disclosure says ------------------------------------------------


def test_the_disclosure_names_every_committed_call_made_after_the_fork_point(writer) -> None:
    """The count is a count of effects the journal recorded, and every call is named. A
    disclosure that says "3 files" without saying which three is a sentence the reader
    cannot act on, which is the failure Dylan ruled against at the Pass 3/4 boundary."""
    rj = RunJournal(writer, PARENT)
    _started(rj)
    _message(rj, "user", "write the notes")
    fork_at = _disk(writer).last_seq(PARENT)
    for i, path in enumerate(("/home/dylan/notes/a.md", "/home/dylan/notes/b.md")):
        _requested(rj, "fs_write", {"path": path, "content": "x"}, step_id=f"s{i}", call_id=f"c{i}")
        _effect(writer, tool="fs_write", args={"path": path, "content": "x"}, step_id=f"s{i}")

    plan = _plan(writer, fork_at)
    assert len(plan.disclosure.committed) == 2
    assert [c.location for c in plan.disclosure.committed] == [
        "/home/dylan/notes/a.md", "/home/dylan/notes/b.md",
    ]
    text = obs.disclosure(plan)
    assert "Since that point I:" in text
    assert "2 fs_write calls, all under /home/dylan/notes/" in text
    assert "/home/dylan/notes/a.md" in text and "/home/dylan/notes/b.md" in text
    assert "I have not undone any of the above" in text


def test_the_shared_location_is_only_claimed_when_the_calls_really_share_one(writer) -> None:
    """"all under X" is a claim about the filesystem. Two files in two different trees share
    only `/`, and a disclosure that rounded that up to a directory would be inventing the one
    detail the reader would check."""
    rj = RunJournal(writer, PARENT)
    _started(rj)
    fork_at = _disk(writer).last_seq(PARENT)
    for i, path in enumerate(("/home/dylan/notes/a.md", "/etc/hosts")):
        _requested(rj, "fs_write", {"path": path}, step_id=f"s{i}", call_id=f"c{i}")
        _effect(writer, tool="fs_write", args={"path": path}, step_id=f"s{i}")

    plan = _plan(writer, fork_at)
    (group,) = plan.disclosure.groups
    assert group.location is None
    text = obs.disclosure(plan)
    assert "2 fs_write calls\n" in text
    assert "all under" not in text
    assert "/etc/hosts" in text


def test_work_from_before_the_fork_point_is_not_disclosed_as_something_it_did(writer) -> None:
    """The disclosure answers "what happened after the point you are rewinding to". Work
    from before it is not being rewound and is not the user's problem here; including it
    would make the honest block longer and the reader less likely to read it."""
    rj = RunJournal(writer, PARENT)
    _started(rj)
    _requested(rj, "fs_write", {"path": "/tmp/early"})
    _effect(writer, tool="fs_write", args={"path": "/tmp/early"})
    fork_at = _disk(writer).last_seq(PARENT)
    _requested(rj, "fs_write", {"path": "/tmp/late"}, step_id="s2", call_id="c2")
    _effect(writer, tool="fs_write", args={"path": "/tmp/late"}, step_id="s2")

    plan = _plan(writer, fork_at)
    (call,) = plan.disclosure.committed
    assert call.location == "/tmp/late"
    assert "/tmp/early" not in obs.disclosure(plan)


def test_a_call_in_flight_at_the_fork_point_that_landed_afterwards_is_disclosed(writer) -> None:
    """The call the fork is most likely to be about: announced before the point, settled
    after it. Filtering on the intent alone would leave it out of the disclosure entirely
    while it sat there, committed, in the journal."""
    rj = RunJournal(writer, PARENT)
    _started(rj)
    _requested(rj, "fs_write", {"path": "/tmp/inflight"})
    effect = EffectLedger(writer).intend(
        run_id=PARENT, step_id="s1", tool="fs_write", effect_class="unsafe_write",
        args={"path": "/tmp/inflight"},
    )
    effect.dispatched()
    fork_at = _disk(writer).last_seq(PARENT)
    effect.committed(result_ref="action:1", result="ok")

    plan = _plan(writer, fork_at)
    (call,) = plan.disclosure.committed
    assert call.intended_seq <= fork_at < (call.settled_seq or 0)
    assert "/tmp/inflight" in obs.disclosure(plan)


def test_a_call_that_may_have_happened_is_never_reported_as_one_that_did(writer) -> None:
    """The whole `uncertain` vocabulary exists so that nobody has to choose between "it
    happened" and "it did not" when nobody knows. A fork disclosure is the last place that
    distinction could be thrown away, so it is kept: two blocks, two sentences."""
    rj = RunJournal(writer, PARENT)
    _started(rj)
    fork_at = _disk(writer).last_seq(PARENT)
    _requested(rj, "notify_user", {"title": "Lease renewal", "body": "submitted"})
    _effect(writer, tool="notify_user", args={"title": "Lease renewal"}, settle=None)

    plan = _plan(writer, fork_at)
    assert plan.disclosure.committed == ()
    (call,) = plan.disclosure.unresolved
    assert call.status is None
    text = obs.disclosure(plan)
    assert "Since that point I:" not in text
    assert "I may also have:" in text
    assert "whether it happened is not known" in text
    assert "Lease renewal" in text


def test_an_effect_an_earlier_resume_gave_up_on_keeps_saying_so_after_a_fork(writer) -> None:
    """A run can be resumed and then forked. The resume closed the effect as `uncertain`,
    and a disclosure that read that closure as "settled, therefore done" would report an
    email nobody can account for as sent."""
    rj = RunJournal(writer, PARENT)
    _started(rj)
    fork_at = _disk(writer).last_seq(PARENT)
    _requested(rj, "fs_write", {"path": "/tmp/maybe"})
    _effect(writer, tool="fs_write", args={"path": "/tmp/maybe"}, settle=None)
    R.resume(PARENT, writer=writer, reason="process restart")

    plan = _plan(writer, fork_at)
    assert plan.disclosure.committed == ()
    (call,) = plan.disclosure.unresolved
    assert call.status == R.UNCERTAIN
    assert "an earlier resume gave up on it" in obs.disclosure(plan)


def test_a_call_that_failed_is_not_disclosed_as_work_the_run_did(writer) -> None:
    """Session 3b's rule: a call that returned a failure did not produce its effect. It is
    still mentioned, because the user may remember asking for it, but as a thing that
    changed nothing rather than as one of the run's accomplishments."""
    rj = RunJournal(writer, PARENT)
    _started(rj)
    fork_at = _disk(writer).last_seq(PARENT)
    _requested(rj, "fs_write", {"path": "/tmp/nope"})
    _effect(writer, tool="fs_write", args={"path": "/tmp/nope"}, settle="failed")

    plan = _plan(writer, fork_at)
    assert plan.disclosure.committed == () and plan.disclosure.unresolved == ()
    assert len(plan.disclosure.failed) == 1
    text = obs.disclosure(plan)
    assert "returned a failure, so it changed nothing outside" in text
    assert "Since that point I:" not in text


def test_a_fork_past_nothing_says_nothing_happened_rather_than_staying_silent(writer) -> None:
    """The empty case is a statement and not an omission. "No effecting call was recorded
    after that point" is what the journal supports - `effect_intended` is synchronous and
    precedes the handler - and it is what the user needs in order to stop worrying."""
    rj = RunJournal(writer, PARENT)
    _started(rj)
    _message(rj, "user", "one")
    _message(rj, "assistant", "two")

    plan = _plan(writer, 2)
    assert plan.disclosure.nothing_recorded
    text = obs.disclosure(plan)
    assert "no effecting call after that point" in text
    assert "nothing outside this conversation was changed" in text


def test_a_call_whose_arguments_were_never_recorded_is_named_as_one(writer) -> None:
    """An effect from a caller that never went through the loop - detached, MCP - has no
    `tool_requested` and therefore no arguments. Saying "(arguments not recorded)" is the
    one honest line; empty brackets would read as a call made with no arguments."""
    rj = RunJournal(writer, PARENT)
    _started(rj)
    fork_at = _disk(writer).last_seq(PARENT)
    _effect(writer, tool="fs_write", args={"path": "/tmp/quiet"})

    plan = _plan(writer, fork_at)
    (call,) = plan.disclosure.committed
    assert call.arguments is None and call.subject is None
    assert "fs_write (arguments not recorded)" in obs.disclosure(plan)


def test_the_path_survives_a_call_whose_other_arguments_are_long(writer) -> None:
    """Session 4c's bug, in a new renderer. `brief_args` truncates the joined line at 120
    characters in call order, so a call with long arguments ahead of the path drops the one
    thing the disclosure exists to name. Two long arguments are needed to make it bite."""
    rj = RunJournal(writer, PARENT)
    _started(rj)
    fork_at = _disk(writer).last_seq(PARENT)
    args = {"content": "c" * 400, "encoding": "e" * 400, "path": "/home/dylan/notes/lease.md"}
    _requested(rj, "fs_write", args)
    _effect(writer, tool="fs_write", args=args)

    plan = _plan(writer, fork_at)
    assert "/home/dylan/notes/lease.md" in obs.disclosure(plan)


def test_the_disclosure_reports_a_workers_effects_as_the_runs_own(writer) -> None:
    """A worker's events live in its caller's run, and its effects carry no `worker_id` at
    all (session 3b). The user asked the run to do something and something was done; who
    inside the run did it is not a distinction a disclosure is allowed to drop work behind."""
    rj = RunJournal(writer, PARENT)
    _started(rj)
    fork_at = _disk(writer).last_seq(PARENT)
    worker = rj.for_worker("w1")
    _requested(worker, "fs_write", {"path": "/tmp/by-the-worker"}, step_id="w1.s1")
    _effect(writer, tool="fs_write", args={"path": "/tmp/by-the-worker"}, step_id="w1.s1")

    plan = _plan(writer, fork_at)
    assert len(plan.disclosure.committed) == 1
    assert "/tmp/by-the-worker" in obs.disclosure(plan)


# --- the state the new run starts from ---------------------------------------


def test_a_fork_rehydrates_the_conversation_as_it_stood_at_the_fork_point(writer) -> None:
    """The rewind, as a message list. It reuses session 4b's fold rather than a second one:
    a fork-only rehydration would be free to disagree with a resume about what a run said,
    and the two would drift where nobody was looking."""
    rj = RunJournal(writer, PARENT)
    _started(rj)
    _message(rj, "user", "the first thing")
    fork_at = _disk(writer).last_seq(PARENT)
    _message(rj, "assistant", "the thing being rewound")

    plan = _plan(writer, fork_at)
    said = [m.preview for m in plan.rehydration.messages]
    assert "the first thing" in said
    assert "the thing being rewound" not in said
    assert plan.rehydration.through_seq == fork_at
    assert plan.parent_last_seq > fork_at


def test_a_fork_hands_back_previews_and_never_calls_them_the_conversation(writer) -> None:
    """Session 4b's rule, inherited rather than re-decided: the journal holds 200 characters
    and the bodies are in the archive. A rehydration that handed the preview back as content
    would give the forked run a fluent memory of words nobody said."""
    rj = RunJournal(writer, PARENT)
    _started(rj)
    _message(rj, "user", "x" * 500)

    plan = _plan(writer, _disk(writer).last_seq(PARENT))
    (message,) = [m for m in plan.rehydration.messages if m.role == "user"]
    assert message.truncated and message.chars == 500
    assert len(message.preview) < message.chars
    assert plan.rehydration.archive is not None
    assert not hasattr(plan.rehydration, "model_messages")


def test_calls_still_open_at_the_fork_point_are_the_parents_and_are_not_hidden(writer) -> None:
    """A fork is not a reconciliation. An effect interrupted *before* the fork point is
    still the parent's to answer for, and it is surfaced on the plan so that a fork cannot
    be the thing that quietly buries it."""
    rj = RunJournal(writer, PARENT)
    _started(rj)
    _requested(rj, "fs_write", {"path": "/tmp/open"})
    _effect(writer, tool="fs_write", args={"path": "/tmp/open"}, settle=None)

    plan = _plan(writer, _disk(writer).last_seq(PARENT))
    (orphan,) = plan.open_at_fork
    assert orphan.tool == "fs_write" and orphan.disposition == R.UNCERTAIN
    # And it is the parent's: nothing was written to reconcile it here.
    assert EffectLedger(writer).get(orphan.idempotency_key).state == "started"


# --- what a fork refuses -----------------------------------------------------


def test_a_fork_point_inside_a_delegation_is_refused(writer) -> None:
    """Workers are atomic. A fork inside one would hand the new run an open delegation that
    nothing can finish, and "re-delegated, not resumed" is the same rule that refuses a
    mid-worker checkpoint - derived from the journal in one place, not decided twice."""
    rj = RunJournal(writer, PARENT)
    _started(rj)
    rj.emit(
        "worker_created",
        {
            "worker_id": "w1", "name": "researcher", "role": "researcher", "autonomy": "ask",
            "max_steps": 4, "tools": [], "task_chars": 3, "task_preview": "dig",
            "parent_step_id": "s1",
        },
    )
    inside = _disk(writer).last_seq(PARENT)
    rj.emit(
        "worker_finished",
        {
            "worker_id": "w1", "name": "researcher", "status": "ok", "summary_chars": 2,
            "tainted": False, "artifacts": 0, "citations": 0, "candidates": 0, "tokens": 0,
            "duration_ms": 1,
        },
    )
    with pytest.raises(F.MidWorkerFork):
        _plan(writer, inside)
    # And the position after the worker returns is fine.
    assert _plan(writer, _disk(writer).last_seq(PARENT)) is not None


def test_a_fork_point_the_run_never_reached_is_refused_rather_than_clamped(writer) -> None:
    """Clamping to the last seq would rewind to somewhere the user did not ask for and
    report a disclosure about a stretch of history that does not exist."""
    rj = RunJournal(writer, PARENT)
    _started(rj)
    _message(rj, "user", "one")
    with pytest.raises(F.NoSuchForkPoint):
        _plan(writer, 99)
    with pytest.raises(F.NoSuchForkPoint):
        _plan(writer, 0)
    with pytest.raises(R.NoSuchRun):
        _plan(writer, 1, run_id="never-existed")


def test_a_fork_refuses_to_open_a_run_that_already_has_events(writer) -> None:
    """`run_forked` is the first event of the run it opens, or it is a run whose lineage is
    buried in the middle of somebody else's history."""
    rj = RunJournal(writer, PARENT)
    _started(rj)
    _message(rj, "user", "one")
    RunJournal(writer, "already-here").emit(
        "message_appended",
        {"role": "user", "actor": "user", "chars": 1, "preview": "x", "trust": "trusted"},
    )
    with pytest.raises(F.RunExists):
        F.fork(PARENT, at_seq=2, writer=writer, reason="rewind", new_run_id="already-here")
    with pytest.raises(F.ForkError):
        F.fork(PARENT, at_seq=2, writer=writer, reason="rewind", new_run_id=PARENT)


def test_an_effect_status_the_disclosure_has_no_sentence_for_raises(writer) -> None:
    """The house bug, refused: a new effect status quietly described as "interrupted" would
    be a claim about the world that nobody wrote. `SETTLED` raises for the same reason on the
    resume side."""
    call = F.DisclosedCall(
        effect_id="e1", tool="fs_write", effect_class="unsafe_write", step_id="s1",
        intended_seq=2, settled_seq=None, status="invented", arguments={"path": "/tmp/a"},
    )
    with pytest.raises(obs.ObservationError):
        obs._may_have_lines(F.by_tool([call]))
