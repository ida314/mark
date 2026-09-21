"""`uncertain` is the one status the runtime cannot resolve on its own, and both ways of
resolving it for the user are wrong.

Told an interrupted call is `blocked`, a run makes it again and the mail goes out twice.
Told nothing at all, the run carries on and quietly reports a success that may never have
happened. Neither looks like an error afterwards, which is why each of these is a test and
not a paragraph in a design note:

- **`uncertain` and `blocked` are never the same observation.** `blocked` has exactly one
  producer, and it is evidence recorded *before* the act: no `effect_intended` at all. A
  call that reported a failure is not one of them - a failure response is not proof the
  effect did not land - and an uncertain `unsafe_write` never carries `retry`.
- **The question names the call.** An orphaned `web_fetch` shows its URL and several of
  them are asked about once, not once each. Both were ruled binding on this pass by Dylan
  at the Pass 3/4 boundary: a prompt the user cannot act on trains blind confirmation.
- **Nothing is dropped.** Every interrupted call in the rebuilt message list produces
  exactly one observation, and every observation carries at least one path.
- **Nothing is offered that cannot be delivered.** "I will check" is only said for tools a
  registered tool can actually read back.
- **The message list answers every tool call it carries**, and the answer says the outcome
  is unknown rather than inventing a failure.
"""

from __future__ import annotations

import pytest

from agentd.agent import observations as obs
from agentd.journal import resume as R
from agentd.journal.events import preview
from agentd.journal.ledger import EffectLedger
from agentd.journal.runtime import RunJournal
from agentd.journal.store import JournalStore
from agentd.journal.writer import JournalWriter


@pytest.fixture
def writer(tmp_path) -> JournalWriter:
    w = JournalWriter(JournalStore(tmp_path / "journal.db"))
    yield w
    w.close()


def _started(rj: RunJournal) -> None:
    rj.emit(
        "agent_started",
        {
            "session_id": "s1", "turn_id": "t1", "role": "main", "actor": "main",
            "origin": "interactive", "channel": "cli", "autonomy": "act", "model": "m",
            "max_steps": 8, "input_chars": 2, "input_preview": "hi", "parent_turn_id": None,
        },
    )


def _assistant(rj: RunJournal, step_id: str, text: str = "on it") -> None:
    rj.emit(
        "message_appended",
        {
            "role": "assistant", "actor": "main", "chars": len(text), "preview": preview(text),
            "trust": "trusted",
        },
        step_id=step_id,
    )


def _requested(rj: RunJournal, name: str, args: dict, *, step_id: str, call_id: str) -> None:
    rj.emit(
        "tool_requested",
        {"call_id": call_id, "name": name, "args": args, "visible": True, "known": True},
        step_id=step_id,
    )


def _call(
    rj: RunJournal,
    ledger: EffectLedger | None,
    name: str,
    args: dict,
    *,
    step_id: str = "p1",
    call_id: str = "c1",
    effect_class: str = "unsafe_write",
    dispatched: bool = True,
    run_id: str = "run-1",
):
    """One tool call the process died in the middle of, exactly as the loop lays it down.

    `ledger=None` is the call that never reached the announcement - refused by policy, or
    still waiting on an approval when the process went away.
    """
    _requested(rj, name, args, step_id=step_id, call_id=call_id)
    rj.emit("tool_started", {"call_id": call_id, "name": name}, step_id=step_id)
    if ledger is None:
        return None
    effect = ledger.intend(
        run_id=run_id, step_id=step_id, tool=name, effect_class=effect_class, args=args
    )
    if dispatched:
        effect.dispatched()
    return effect


def _plan(writer: JournalWriter, run_id: str = "run-1") -> R.ResumePlan:
    """What a second process would make of the file, which is the only reader that matters."""
    writer.flush()
    return R.plan(run_id, store=writer.store)


# --- the two words -----------------------------------------------------------


