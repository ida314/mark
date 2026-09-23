"""What a worker hands back, and what happens when it hands back something else.

A delegation is the one place in this runtime where a whole piece of work arrives as a
single value, and the expensive failure is not a worker that fails - it is a worker that
fails and produces something the caller reads as an answer anyway. That is the shape this
codebase keeps shipping: a value that degrades into a plausible one. Before session 6b, a
worker whose final report did not decode got `status="failed"` with its own raw transcript
pasted into the summary field, which reads to the orchestrator as a report, and puts the
transcript into orchestrator context while it is at it.

So the tests below pin three things:

* a report that is not the schema is `uncertain` with `report_valid=False`, and the answer
  is a sentence the runtime wrote - never the transcript;
* a malformed or cut-off result is never `blocked`, because Pass 4c's `blocked` means the
  work did not happen and puts `retry` first, and re-running a worker that already wrote
  files is the duplicate this vocabulary exists to prevent;
* the transcript is kept by the runtime and reaches the orchestrator through exactly one
  door, which is shut unless `[delegation] debug_transcripts` is on - both for the tool
  result the delegating step sees and for the history the next turn is rebuilt from.
"""

from __future__ import annotations

from typing import get_args

import pytest

from agentd.agent.context import history_messages
from agentd.agent.delegation import TaskSpec
from agentd.agent.loop import Session
from agentd.agent.results import (
    NOTE_BUDGET,
    NOTE_NO_ANSWER,
    STATUSES,
    WorkerReport,
    WorkerResult,
    unreadable_report,
    validate_report,
)
from agentd.agent.subagents import SubagentSpec, run_subagent
from agentd.db import repo_archive
from agentd.journal.events import WORKER_STATUSES
from agentd.llm.base import LLMError
from agentd.llm.fake import FakeProvider
from agentd.llm.roles import set_provider
from agentd.policy.approvals import AutoApprover
from agentd.tools.base import ToolContext
from agentd.tools.registry import build_registry

TRANSCRIPT = "I read the lease and the renewal clause is on page four."


def _spec(**kwargs) -> SubagentSpec:
    defaults = dict(name="researcher", prompt="be useful", tool_names=["fs_read"], max_steps=3)
    defaults.update(kwargs)
    return SubagentSpec(**defaults)


class NoJson(FakeProvider):
    """A provider whose final call fails the way the real one does.

    `openai_compat.complete_json` validates, shows the model its own error, tries once more
    and then raises `LLMError`. Nothing downstream ever sees the prose that caused it.
    """

    async def complete_json(self, messages, schema, *, params):
        raise LLMError("fake: could not get valid JSON: 1 validation error for WorkerReport")


class RaisesValidation(FakeProvider):
    """A provider that validates locally and lets pydantic's error out, rather than an
    `LLMError`. Different exception, same situation: there is no result."""

    async def complete_json(self, messages, schema, *, params):
        return schema.model_validate_json("I could not find it, sorry.")


async def _run(cfg, provider, **kwargs) -> tuple[WorkerResult, Session]:
    session = await Session.create("test")
    result = await run_subagent(
        _spec(**kwargs.pop("spec", {})),
        TaskSpec("researcher", "find the renewal clause"),
        parent_session_id=session.id, parent_turn_id=session.id, parent_autonomy="assist",
        approver=AutoApprover(True), registry=build_registry(), cfg=cfg, provider=provider,
        **kwargs,
    )
    return result, session


# --- a report that is not the schema -----------------------------------------


@pytest.mark.parametrize("provider_cls", [NoJson, RaisesValidation])
async def test_a_worker_that_does_not_return_the_schema_is_a_failure(cfg, provider_cls):
    """Not a degraded success. The caller can tell, without reading the answer, that this
    worker reported nothing: `report_valid` says so on its own."""
    result, _ = await _run(cfg, provider_cls(turns=[TRANSCRIPT]))
    assert result.status == "uncertain"
    assert result.report_valid is False
    assert result.evidence == () and result.actions_taken == ()


@pytest.mark.parametrize("provider_cls", [NoJson, RaisesValidation])
async def test_a_worker_that_does_not_return_the_schema_is_never_blocked(cfg, provider_cls):
    """`blocked` means the work did not happen, and `paths_for(BLOCKED)` puts `retry` first.
    A worker whose report came back unreadable may already have written files, so calling it
    blocked authorises re-running whatever it did."""
    result, _ = await _run(cfg, provider_cls(turns=[TRANSCRIPT]))
    assert result.status != "blocked"


