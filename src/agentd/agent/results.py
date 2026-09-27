"""What a worker comes back with, and what makes it a result rather than prose.

`delegation.py` is the request; this is the response. A delegation that cannot be told apart
from a worker rambling is a delegation whose failures are invisible, so there are two objects
here and the difference between them is the pass:

* `WorkerReport` is what the *model* is asked to emit - a pydantic schema, sent to
  `complete_json` for guided decoding. It is what a worker claims.
* `WorkerResult` is what the *runtime* concluded - a frozen dataclass nothing decodes into.
  It is what the caller is told.

Nothing turns the first into the second except `validate_report`, and it is allowed to
disagree with the worker: a worker cut off by its step budget does not get to call itself
completed. That reconciliation happens **after** decoding, deliberately. A cross-field
`model_validator` on `WorkerReport` would make the disagreement a `ValidationError` inside
`complete_json`, which gets one repair attempt and then raises `LLMError` - and an honest
"I ran out of steps" report would be thrown away by the schema instead of being recorded as
the unfinished work it is.

The three statuses are the pass's, and they are read against the vocabulary Pass 4c fixed
for effects, because the two words mean the same things in both places:

    completed   the work happened and this is the report of it
    blocked     the work did not happen, and the worker says why
    uncertain   something happened and nobody can say what it amounts to

`uncertain` is the one a malformed report lands in, and that is a safety property rather
than a hedge. `blocked` means nothing happened, so re-running a blocked delegation costs a
worker; re-running an *uncertain* one re-runs whatever the worker already did to the file
system before its report came back wrong. A report that cannot be read is never blocked.

Transcripts: `WorkerResult.transcript` is the worker's own prose, carried for the runtime's
use - debugging, evaluation, auditing. It is not persisted: session 6c caches results and
deliberately leaves the transcript out of the entry, so a result served from the cache
carries `reused_from` and an empty transcript rather than a stale copy of one.
`for_orchestrator()` is the only function that turns a result into something a model is
shown, and it omits the transcript and the raw decode error unless it is asked for them.
The decode error is on that list because it is a pydantic `ValidationError` string, which
quotes the model's own malformed output back.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, Field

from ..journal.events import VALIDATION_STATUSES, WORKER_STATUSES

# The journal declares them; this module does not get a second opinion. The `Literal` on
# `WorkerReport.status` below has to spell them out - a type annotation cannot read a tuple -
# and `test_the_result_schema_and_the_journal_agree_about_a_worker_status` is what keeps the
# two spellings the same.
STATUSES: tuple[str, ...] = WORKER_STATUSES
# Session 9a, and declared in the journal for the same reason the three statuses are: the
# enum the journal enforces and the words the runtime can produce must not drift apart.
VALIDATIONS: tuple[str, ...] = VALIDATION_STATUSES

# Every note is written here, by the runtime, in full. None of them is assembled from model
# output, which is what makes `notes` safe to put in front of the orchestrator while the
# transcript and the decode error are not.
NOTE_UNREADABLE = (
    "the worker did not return a usable result; its transcript is retained by the runtime"
)
NOTE_NO_ANSWER = "the worker returned the result schema with nothing in its answer"
NOTE_BUDGET = "the worker ran out of steps before it finished, so this describes unfinished work"


@dataclass(frozen=True)
class Flag:
    """One contradiction the runtime found between a report and the journal behind it.

    `detail` is written by `agent/verification.py`, in full, quoting the journal - a tool
    name, an exit code, a path. Nothing in it comes from the worker, which is what makes a
    flag safe to render for the orchestrator while the transcript is not.

    `hard` is the difference between "this is not true" and "this cannot be corroborated",
    and it is a property of the check rather than of the run: see `verification.HARD_FLAGS`.
    """

    code: str
    detail: str
    hard: bool = False


class CandidateIn(BaseModel):
    """A durable fact a worker thinks is worth keeping. A proposal, never a fact: it goes to
    `candidate_memories` and the review gate decides."""

    statement: str
    confidence: float = 0.6
    category: str = "other"
    evidence: list[dict] = Field(default_factory=list)


class WorkerReport(BaseModel):
    """The schema a worker is required to return, and the one sent for guided decoding.

    `status` and `answer` have no defaults on purpose. A default here is this codebase's
    recurring bug: a model that emitted nothing would produce a fully-formed report saying
    "completed" with an empty answer, and there would be no way left to tell that apart from
    a worker that genuinely completed something and said so badly.

    There is no cross-field validator. See the module docstring: reconciliation is
    `validate_report`'s job, after the decode, where a disagreement can be recorded instead
    of raising.
    """

    status: Literal["completed", "blocked", "uncertain"]
    answer: str
    evidence: list[str] = Field(default_factory=list)
    actions_taken: list[str] = Field(default_factory=list)
    followups: list[str] = Field(default_factory=list)
    candidate_memories: list[CandidateIn] = Field(default_factory=list)


@dataclass(frozen=True)
class WorkerResult:
    """What the runtime concluded about one worker's work.

    `report_valid` is a fact about the worker's report and not about its work: False means
    the runtime never got a readable result, which is why the status is then `uncertain` and
    not `blocked`.
    """

    status: str
    answer: str
    evidence: tuple[str, ...] = ()
    actions_taken: tuple[str, ...] = ()
    followups: tuple[str, ...] = ()
    candidate_memories: tuple[CandidateIn, ...] = ()
    tainted: bool = False
    report_valid: bool = True
    notes: tuple[str, ...] = ()
    # Runtime-only, all three. `report_error` is a decoder's complaint and quotes the
    # model's malformed output; `transcript` is the worker's prose in full.
    report_error: str = ""
    transcript: str = field(default="", repr=False)
    # Session 9a. What the runtime could corroborate, which is a different axis from
    # `status`: `status` is what the worker says happened, `validation` is what its own
    # journal says about that claim. A `completed` result may be `invalidated`; the worker
    # still ran, and what it did is still on the record - what is refused is the claim.
    validation: str = "valid"
    flags: tuple[Flag, ...] = ()
    # Session 6c. The worker whose run earned this result, when it was served from this
    # run's result cache instead of being re-run; "" when a worker produced it just now.
    # It exists so that an empty `transcript` on a reused result is explained rather than
    # read as a worker that said nothing - the transcript is not cached (see
    # `agent/result_cache.py`), and a field that says why is cheaper than a copy of it.
    reused_from: str = ""

    def __post_init__(self) -> None:
        if self.status not in STATUSES:
            raise ValueError(
                f"{self.status!r} is not a worker status; this runtime has {', '.join(STATUSES)}"
            )
        if self.validation not in VALIDATIONS:
            raise ValueError(
                f"{self.validation!r} is not a validation status; this runtime has "
                f"{', '.join(VALIDATIONS)}"
            )

    def for_orchestrator(self, *, debug: bool = False) -> dict[str, Any]:
        """The result as the orchestrator may see it.

        The one door. A worker's transcript never enters orchestrator context outside an
        explicit debug mode, and this is where that rule is kept for the delegating model -
        `db.repo_archive.recent_messages` keeps it for the next turn's history.
        """
        payload: dict[str, Any] = {
            "status": self.status,
            "answer": self.answer,
            "evidence": list(self.evidence),
            "actions_taken": list(self.actions_taken),
            "followups": list(self.followups),
        }
        if self.notes:
            payload["notes"] = list(self.notes)
        # Always present, even when it is "valid": a caller that has to infer a missing key
        # as "nothing was checked" is the absent-means-null bug this codebase keeps having.
        payload["validation"] = self.validation
        if self.flags:
            payload["validation_flags"] = [f.detail for f in self.flags]
        if debug:
            payload["report_valid"] = self.report_valid
            payload["report_error"] = self.report_error
            payload["transcript"] = self.transcript
            payload["reused_from"] = self.reused_from
        return payload


def _lines(values: list[str]) -> tuple[str, ...]:
    """List entries, stripped, with the empty ones dropped.

    A model that pads its arrays with "" is saying nothing three times; counting those as
    evidence would make an empty citation list look like a cited one.
    """
    return tuple(text for text in (str(v).strip() for v in values) if text)


def validate_report(
    report: WorkerReport,
    *,
    budget_exhausted: bool = False,
    tainted: bool = False,
    transcript: str = "",
) -> WorkerResult:
    """A decoded report, checked against what the runtime watched the worker do.

    Two rules, and both of them can only move a status towards `uncertain`:

    1. A report with no answer in it is not a report. It decoded, so the worker is not
       ignoring the schema; it filled the schema in with nothing, which tells the caller
       exactly as much as prose would.
    2. A worker that ran out of steps did not finish, whatever it says. `agent/loop.py`
       forces the last step tool-free with a nudge to wrap up, so the report it writes is a
       summary of unfinished work - which is a true thing to have, under a status that says
       so.

    Nothing here checks that a `completed` cited anything. That is a judgement about the
    quality of the work rather than about the shape of the result, and a rule that turned
    every uncited answer into a failure would be a rule the local model trips on constantly
    and everyone learns to ignore.
    """
    answer = report.answer.strip()
    notes: list[str] = []
    status = report.status
    valid = True

    if not answer:
        status = "uncertain"
        valid = False
        notes.append(NOTE_NO_ANSWER)
    if budget_exhausted and status != "uncertain":
        # Forced, not merely applied to `completed`: a worker that was cut off mid-run may
        # already have changed things, so "blocked" - which means nothing happened, and
        # invites a retry - is the one word it must not keep.
        status = "uncertain"
        notes.append(NOTE_BUDGET)

    return WorkerResult(
        status=status,
        answer=answer,
        evidence=_lines(report.evidence),
        actions_taken=_lines(report.actions_taken),
        followups=_lines(report.followups),
        candidate_memories=tuple(report.candidate_memories),
        tainted=tainted,
        report_valid=valid,
        notes=tuple(notes),
        transcript=transcript,
    )


def unreadable_report(
    error: str, *, tainted: bool = False, transcript: str = "", budget_exhausted: bool = False
) -> WorkerResult:
    """The worker never produced a result: it returned prose, or unparseable JSON, or the
    final call raised.

    A failure, not a degraded success, and the difference is visible from the caller: the
    status is `uncertain`, `report_valid` is False, and the answer is a sentence this module
    wrote rather than the first 1000 characters of the transcript. Pasting the transcript
    into the answer - which is what this runtime used to do - reads as a worker that reported
    something, in the one situation where nothing was reported, and smuggles the raw
    transcript into orchestrator context besides.
    """
    notes = [NOTE_UNREADABLE]
    if budget_exhausted:
        notes.append(NOTE_BUDGET)
    return WorkerResult(
        status="uncertain",
        answer=(
            "No result: this worker did not return the result schema, so what it did and "
            "what it found are not known here. Its transcript is in the archive."
        ),
        tainted=tainted,
        report_valid=False,
        notes=tuple(notes),
        report_error=error,
        transcript=transcript,
    )