def test_an_interrupted_unsafe_write_is_uncertain_and_is_never_called_blocked(writer) -> None:
    """The call was entered and never reported back. "It did not happen" is the one thing
    nobody may say about it, because saying it is how the same action is taken twice."""
    rj = RunJournal(writer, "run-1")
    _started(rj)
    _assistant(rj, "p1")
    _call(rj, EffectLedger(writer), "web_fetch", {"url": "https://example.com/a"})

    (item,) = obs.observations(_plan(writer))
    assert item.status == obs.UNCERTAIN
    assert item.status != obs.BLOCKED
    assert item.evidence == R.MAY_HAVE_RUN
    assert "may have completed" in item.statement


def test_a_call_the_runtime_never_announced_is_blocked_rather_than_uncertain(writer) -> None:
    """`effect_intended` is synchronous and is written before the handler is awaited, so a
    request with no announcement behind it is the one shape where "nothing outside changed"
    is evidence rather than a hope. This is the crash that lands while an approval is still
    on screen, and it is the only producer of `blocked` in the runtime."""
    rj = RunJournal(writer, "run-1")
    _started(rj)
    _assistant(rj, "p1")
    _call(rj, None, "fs_write", {"path": "/tmp/x", "content": "hi"})

    (item,) = obs.observations(_plan(writer))
    assert item.status == obs.BLOCKED
    assert item.evidence == obs.NEVER_ANNOUNCED
    assert item.effect_class is None  # the journal does not say; it is not a default
    assert "did not get far enough to change anything" in obs.prompt(_plan(writer))


def test_only_a_call_that_provably_did_not_happen_may_be_retried_without_asking(writer) -> None:
    """The pass's hard rule, as data rather than as a sentence in a prompt. Both calls below
    are `web_fetch`; the difference is whether the runtime got as far as announcing one."""
    rj = RunJournal(writer, "run-1")
    _started(rj)
    _assistant(rj, "p1")
    _call(rj, EffectLedger(writer), "web_fetch", {"url": "https://example.com/a"}, call_id="c1")
    _call(rj, None, "web_fetch", {"url": "https://example.com/b"}, call_id="c2")

    unsure, stopped = obs.observations(_plan(writer))
    assert unsure.status == obs.UNCERTAIN and not unsure.may_retry
    assert obs.RETRY not in unsure.paths
    assert stopped.status == obs.BLOCKED and stopped.may_retry


def test_an_interrupted_read_is_never_put_to_the_user_as_a_question(writer) -> None:
    """`memory_search` is the runtime's most-called tool and was ruled `read` at the Pass
    3/4 boundary precisely so that recovery does not prompt about it. A read gets no effect
    row and no effect events, so it can be observed - it is still a call with no answer -
    but it can never be the thing the user is asked to decide."""
    rj = RunJournal(writer, "run-1")
    _started(rj)
    _assistant(rj, "p1")
    _call(rj, None, "memory_search", {"query": "the lease"})

    plan = _plan(writer)
    (item,) = obs.observations(plan)
    assert item.status == obs.BLOCKED
    question = obs.prompt(plan)
    assert "cannot tell whether" not in question
    assert "tell me to run" not in question


# --- the question the user is asked -------------------------------------------


def test_the_question_about_an_orphaned_fetch_names_the_url(writer) -> None:
    """Dylan's first ruling at the Pass 3/4 boundary, and the reason it is binding: "confirm
    this fetch?" with no URL in it is a question that can only be answered by habit."""
    rj = RunJournal(writer, "run-1")
    _started(rj)
    _assistant(rj, "p1")
    _call(rj, EffectLedger(writer), "web_fetch", {"url": "https://example.com/a?token=1"})

    question = obs.prompt(_plan(writer))
    assert "https://example.com/a?token=1" in question