async def test_an_unreadable_report_does_not_borrow_the_transcript_for_an_answer(cfg):
    """The old behaviour: `summary=f"...Raw output: {text[:1000]}"`. It made a failure look
    like a report and leaked the transcript in the same line."""
    result, session = await _run(cfg, NoJson(turns=[TRANSCRIPT]))
    assert "renewal clause is on page four" not in result.answer
    assert "No result" in result.answer

    # Kept, though: the work is not lost, it is just not being passed off as a result. The
    # worker's own turn archived it, which is why there is no third copy of it here.
    events = await repo_archive.events_for_session(session.id)
    prose = [
        e for e in events
        if e["actor"] == "subagent:researcher" and (e["content"] or "").strip() == TRANSCRIPT
    ]
    assert prose, "the worker's transcript is not in the archive"


async def test_the_decoders_complaint_is_kept_out_of_what_the_orchestrator_sees(cfg):
    """A `ValidationError` string quotes the input that failed, which is the model's own
    malformed output. It is diagnostic, so it is recorded - in the archive and on the
    result - and it is not in the payload a model is shown."""
    result, session = await _run(cfg, NoJson(turns=[TRANSCRIPT]))
    assert "validation error" in result.report_error
    assert "report_error" not in result.for_orchestrator()
    assert result.for_orchestrator(debug=True)["report_error"] == result.report_error

    archived = [
        e for e in await repo_archive.events_for_session(session.id)
        if e["kind"] == "subagent_result"
    ]
    assert archived[0]["payload"]["report_valid"] is False
    assert "validation error" in archived[0]["payload"]["report_error"]


async def test_the_journal_says_whether_the_worker_reported_at_all(cfg, journaled):
    """`status` cannot carry it: an unreadable report is `uncertain`, and so is a worker
    that reported honestly that it could not vouch for its own work."""
    await _run(cfg, NoJson(turns=[TRANSCRIPT]), parent_run_id="run-a")
    await _run(
        cfg,
        FakeProvider(
            turns=["found it"],
            json_results=[WorkerReport(status="uncertain", answer="I am not sure it applies.")],
        ),
        parent_run_id="run-b",
    )
    by_run = {e.run_id: e.payload for e in journaled("worker_finished")}
    assert by_run["run-a"]["status"] == "uncertain"
    assert by_run["run-a"]["report_valid"] is False
    assert by_run["run-b"]["status"] == "uncertain"
    assert by_run["run-b"]["report_valid"] is True


# --- the schema filled in with nothing ---------------------------------------


async def test_a_report_with_no_answer_in_it_is_not_a_completed_one(cfg):
    """It decoded, so the worker is not ignoring the schema - it filled the schema in with
    nothing, which tells the caller exactly as much as prose would."""
    provider = FakeProvider(
        turns=[TRANSCRIPT], json_results=[WorkerReport(status="completed", answer="   ")]
    )
    result, _ = await _run(cfg, provider)
    assert result.status == "uncertain"
    assert result.report_valid is False
    assert NOTE_NO_ANSWER in result.notes


def test_an_empty_evidence_entry_is_not_evidence():
    """A model that pads its arrays with blanks is saying nothing three times; counting
    those would make an uncited answer look cited."""
    result = validate_report(
        WorkerReport(
            status="completed", answer="done", evidence=["", "  ", "file://a"],
            followups=[" tidy up "],
        )
    )
    assert result.evidence == ("file://a",)
    assert result.followups == ("tidy up",)
    assert result.report_valid is True


# --- the runtime is allowed to disagree with the worker ----------------------


def test_a_worker_that_ran_out_of_steps_cannot_call_itself_completed():
    """The last step is forced tool-free with a nudge to wrap up, so what comes back is a
    summary of unfinished work. Recording that as `completed` is the pass's whole failure
    mode in one field."""
    result = validate_report(
        WorkerReport(status="completed", answer="I got halfway."), budget_exhausted=True
    )
    assert result.status == "uncertain"
    assert NOTE_BUDGET in result.notes
    # The work it did report is still there. This is a re-reading, not a discard.
    assert result.answer == "I got halfway."


def test_a_worker_that_ran_out_of_steps_cannot_call_itself_blocked_either():
    """Same reason as an unreadable report: it may already have changed something, and
    `blocked` is the word that invites running it again."""
    result = validate_report(
        WorkerReport(status="blocked", answer="I could not get to it."), budget_exhausted=True
    )
    assert result.status == "uncertain"


