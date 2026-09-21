"""`uncertain` as something the orchestrator can act on, and the words it says out loud.

Session 4b closed an interrupted `unsafe_write` in the journal: the effect gets
`effect_committed(status="uncertain")`, the ledger row moves to `orphaned`, and nothing is
re-run. That is the durable half. This module is the other half - turning that record into
an observation with a *path*, and into sentences a person and a model can act on.

## Two words that are not synonyms, and a third the journal insisted on

    blocked      the work did not happen
    uncertain    it is not known whether the work happened
    result_lost  the work happened and what it returned did not survive

The first two are the pass file's. The third is not an invention: `effect_*` events are
synchronous and the loop's `tool_*` events are buffered, so a call whose effect committed
milliseconds before the kill leaves the same unanswered tool call in the rebuilt message
list as an orphan does. It has a known answer - yes, it happened - so making it `uncertain`
would be a question nobody needs to answer, and making it `blocked` would invite the
duplicate this pass exists to prevent.

The distinction is not pedantry, it is the whole recovery behaviour: a `blocked` call may
be made again, because making it again duplicates nothing. An `uncertain` one may not, ever,
without somebody deciding. Collapsing the two in either direction is a real failure - call
an uncertain send `blocked` and it goes out twice; call a blocked one `uncertain` and the
run stalls on a question with no content.

So `blocked` is produced here only from evidence that is durable **before** the act it
describes. A call is `blocked` when the journal holds the request and no `effect_intended`:
`effect_*` events are synchronous (`writer.SYNC_PREFIXES`) and the intent is recorded
before the handler is awaited, so no announcement means nothing outside was changed by it.
An effect the journal already closed as `failed` is `blocked` for the same reason: session
3b ruled that a call that returned a failure did not produce its effect.

`blocked` therefore claims exactly that much and no more. A `read` never gets an effect at
all, so every interrupted `read` lands here - and for a read the claim is still true, since
a read changes nothing outside by definition of the class, even in the case where the call
did run and its buffered `tool_finished` died with the process. The wording below says "did
not get far enough to change anything" rather than "never ran", because the second is a
claim about execution that this evidence does not support for a `read`.

Two caveats, stated rather than buried. Both are about the same assumption:

- A journal read without its `-wal` - a file copied the way the live-data checks copy it -
  is missing its most recent events, including synchronous ones. Under that reading a call
  that did run can look `blocked`. The file is not lying; the copy is incomplete.
- If an fsync does not mean what it says (pass-02 outcome, open question 2: power-loss
  durability is reasoned and not measured), a committed `effect_intended` could be lost.
  Nothing in this repo can do better than the storage layer's promise.

## The path is not the status

`status` is what is known. `paths` is what may be done about it, and every observation
carries at least one, because an observation with no path is the silent drop this session
exists to prevent.

    verify           read the world back and find out - only offered when a registered tool
                     can actually answer the question (`READBACK`)
    ask              put it to the user, with the arguments in the question
    proceed_without  carry on, and say so in the answer. Never silent.
    retry            make the call again. Only ever on an observation where re-making it
                     duplicates nothing: a `blocked` call, or an `idempotent_write`.

`retry` is never in an uncertain `unsafe_write`'s paths. That is the pass's one hard rule
expressed as data rather than as a sentence in a prompt.

## Why the wording is here and not at each caller

Session 4b deliberately left the question a user is asked unphrased, because phrasing it in
the CLI and again in the resumed turn is how the two drift apart. `prompt()` is the user's
sentence, `notice()` is the model's, `closing_messages()` is what goes in the message list,
and all three are built from the same observations. The requirements they have to meet were
settled by Dylan at the Pass 3/4 boundary (pass-03 outcome): an orphaned `web_fetch` names
its URL, and several orphaned fetches are one question and not one each. A prompt the user
cannot act on trains blind confirmation, which is worse than no prompt at all.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from ..journal import resume as R
from ..journal.render import LEADING_ARGS, brief_args
from ..tools import effects

# --- vocabulary --------------------------------------------------------------

UNCERTAIN = R.UNCERTAIN
BLOCKED = "blocked"
# The pass names two words and the journal turned out to hold a third shape. A call whose
# effect committed and whose `tool_finished` died in the buffer - `effect_*` is synchronous
# and the tool events are not, so this window is wider than the crash that makes an orphan -
# did happen, and only its result text is gone. Calling that `uncertain` would ask the user
# a question with a known answer; calling it `blocked` would invite a duplicate.
RESULT_LOST = "result_lost"
STATUSES: tuple[str, ...] = (UNCERTAIN, BLOCKED, RESULT_LOST)

VERIFY = "verify"
ASK = "ask"
PROCEED_WITHOUT = "proceed_without"
RETRY = R.RETRY  # the same word 4b's disposition uses, deliberately not a second one
PATHS: tuple[str, ...] = (VERIFY, ASK, PROCEED_WITHOUT, RETRY)

# 4b's three evidence values say how far an *announced* call got. These four say what the
# journal knows about a call the rebuilt message list is still waiting on.
NEVER_ANNOUNCED = "never_announced"  # no `effect_intended` at all: the only support for `blocked`
COMMITTED = "committed"  # the effect landed; only the result text went with the process
EFFECT_FAILED = "effect_failed"  # the call resolved without producing its effect
CLOSED_UNCERTAIN = "closed_uncertain"  # an earlier resume already gave up on it

# How a call is described to whoever has to decide. Keyed by evidence, and total over it:
# an evidence value with no sentence raises rather than printing an empty line, because a
# line the reader cannot act on is the thing this whole path is built to avoid.
STATEMENTS: dict[str, str] = {
    R.MAY_HAVE_RUN: "started, and never reported back - it may have completed",
    R.NEVER_DISPATCHED: "recorded, and this attempt was never started",
    R.UNKNOWN: "announced, with no record of whether it was started",
    NEVER_ANNOUNCED: "never started: the process exited before the call was announced",
    COMMITTED: "completed - the effect is recorded, and only its result went with the process",
    EFFECT_FAILED: "reported a failure, and the report went with the process",
    CLOSED_UNCERTAIN: "interrupted by an earlier restart, and never established either way",
}

# Added to a `never_dispatched` line when this was not the first attempt at the call. The
# ledger row is keyed by idempotency key and a re-intent resets it to `intended`, so
# "this attempt never started" is a statement about the attempt and not about the call.
EARLIER_ATTEMPT = " - an earlier attempt at the same call may still have run"

# What a registered tool can be called to answer "did that actually happen?". Only tools
# that really have a read-back are in here: offering to check and then not checking is the
# same broken promise as a prompt with no URL in it.
#
# The absences are findings rather than omissions. `reminder_set` and `watcher_add` write
# watcher rows that **no registered tool can list**, so their only honest path is to ask.
# `web_fetch` leaves no trace to read back - fetching the page again is a new fetch, not
# evidence about the old one. `notify_user`'s read-back is the user's own eyes.
# `shell_exec` and `delegate` do arbitrary things and nothing generic can check them.
READBACK: dict[str, str] = {
    "fs_write": "read the file back with fs_read and see whether it holds the new content",
    "memory_remember": "look the statement up with memory_search",
    "open_loop_add": "list the open loops with open_loops_list",
}


class ObservationError(Exception):
    """An observation could not be described. Raised, never rendered as a blank line."""


def paths_for(status: str, *, tool: str, effect_class: str | None) -> tuple[str, ...]:
    """Every path open for this observation, the runtime's recommendation first.

    Total over `STATUSES` and never empty. An unknown status raises: a new kind of
    observation that quietly inherited "ask" would be a question nobody wrote.
    """
    if status == BLOCKED:
        # The work did not happen, so making the call again duplicates nothing. It is an
        # ordinary call: it passes policy and approval again like any other.
        return (RETRY, PROCEED_WITHOUT)
    if status == UNCERTAIN:
        if effect_class != effects.UNSAFE_WRITE:
            # `idempotent_write`: the pass's reconciliation table says re-execute, and
            # re-execution converges. It is still uncertain; it just needs no ceremony.
            return (RETRY, PROCEED_WITHOUT)
        return ((VERIFY,) if tool in READBACK else ()) + (ASK, PROCEED_WITHOUT)
    if status == RESULT_LOST:
        # It happened. Nothing here may put it back on the table as a retry for an
        # `unsafe_write`: that is the one call where doing it twice is the whole danger,
        # and this is the case where we know for certain it was done once.
        if effect_class != effects.UNSAFE_WRITE:
            return (RETRY, PROCEED_WITHOUT)
        return ((VERIFY,) if tool in READBACK else ()) + (PROCEED_WITHOUT,)
    raise ObservationError(f"no paths defined for status {status!r}; expected one of {STATUSES}")


@dataclass(frozen=True)
class Observation:
    """One interrupted call, what is known about it, and what may be done.

    `subject` is `None` only when the journal holds no arguments for the call - a detached
    or MCP caller, neither of which goes through the loop that records them. It is not a
    formatting failure and it is never rendered as empty brackets.
    """

    status: str
    tool: str
    subject: str | None
    evidence: str
    effect_class: str | None
    seq: int
    paths: tuple[str, ...]
    effect_id: str | None = None
    call_id: str | None = None
    step_id: str | None = None
    attempt: int = 1

    @property
    def path(self) -> str:
        """The path the runtime recommends. Recommending is not deciding."""
        return self.paths[0]

    @property
    def may_retry(self) -> bool:
        return RETRY in self.paths

    @property
    def statement(self) -> str:
        """How far the call got, in one clause, for a person."""
        try:
            said = STATEMENTS[self.evidence]
        except KeyError:
            raise ObservationError(
                f"no statement for evidence {self.evidence!r}; expected one of "
                f"{', '.join(STATEMENTS)}"
            ) from None
        if self.evidence == R.NEVER_DISPATCHED and self.attempt > 1:
            return said + EARLIER_ATTEMPT
        return said

    @property
    def name(self) -> str:
        """The call as a line, always naming what it was called with when that is known."""
        return self.subject or f"{self.tool} (arguments not recorded)"


@dataclass(frozen=True)
class ObservationGroup:
    """Every observation of one tool with one status, together.

    Grouping is a requirement and not a tidiness: four interrupted fetches are one question
    naming four URLs, because four questions are four chances to confirm blind (pass-03
    outcome, the rulings settled at the Pass 3/4 boundary).
    """

    tool: str
    status: str
    observations: tuple[Observation, ...]

    @property
    def paths(self) -> tuple[str, ...]:
        """The group's paths. Uniform by construction: they depend on the tool and the
        status, which are the group key, and never on the evidence."""
        return self.observations[0].paths

    @property
    def readback(self) -> str | None:
        return READBACK.get(self.tool)


# --- reading a plan ----------------------------------------------------------


def observations(plan: R.ResumePlan) -> tuple[Observation, ...]:
    """Every interrupted call in this run, in the order the journal recorded them.

    Exactly one observation per interrupted call and no call left out: an orphaned effect
    becomes `uncertain`, and a tool call the message list is still waiting on with no effect
    behind it becomes `blocked`. The two sets do not overlap - an orphan by definition has
    an `effect_intended` - and the join is `(step_id, call_id)`, because a `call_id` comes
    from the model and is only unique within a step (session 4b found two calls in one run
    both called `fake_0`).
    """
    found: list[Observation] = []
    claimed: set[tuple[str | None, str | None]] = set()
    for orphan in plan.reconciliation.orphans:
        claimed.add((orphan.step_id, orphan.call_id))
        found.append(
            Observation(
                status=UNCERTAIN,
                tool=orphan.tool,
                subject=orphan.subject,
                evidence=orphan.evidence,
                effect_class=orphan.effect_class,
                seq=orphan.intended_seq,
                paths=paths_for(UNCERTAIN, tool=orphan.tool, effect_class=orphan.effect_class),
                effect_id=orphan.effect_id,
                call_id=orphan.call_id,
                step_id=orphan.step_id,
                attempt=orphan.attempt,
            )
        )
    settled = {
        (a.step_id, a.call_id): a for a in plan.reconciliation.announced if a.status is not None
    }
    for call, step_id in _unanswered(plan.rehydration):
        if (step_id, call.call_id) in claimed:
            continue
        effect = settled.get((step_id, call.call_id))
        status, evidence = _settled_as(effect)
        found.append(
            Observation(
                status=status,
                tool=call.name,
                subject=f"{call.name}({brief_args(call.arguments, lead=LEADING_ARGS)})",
                evidence=evidence,
                # `None` when no effect was ever announced: the journal does not say what
                # class the tool was, and that is different from the tool having none.
                effect_class=effect.effect_class if effect is not None else None,
                seq=call.seq,
                paths=paths_for(
                    status,
                    tool=call.name,
                    effect_class=effect.effect_class if effect is not None else None,
                ),
                effect_id=effect.effect_id if effect is not None else None,
                call_id=call.call_id,
                step_id=step_id,
            )
        )
    return tuple(sorted(found, key=lambda o: o.seq))


# How an already-closed effect behind a call nobody answered is described. A `read` never
# gets an effect at all, so `None` here is both "the approval was still on screen when the
# process died" and "this was a read" - which is why `blocked` claims only that nothing
# outside was changed by it.
SETTLED: dict[str | None, tuple[str, str]] = {
    None: (BLOCKED, NEVER_ANNOUNCED),
    "committed": (RESULT_LOST, COMMITTED),
    "failed": (BLOCKED, EFFECT_FAILED),
    "uncertain": (UNCERTAIN, CLOSED_UNCERTAIN),
}


def _settled_as(effect: R.AnnouncedCall | None) -> tuple[str, str]:
    """What to call a dangling call, given the state its effect reached.

    Raises on a status this module has not been taught, rather than falling through to
    `blocked`: a new effect status quietly reported as "it did not happen" is the failure
    this whole module is built to prevent.
    """
    status = effect.status if effect is not None else None
    try:
        return SETTLED[status]
    except KeyError:
        raise ObservationError(
            f"effect status {status!r} has no observation; expected one of "
            f"{', '.join(k for k in SETTLED if k is not None)}"
        ) from None


def _unanswered(rehydration: R.Rehydration) -> tuple[tuple[R.ToolCallRef, str | None], ...]:
    """The tool calls in the rebuilt message list that no tool message answers.

    Read out of the message list rather than out of the raw events on purpose: a dangling
    call matters because the model's message list wants one tool message per tool call, so
    the question is about the list that will be handed back, not about the file.
    """
    answered = {
        (m.step_id, m.tool_call_id) for m in rehydration.messages if m.tool_call_id is not None
    }
    return tuple(
        (call, message.step_id)
        for message in rehydration.messages
        for call in message.calls
        if (message.step_id, call.call_id) not in answered
    )


def groups(found: Sequence[Observation]) -> tuple[ObservationGroup, ...]:
    """One group per (status, tool), first seen first."""
    order: list[tuple[str, str]] = []
    by_key: dict[tuple[str, str], list[Observation]] = {}
    for item in found:
        key = (item.status, item.tool)
        if key not in by_key:
            by_key[key] = []
            order.append(key)
        by_key[key].append(item)
    return tuple(
        ObservationGroup(tool=tool, status=status, observations=tuple(by_key[(status, tool)]))
        for status, tool in order
    )


# --- the words ---------------------------------------------------------------


def prompt(plan: R.ResumePlan) -> str:
    """What the user is told and asked, after a run is picked up. Empty when nothing broke.

    One block per tool, every call named with its arguments, and the question stated in
    terms the reader can answer. Nothing here offers to re-run a call that may already have
    happened; if the user wants that they can say so, and it is their sentence rather than
    a pre-ticked box.
    """
    found = observations(plan)
    if not found:
        return ""
    # The standing rule is stated once, at the top, and not repeated per group. Repeating
    # "I have not re-run them and I will not" under four headings is how a warning turns
    # into a wall a reader skips, which is the same failure as a prompt with no URL in it.
    blocks: list[str] = [
        "Picking this run back up. Some of it was interrupted, and I have not re-run "
        "anything on my own - here is what I know and what I need from you."
    ]
    for group in groups(found):
        if group.status == UNCERTAIN and RETRY in group.paths:
            continue  # an idempotent_write: re-running it changes nothing. Mentioned below.
        if group.status == UNCERTAIN:
            blocks.append(_uncertain_block(group))
        elif group.status == RESULT_LOST:
            blocks.append(_result_lost_block(group))
        else:
            blocks.append(_blocked_block(group))
    convergent = [o for o in found if o.status == UNCERTAIN and o.may_retry]
    if convergent:
        names = ", ".join(o.name for o in convergent)
        blocks.append(
            f"({names} {'was' if len(convergent) == 1 else 'were'} interrupted too. Running "
            f"{'it' if len(convergent) == 1 else 'them'} again changes nothing, so there is "
            f"nothing for you to decide.)"
        )
    return "\n\n".join(blocks)


def _uncertain_block(group: ObservationGroup) -> str:
    count = len(group.observations)
    calls = "call" if count == 1 else "calls"
    head = (
        f"I was interrupted partway through {count} {group.tool} {calls} and I cannot tell "
        f"whether {'it' if count == 1 else 'they'} happened:"
    )
    lines = [f"  - {o.name}\n      {o.statement}" for o in group.observations]
    it = "it" if count == 1 else "them"
    they = "it" if count == 1 else "they"
    if group.readback:
        tail = (
            f"Say \"check\" and I will {group.readback}; \"leave it\" and I carry on "
            f"without {it} and say so in what I write; or tell me to run {it} again if you "
            f"know {they} did not happen."
        )
    else:
        tail = (
            f"Nothing I can call will tell me whether {they} happened, so this one is "
            f"yours: say \"leave it\" and I carry on without {it} and say so in what I "
            f"write, or tell me to run {it} again if you know {they} did not happen."
        )
    return "\n".join([head, "", *lines, "", tail])


def _blocked_block(group: ObservationGroup) -> str:
    count = len(group.observations)
    calls = "call" if count == 1 else "calls"
    head = (
        f"{count} {group.tool} {calls} did not get far enough to change anything: the run "
        f"ended before the runtime recorded an effect for "
        f"{'it' if count == 1 else 'any of them'}. Running "
        f"{'it' if count == 1 else 'them'} again duplicates nothing."
    )
    lines = [f"  - {o.name}" for o in group.observations]
    return "\n".join([head, "", *lines])


def _result_lost_block(group: ObservationGroup) -> str:
    """A statement and not a question: the answer to "did that happen" is yes."""
    count = len(group.observations)
    calls = "call" if count == 1 else "calls"
    head = (
        f"{count} {group.tool} {calls} went through and I no longer have what "
        f"{'it' if count == 1 else 'they'} returned - the record that "
        f"{'it happened' if count == 1 else 'they happened'} survived the restart and the "
        f"result did not. I am not running "
        f"{'it' if count == 1 else 'them'} again:"
    )
    lines = [f"  - {o.name}" for o in group.observations]
    tail = (
        f"If you need what {'it' if count == 1 else 'they'} returned, say \"check\" and I "
        f"will {group.readback}."
        if group.readback
        else None
    )
    return "\n".join([head, "", *lines] + (["", tail] if tail else []))


def notice(plan: R.ResumePlan) -> str:
    """The block a resumed orchestrator is given before it does anything else.

    It states the vocabulary, names every interrupted call with its arguments, lists the
    paths open for each, and says the two things that are forbidden. It is deliberately
    blunt: the failure it is written against is an agent that reads "interrupted" as
    "failed", re-runs the call, and reports a tidy success.
    """
    found = observations(plan)
    if not found:
        return ""
    unsure = [o for o in found if o.status == UNCERTAIN]
    stopped = [o for o in found if o.status == BLOCKED]
    done = [o for o in found if o.status == RESULT_LOST]
    parts = [
        "Calls interrupted by the restart you are picking this run up from. Read this "
        "before you do anything else.",
        "",
        "`uncertain` is not `blocked`. Blocked means the work did not happen. Uncertain "
        "means nobody knows whether it happened, and the run cannot find out by itself.",
    ]
    if unsure:
        parts += ["", f"{len(unsure)} uncertain:"]
        parts += [
            f"  - {o.name}\n      {o.statement}\n      you may: {_paths_sentence(o)}"
            for o in unsure
        ]
    if done:
        parts += ["", f"{len(done)} happened, with the result lost rather than the call. "
                  "Do not run these again; say what they did if it matters:"]
        parts += [f"  - {o.name}\n      you may: {_paths_sentence(o)}" for o in done]
    if stopped:
        parts += ["", f"{len(stopped)} blocked - none of these changed anything outside, "
                  "and running one again duplicates nothing:"]
        parts += [f"  - {o.name}" for o in stopped]
    if unsure:
        parts += [
            "",
            "You must not re-run an uncertain call, and you must not pass over one without "
            "saying so. Take one of the paths listed for each of them. If you proceed "
            "without a call, the answer you give has to say that you did.",
        ]
    return "\n".join(parts)


def _paths_sentence(observation: Observation) -> str:
    phrases = {
        VERIFY: f"verify - {READBACK.get(observation.tool, 'read the result back')}",
        ASK: "ask the user, naming the call and its arguments",
        PROCEED_WITHOUT: "proceed without it, and say in your answer that you did",
        RETRY: "run it again - re-running this one duplicates nothing",
    }
    return "; ".join(phrases[p] for p in observation.paths)


# --- what the resumed message list carries -----------------------------------

# Prefixed on every synthetic tool message. Session 4b refused to hand the journal's
# 200-character previews to a model as message bodies, because text nobody said, presented
# as text somebody said, is a laundering channel. The same rule applies to this text: it is
# the runtime's, not the tool's, and it says so in the first characters the model reads.
RUNTIME_PREFIX = "[runtime, on resume - not output from the tool]"

CLOSING_TEXT: dict[str, str] = {
    UNCERTAIN: (
        "This call was interrupted by the process exiting. Whether it completed is not "
        "known, and it has not been re-run. Do not re-run it yourself."
    ),
    BLOCKED: (
        "This call was interrupted by the process exiting. The runtime never recorded it "
        "as started, so it changed nothing outside and no result came back. Making the "
        "call again duplicates nothing."
    ),
    RESULT_LOST: (
        "This call went through - the runtime holds the record that it did - and what it "
        "returned was lost when the process exited. Do not run it again."
    ),
}


@dataclass(frozen=True)
class ClosingMessage:
    """A tool message for a call whose real result died with the process.

    Session 4b's open question 2, ruled here: the resumed message list **does** carry one.
    Resume writes no `tool_failed` - inventing a terminal tool event would put a lie in the
    journal about a call the loop never finished - but the message list handed to a model is
    a reconstruction rather than a record, and a model given an assistant message with a
    tool call and no answering tool message either errors out at the provider or invents the
    result. Saying "this was interrupted and the outcome is unknown" is the one answer that
    is both present and true.

    `synthetic` is on the record rather than implied, and the text begins with
    `RUNTIME_PREFIX`, so nothing downstream can mistake it for something a tool returned.
    """

    tool_call_id: str
    tool: str
    status: str
    content: str
    step_id: str | None = None
    synthetic: bool = True


def closing_messages(plan: R.ResumePlan) -> tuple[ClosingMessage, ...]:
    """One tool message per interrupted call the message list is still waiting on.

    Only for calls with a `call_id`: an effect announced by a caller that never went through
    the loop (detached, MCP) has no tool call in any message list, so there is nothing to
    close and nothing to invent.
    """
    out = []
    for item in observations(plan):
        if item.call_id is None:
            continue
        out.append(
            ClosingMessage(
                tool_call_id=item.call_id,
                tool=item.tool,
                status=item.status,
                content=f"{RUNTIME_PREFIX} {CLOSING_TEXT[item.status]}",
                step_id=item.step_id,
            )
        )
    return tuple(out)


__all__ = [
    "ASK",
    "BLOCKED",
    "CLOSED_UNCERTAIN",
    "CLOSING_TEXT",
    "COMMITTED",
    "EARLIER_ATTEMPT",
    "EFFECT_FAILED",
    "NEVER_ANNOUNCED",
    "PATHS",
    "PROCEED_WITHOUT",
    "READBACK",
    "RESULT_LOST",
    "RETRY",
    "RUNTIME_PREFIX",
    "SETTLED",
    "STATEMENTS",
    "STATUSES",
    "UNCERTAIN",
    "VERIFY",
    "ClosingMessage",
    "Observation",
    "ObservationError",
    "ObservationGroup",
    "closing_messages",
    "groups",
    "notice",
    "observations",
    "paths_for",
    "prompt",
]