def test_several_orphaned_fetches_are_asked_about_once_and_not_once_each(writer) -> None:
    """Dylan's second ruling. Three prompts are three chances to confirm blind; one prompt
    naming three URLs is a thing a person can actually read."""
    rj = RunJournal(writer, "run-1")
    _started(rj)
    _assistant(rj, "p1")
    ledger = EffectLedger(writer)
    for n, letter in enumerate("abc"):
        _call(
            rj, ledger, "web_fetch", {"url": f"https://example.com/{letter}"},
            call_id=f"c{n}", step_id="p1",
        )

    question = obs.prompt(_plan(writer))
    assert question.count("I was interrupted partway through") == 1
    assert "3 web_fetch calls" in question
    for letter in "abc":
        assert f"https://example.com/{letter}" in question


def test_a_second_attempt_that_never_started_still_warns_about_the_first(writer) -> None:
    """Why `never_dispatched` is not `blocked`. The ledger row is keyed by idempotency key,
    and a re-intent resets it to `intended` - so a row reading `intended` after a first
    attempt committed says "this attempt never started", not "this call never ran". Reading
    it as `blocked` would authorise a retry of a call that already went out."""
    rj = RunJournal(writer, "run-1")
    _started(rj)
    _assistant(rj, "p1")
    ledger = EffectLedger(writer)
    first = _call(rj, ledger, "notify_user", {"title": "the lease"})
    first.committed(result_ref="action:1")
    _call(rj, ledger, "notify_user", {"title": "the lease"}, call_id="c2", dispatched=False)

    done, item = obs.observations(_plan(writer))
    # The first attempt went through and only its result was lost - the loop's
    # `tool_finished` is buffered and its `effect_committed` is not.
    assert done.status == obs.UNREPORTED and not done.may_retry
    assert item.status == obs.UNCERTAIN
    assert item.evidence == R.NEVER_DISPATCHED and item.attempt == 2
    assert obs.EARLIER_ATTEMPT in item.statement
    assert obs.EARLIER_ATTEMPT in obs.prompt(_plan(writer))


def test_a_tool_with_nothing_to_read_back_asks_instead_of_promising_to_check(writer) -> None:
    """No registered tool lists watchers, so nothing the runtime can call would answer
    "was that reminder set?". Offering to check and then not checking is the same broken
    promise as a prompt with no URL in it."""
    rj = RunJournal(writer, "run-1")
    _started(rj)
    _assistant(rj, "p1")
    _call(rj, EffectLedger(writer), "reminder_set", {"text": "keys", "at": "18:00"})

    plan = _plan(writer)
    (item,) = obs.observations(plan)
    assert item.paths == (obs.ASK, obs.PROCEED_WITHOUT)
    assert "this one is yours" in obs.prompt(plan)


def test_a_tool_that_can_be_read_back_offers_to_do_it(writer) -> None:
    """The other half: where a read-back exists, the question leads with it, because the
    best answer to "did that happen" is to go and look rather than to ask the user."""
    rj = RunJournal(writer, "run-1")
    _started(rj)
    _assistant(rj, "p1")
    _call(rj, EffectLedger(writer), "fs_write", {"path": "/tmp/notes.md", "content": "hi"})

    plan = _plan(writer)
    (item,) = obs.observations(plan)
    assert item.path == obs.VERIFY
    assert obs.READBACK["fs_write"] in obs.prompt(plan)


# --- nothing dropped, nothing invented ----------------------------------------


def test_every_interrupted_call_produces_exactly_one_observation(writer) -> None:
    """The silent drop is the failure mode this session exists to prevent, so the count is
    asserted directly: three interrupted calls of three shapes, three observations, each
    with a path somebody could take."""
    rj = RunJournal(writer, "run-1")
    _started(rj)
    _assistant(rj, "p1")
    ledger = EffectLedger(writer)
    _call(rj, ledger, "web_fetch", {"url": "https://example.com/a"}, call_id="c1")
    _call(rj, ledger, "goal_upsert", {"title": "renew the lease"}, call_id="c2",
          effect_class="idempotent_write")
    _call(rj, None, "fs_read", {"path": "/tmp/x"}, call_id="c3")

    plan = _plan(writer)
    found = obs.observations(plan)
    assert len(found) == 3
    assert [o.tool for o in found] == ["web_fetch", "goal_upsert", "fs_read"]
    assert all(o.paths for o in found)
    # The convergent one needs no decision, and is still said out loud rather than dropped.
    question = obs.prompt(plan)
    assert "goal_upsert(title=renew the lease)" in question
    assert "nothing for you to decide" in question