def test_the_runtime_never_upgrades_a_worker_that_reported_honestly():
    """Reconciliation only ever moves towards `uncertain`. A worker that says it was blocked
    stays blocked; nothing here can turn a worker's own bad news into good news."""
    for status in ("completed", "blocked", "uncertain"):
        result = validate_report(WorkerReport(status=status, answer="said something"))
        assert result.status == status


def test_a_status_this_runtime_does_not_have_is_refused_rather_than_stored():
    with pytest.raises(ValueError) as exc:
        WorkerResult(status="ok", answer="done")
    assert "not a worker status" in str(exc.value)


def test_the_result_schema_and_the_journal_agree_about_a_worker_status():
    """`WorkerReport.status` has to spell the three out - a `Literal` cannot read a tuple -
    so this is what stops the enum the journal enforces from drifting from the words the
    runtime can produce."""
    assert get_args(WorkerReport.model_fields["status"].annotation) == WORKER_STATUSES
    assert STATUSES == WORKER_STATUSES


# --- transcripts stay in the runtime -----------------------------------------


async def _delegate_through_the_tool(cfg, provider) -> tuple[object, Session]:
    from agentd.tools import builtin_delegate

    set_provider(provider)
    session = await Session.create("test")
    ctx = ToolContext(
        session_id=session.id, turn_id=session.id, autonomy="assist", run_id="run-tool",
        step_id="s1", extra={"approver": AutoApprover(True)},
    )
    result = await builtin_delegate.delegate.handler(
        {"agent": "researcher", "task": "find the renewal clause"}, ctx
    )
    return result, session


async def test_a_workers_transcript_does_not_come_back_through_the_delegate_tool(cfg):
    provider = FakeProvider(
        turns=[TRANSCRIPT],
        json_results=[WorkerReport(status="completed", answer="Page four.", evidence=["lease"])],
    )
    result, _ = await _delegate_through_the_tool(cfg, provider)
    assert result.ok
    assert "Page four." in result.content
    assert "renewal clause is on page four" not in result.content


async def test_debug_mode_is_the_one_way_a_transcript_reaches_the_delegating_model(cfg):
    """The rule is "not outside an explicit debug mode", so the mode has to actually do
    something - a flag nothing reads is the same as no rule at all."""
    cfg.delegation.debug_transcripts = True
    provider = FakeProvider(
        turns=[TRANSCRIPT], json_results=[WorkerReport(status="completed", answer="Page four.")]
    )
    result, _ = await _delegate_through_the_tool(cfg, provider)
    assert "renewal clause is on page four" in result.content


async def test_a_workers_prose_does_not_reach_the_next_turns_history(cfg):
    """A worker runs inside its caller's session, so its final prose is archived as an
    `assistant_message` on that session like any other. Without the actor clause in
    `recent_messages`, the orchestrator's next turn rebuilds its history out of rows a
    worker wrote - the same rule broken in the place nobody would look."""
    provider = FakeProvider(
        turns=[TRANSCRIPT], json_results=[WorkerReport(status="completed", answer="Page four.")]
    )
    _, session = await _delegate_through_the_tool(cfg, provider)
    history = await history_messages(session.id, budget_tokens=10_000)
    assert all("renewal clause is on page four" not in m["content"] for m in history)


async def test_a_worker_that_is_not_completed_is_not_a_successful_tool_call(cfg):
    """`blocked` and `uncertain` are both `ok=False`. The executor writes that as a failed
    effect, and `agent/observations.py` reads a failed effect as uncertain rather than as
    blocked, so nothing offers to run the delegation again on its own."""
    provider = FakeProvider(
        turns=[TRANSCRIPT],
        json_results=[WorkerReport(status="blocked", answer="The file is not readable.")],
    )
    result, _ = await _delegate_through_the_tool(cfg, provider)
    assert not result.ok
    assert result.data["status"] == "blocked"
    assert result.data["report_valid"] is True


def test_an_unreadable_report_carries_its_transcript_without_showing_it():
    """`for_orchestrator` is the only door, so the transcript can ride on the result for the
    archive and the debug path without being one `json.dumps(result)` away from a prompt."""
    result = unreadable_report("LLMError: nope", transcript=TRANSCRIPT)
    assert result.transcript == TRANSCRIPT
    assert TRANSCRIPT not in str(result.for_orchestrator())
    assert TRANSCRIPT in str(result.for_orchestrator(debug=True))