def test_the_orchestrator_is_given_a_path_for_every_uncertain_call(writer) -> None:
    """The pass's requirement: an explicit path, not a status to interpret. The block also
    states the two forbidden moves, because the failure it is written against is a model
    that reads "interrupted" as "failed" and tidies it away."""
    rj = RunJournal(writer, "run-1")
    _started(rj)
    _assistant(rj, "p1")
    _call(rj, EffectLedger(writer), "fs_write", {"path": "/tmp/notes.md", "content": "hi"})

    block = obs.notice(_plan(writer))
    assert "`uncertain` is not `blocked`" in block
    assert "you may: " in block
    assert "verify" in block and "ask the user" in block and "proceed without it" in block
    assert "must not re-run an uncertain call" in block
    assert "must not pass over one without saying so" in block


def test_the_resumed_message_list_answers_every_tool_call_it_carries(writer) -> None:
    """Session 4b's open question 2, ruled here. Resume writes no `tool_failed` - the
    journal must not claim a terminal event the loop never wrote - but the message list is a
    reconstruction rather than a record, and an assistant message whose tool call nothing
    answers makes the model invent the result."""
    rj = RunJournal(writer, "run-1")
    _started(rj)
    _assistant(rj, "p1")
    _call(rj, EffectLedger(writer), "web_fetch", {"url": "https://example.com/a"}, call_id="c1")
    _call(rj, None, "fs_read", {"path": "/tmp/x"}, call_id="c2")

    plan = _plan(writer)
    dangling = {
        call.call_id for m in plan.rehydration.messages for call in m.calls
    } - {m.tool_call_id for m in plan.rehydration.messages if m.tool_call_id}
    assert dangling == {"c1", "c2"}
    closed = obs.closing_messages(plan)
    assert {m.tool_call_id for m in closed} == dangling
    assert all(m.synthetic for m in closed)


def test_a_closing_tool_message_never_says_the_call_failed(writer) -> None:
    """It says the one true thing: the outcome is unknown and nobody re-ran it. It also
    announces itself as the runtime's text, for the same reason 4b refused to hand a
    200-character preview to a model as a message body - text nobody said, presented as text
    somebody said, is a laundering channel."""
    rj = RunJournal(writer, "run-1")
    _started(rj)
    _assistant(rj, "p1")
    _call(rj, EffectLedger(writer), "web_fetch", {"url": "https://example.com/a"})

    (message,) = obs.closing_messages(_plan(writer))
    assert message.content.startswith(obs.RUNTIME_PREFIX)
    assert "not known" in message.content
    assert "failed" not in message.content and "error" not in message.content


def test_an_unknown_status_raises_rather_than_defaulting_to_a_question(writer) -> None:
    """A fourth kind of observation must not inherit whichever branch was written first.
    The same rule 4b applied to effect classes, in the layer that phrases the sentence."""
    with pytest.raises(obs.ObservationError):
        obs.paths_for("probably_fine", tool="web_fetch", effect_class="unsafe_write")


def test_looking_at_a_wrecked_run_writes_nothing(writer) -> None:
    """Reading is reading. The report a person asks for must not be the thing that closes
    the effects: `agent journal resume --apply` does that, deliberately and separately."""
    rj = RunJournal(writer, "run-1")
    _started(rj)
    _assistant(rj, "p1")
    _call(rj, EffectLedger(writer), "web_fetch", {"url": "https://example.com/a"})
    writer.flush()
    before = len(writer.store.read("run-1"))

    plan = _plan(writer)
    obs.observations(plan), obs.prompt(plan), obs.notice(plan), obs.closing_messages(plan)

    assert len(writer.store.read("run-1")) == before
    assert EffectLedger.reading(writer.store).get(
        plan.reconciliation.orphans[0].idempotency_key
    ).state == "started"


def test_the_url_survives_a_call_whose_other_arguments_are_long(writer) -> None:
    """The ruling is that the prompt shows the URL, and a renderer that truncates at 120
    characters decides what falls off the end. Rendering the arguments in call order let a
    long body push the identifying one off the line, which turns a binding requirement into
    a line the reader cannot act on without anybody noticing."""
    rj = RunJournal(writer, "run-1")
    _started(rj)
    _assistant(rj, "p1")
    _call(
        rj, EffectLedger(writer), "web_fetch",
        {"body": "x" * 400, "headers": "y" * 400, "url": "https://example.com/renew?id=7"},
        call_id="c1",
    )
    # And the same for a call that never reached the announcement, which is rendered by
    # the other of the two code paths that name a call.
    _call(
        rj, None, "web_fetch",
        {"body": "z" * 400, "headers": "y" * 400, "url": "https://example.com/chase?id=8"},
        call_id="c2",
    )

    unsure, stopped = obs.observations(_plan(writer))
    assert "url=https://example.com/renew?id=7" in unsure.subject
    assert "url=https://example.com/chase?id=8" in stopped.subject
    question = obs.prompt(_plan(writer))
    assert "https://example.com/renew?id=7" in question
    assert "https://example.com/chase?id=8" in question


def test_a_call_a_previous_resume_gave_up_on_is_still_uncertain_the_next_time(writer) -> None:
    """The second resume of the same run is where a silent drop would actually happen: the
    effect is closed, so it is no longer an orphan, and the tool call it belongs to is still
    unanswered. Reading a closure as "settled, nothing to say" would lose the one call
    anybody needed to hear about, on the restart after the one that reported it."""
    rj = RunJournal(writer, "run-1")
    _started(rj)
    _assistant(rj, "p1")
    _call(rj, EffectLedger(writer), "web_fetch", {"url": "https://example.com/a"})
    R.resume("run-1", writer=writer, reason="first restart")

    plan = _plan(writer)
    assert plan.reconciliation.orphans == ()  # closed; a second resume must not re-orphan it
    (item,) = obs.observations(plan)
    assert item.status == obs.UNCERTAIN
    assert item.evidence == obs.CLOSED_UNCERTAIN
    assert obs.RETRY not in item.paths
    assert "never established either way" in obs.prompt(plan)


def test_a_call_whose_effect_landed_is_not_offered_as_something_to_redo(writer) -> None:
    """`effect_committed` is synchronous and `tool_finished` is buffered, so the call that
    succeeded a moment before the kill leaves the same unanswered tool call as one that may
    never have run. It did run. Asking about it would be a question with a known answer, and
    offering to redo it would duplicate the one kind of call that must never be duplicated."""
    rj = RunJournal(writer, "run-1")
    _started(rj)
    _assistant(rj, "p1")
    effect = _call(rj, EffectLedger(writer), "notify_user", {"title": "the lease"})
    effect.committed(result_ref="action:1")

    plan = _plan(writer)
    (item,) = obs.observations(plan)
    assert item.status == obs.UNREPORTED
    assert obs.RETRY not in item.paths and obs.ASK not in item.paths
    assert "went through" in obs.prompt(plan)
    assert "cannot tell whether" not in obs.prompt(plan)


# --- a failure is not proof it did not happen ---------------------------------


def test_a_failed_unsafe_write_is_never_described_as_safe_to_run_again(writer) -> None:
    """The ruling at the Pass 4/5 boundary, and the reason it is not a wording preference:
    `effect.failed()` is written both when a handler returns `ok=False` and when it raises,
    and a fetch can fail after the server has already acted. Describing that as "running it
    again duplicates nothing" does not merely mislead a person, it authorises a model to
    send the thing twice. Asserted on all three surfaces, because the model reads the one
    the person never sees."""
    rj = RunJournal(writer, "run-1")
    _started(rj)
    _assistant(rj, "p1")
    effect = _call(rj, EffectLedger(writer), "web_fetch", {"url": "https://example.com/pay"})
    effect.failed("502 from upstream", result_ref="action:1")

    plan = _plan(writer)
    (item,) = obs.observations(plan)
    assert item.status == obs.UNCERTAIN and item.evidence == obs.EFFECT_FAILED
    assert obs.RETRY not in item.paths
    rendered = [obs.prompt(plan), obs.notice(plan)]
    rendered += [m.content for m in obs.closing_messages(plan)]
    for text in rendered:
        assert "duplicates nothing" not in text
    assert "reported a failure" in rendered[0]


def test_a_failed_call_that_converges_says_it_started_rather_than_that_it_was_interrupted(
    writer,
) -> None:
    """The other half of the same ruling. A failed `idempotent_write` keeps its retry path
    and lands in the one-line parenthetical, where "was interrupted" would be a plain
    untruth about a call that ran and came back with an error. Nothing here may read as
    "it did not get far enough to change anything" either: that is `blocked`'s sentence and
    this call is not blocked."""
    rj = RunJournal(writer, "run-1")
    _started(rj)
    _assistant(rj, "p1")
    effect = _call(
        rj, EffectLedger(writer), "goal_upsert", {"title": "renew the lease"},
        effect_class="idempotent_write",
    )
    effect.failed("the database went away", result_ref="action:1")

    plan = _plan(writer)
    (item,) = obs.observations(plan)
    assert item.status == obs.UNCERTAIN and item.may_retry
    question = obs.prompt(plan)
    assert "goal_upsert(title=renew the lease) started and reported a failure" in question
    assert "was interrupted too" not in question
    assert "did not get far enough to change anything" not in question
    assert "nothing for you to decide" in question
    # One line, not a block: a call that needs no decision is never turned into a question,
    # and it is named once rather than under a heading and again in the parenthetical.
    assert "cannot tell whether" not in question
    assert question.count("goal_upsert(title=renew the lease)") == 1


def test_the_one_line_for_calls_needing_no_decision_is_true_of_both_kinds_at_once(
    writer,
) -> None:
    """The convergent calls share a single parenthetical on purpose - 4c rejected a
    paragraph per group because a wall of text is a wall the reader skips. Sharing it means
    one sentence has to be true of a call that never reported back *and* of one that
    reported a failure, which it cannot be unless the clause is split."""
    rj = RunJournal(writer, "run-1")
    _started(rj)
    _assistant(rj, "p1")
    ledger = EffectLedger(writer)
    broke = _call(
        rj, ledger, "goal_upsert", {"title": "renew the lease"}, call_id="c1",
        effect_class="idempotent_write",
    )
    broke.failed("the database went away", result_ref="action:1")
    _call(
        rj, ledger, "goal_upsert", {"title": "chase the deposit"}, call_id="c2",
        effect_class="idempotent_write",
    )

    question = obs.prompt(_plan(writer))
    assert "goal_upsert(title=renew the lease) started and reported a failure" in question
    assert "goal_upsert(title=chase the deposit) was interrupted too" in question
    assert question.count("nothing for you to decide") == 1


def test_only_the_absence_of_an_announcement_can_make_a_call_blocked(writer) -> None:
    """`blocked` is the one status that authorises a silent re-run, so its producers are
    asserted as a set rather than trusted to stay at one. Routing the rest by
    `effect_class` is what keeps a failed `read` retryable and a failed send not."""
    assert [k for k, (status, _) in obs.SETTLED.items() if status == obs.BLOCKED] == [None]
    assert obs.paths_for(obs.UNCERTAIN, tool="web_search", effect_class="read")[0] == obs.RETRY
    assert obs.RETRY in obs.paths_for(
        obs.UNCERTAIN, tool="goal_upsert", effect_class="idempotent_write"
    )
    assert obs.RETRY not in obs.paths_for(
        obs.UNCERTAIN, tool="web_fetch", effect_class="unsafe_write"
    )

    rj = RunJournal(writer, "run-1")
    _started(rj)
    _assistant(rj, "p1")
    effect = _call(rj, EffectLedger(writer), "notify_user", {"title": "the lease"}, call_id="c1")
    effect.failed("the transport refused it", result_ref="action:1")
    _call(rj, None, "fs_write", {"path": "/tmp/x", "content": "hi"}, call_id="c2")

    sent, written = obs.observations(_plan(writer))
    assert sent.status == obs.UNCERTAIN
    assert written.status == obs.BLOCKED
    question = obs.prompt(_plan(writer))
    assert "1 fs_write call did not get far enough" in question
    assert "1 notify_user call did not get far enough" not in question


# --- the status word says less than the evidence does -------------------------


def test_the_model_is_told_what_separates_an_unreported_call_from_an_uncertain_one(
    writer,
) -> None:
    """An uncertain call is unreported too - its result did not come back either. The word
    is not what separates them; the evidence is, and it is `committed` for exactly one of
    them. So the heading carries that clause rather than the status word alone, and the
    user's block goes on saying it in full without ever using the word."""
    rj = RunJournal(writer, "run-1")
    _started(rj)
    _assistant(rj, "p1")
    ledger = EffectLedger(writer)
    effect = _call(rj, ledger, "notify_user", {"title": "the lease"}, call_id="c1")
    effect.committed(result_ref="action:1")
    _call(rj, ledger, "web_fetch", {"url": "https://example.com/a"}, call_id="c2")

    plan = _plan(writer)
    done, unsure = obs.observations(plan)
    assert (done.status, done.evidence) == (obs.UNREPORTED, obs.COMMITTED)
    # The word itself, because `ClosingMessage.status` hands it on with no evidence beside
    # it: a consumer that only reads the status is reading the one field that is ambiguous.
    assert done.status == "unreported"
    closing = {m.tool_call_id: m.status for m in obs.closing_messages(plan)}
    assert closing == {"c1": "unreported", "c2": "uncertain"}
    assert unsure.status == obs.UNCERTAIN and unsure.evidence != obs.COMMITTED
    block = obs.notice(plan)
    assert (
        "1 unreported - the call is recorded as having happened and its result did not "
        "survive. Do not run these again; say what they did if it matters:"
    ) in block
    question = obs.prompt(plan)
    assert "unreported" not in question
    assert "went through and I no longer have what it returned" in question


def test_a_write_no_registered_tool_can_see_is_never_offered_a_read_back(writer) -> None:
    """`memory_remember` does not write a fact: it queues a candidate that a review gate
    later promotes, merges or rejects. `memory_search` is ranked and reads facts, so it
    would report "nothing there" about a call that did exactly what it was asked to - and
    no registered tool lists candidates, so no exact lookup fixes it either. A read-back
    that can return a false negative is worse than no read-back, because the answer to
    "did that happen" is then a no that nobody doubts."""
    rj = RunJournal(writer, "run-1")
    _started(rj)
    _assistant(rj, "p1")
    _call(rj, EffectLedger(writer), "memory_remember", {"statement": "the lease ends in June"})

    plan = _plan(writer)
    (item,) = obs.observations(plan)
    assert "memory_remember" not in obs.READBACK
    assert item.paths == (obs.ASK, obs.PROCEED_WITHOUT)
    question = obs.prompt(plan)
    assert "this one is yours" in question
    assert "memory_search" not in question
    # `open_loop_add` keeps its read-back: `open_loops_list` is every row of that status
    # with its id, ordered rather than ranked, so an added loop cannot read as absent.
    assert obs.READBACK["open_loop_add"] == "list the open loops with open_loops_list"
