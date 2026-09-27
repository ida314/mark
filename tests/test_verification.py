"""Session 9a. A worker's report, checked against the journal that worker wrote.

Every check here is built from a shape that was measured, not invented: the row it came from
is named in the test's docstring. The ledger half is driven by synthetic events, because the
point of `ledger_from_events` is that it is a fold over the journal and needs no runtime to
exercise; the end-to-end half drives a real `run_subagent` with a scripted provider, because
the thing that has to hold is that the events a worker really writes produce the same reading.
"""

from __future__ import annotations

import pytest

from agentd.agent.delegation import TaskSpec
from agentd.agent.loop import Session
from agentd.agent.results import Flag, WorkerResult
from agentd.agent.subagents import SubagentSpec, run_subagent
from agentd.agent.verification import (
    ledger_from_events,
    validation_of,
    verify,
)
from agentd.journal.store import Event
from agentd.llm.fake import FakeProvider
from agentd.policy.approvals import AutoApprover
from agentd.tools.registry import build_registry

# --- synthetic events --------------------------------------------------------

PATHS = {"fs_read": ("path",), "fs_write": ("path",), "fs_list": ("path",), "fs_search": ("path",)}


def _ev(seq: int, type_: str, payload: dict) -> Event:
    return Event(id=seq, run_id="run-1", seq=seq, ts="2026-09-25T00:00:00Z", type=type_, payload=payload)


def _call(seq: int, name: str, args: dict, *, ok: bool = True, summary: str = "", denied: bool = False):
    """The two events the journal writes for one call, as the loop writes them."""
    yield _ev(seq, "tool_requested", {"call_id": f"c{seq}", "name": name, "args": args,
                                      "visible": True, "known": True})
    if ok:
        yield _ev(seq + 1, "tool_finished", {"call_id": f"c{seq}", "name": name, "duration_ms": 1,
                                             "result_chars": 10, "trust": "trusted",
                                             "summary": summary})
    else:
        yield _ev(seq + 1, "tool_failed", {"call_id": f"c{seq}", "name": name, "duration_ms": 1,
                                           "error": summary or "boom", "denied": denied,
                                           "invalid_args": False})


def _finished(seq: int, status: str = "completed", steps: int = 3) -> Event:
    return _ev(seq, "agent_finished", {"turn_id": "t", "status": status, "steps": steps,
                                       "duration_ms": 1, "answer_chars": 1, "usage": {},
                                       "usage_reported": False, "context_tokens": 1,
                                       "context_ceiling_tokens": 2, "context_threshold_tokens": 1,
                                       "context_crossed": False, "context_basis": "estimate"})


def _ledger(*events: Event):
    return ledger_from_events(events, path_args=PATHS)


def _report(**kwargs) -> WorkerResult:
    fields = dict(status="completed", answer="Done.")
    fields.update(kwargs)
    return WorkerResult(**fields)


# --- the ledger --------------------------------------------------------------


def test_the_ledger_joins_a_call_to_how_it_ended() -> None:
    ledger = _ledger(
        *_call(1, "fs_read", {"path": "/repo/loop.py"}),
        *_call(3, "shell_exec", {"command": "pytest -q"}, ok=False, summary="exit=1\n2 failed"),
    )
    assert [c.name for c in ledger.calls] == ["fs_read", "shell_exec"]
    assert ledger.calls[0].ok is True
    assert ledger.calls[1].ok is False
    assert ledger.failures == 1
    assert ledger.files_touched == frozenset({"/repo/loop.py"})


def test_a_call_that_never_terminated_is_neither_a_success_nor_a_failure() -> None:
    """A process killed mid-call leaves a `tool_requested` with nothing after it. Reading
    that as success is how a crash comes to look like work that was done."""
    ledger = _ledger(_ev(1, "tool_requested", {"call_id": "c1", "name": "shell_exec",
                                               "args": {"command": "pytest"}, "visible": True,
                                               "known": True}))
    assert ledger.calls[0].ok is None
    assert ledger.failures == 0


def test_the_last_shell_exit_code_is_read_and_a_timeout_has_none() -> None:
    """`shell_exec` writes `exit=N` as the first line. A timed-out command writes no such
    line at all, and an absent exit code must not read as zero."""
    ran = _ledger(
        *_call(1, "shell_exec", {"command": "pytest"}, ok=False, summary="exit=2\nboom"),
        *_call(3, "shell_exec", {"command": "ls"}, summary="exit=0\nfiles"),
    )
    assert ran.last_shell_exit == 0
    killed = _ledger(
        *_call(1, "shell_exec", {"command": "sleep 999"}, ok=False,
               summary="Command timed out after 120s and was killed."),
    )
    assert killed.last_shell_exit is None


def test_a_denial_is_counted_as_a_denial_and_not_only_as_a_failure() -> None:
    ledger = _ledger(*_call(1, "web_fetch", {"url": "https://x.test"}, ok=False,
                            summary='{"denied": true}', denied=True))
    assert ledger.failures == 1 and ledger.denials == 1


def test_one_workers_events_are_read_out_of_a_run_that_holds_two() -> None:
    """A worker's events live in its caller's run, tagged. Narrowing is the caller's job."""
    mine = [{**e.payload, "worker_id": "w-1"} for e in _call(1, "fs_read", {"path": "/a.py"})]
    theirs = [{**e.payload, "worker_id": "w-2"} for e in _call(3, "fs_read", {"path": "/b.py"})]
    events = [
        _ev(1, "tool_requested", mine[0]), _ev(2, "tool_finished", mine[1]),
        _ev(3, "tool_requested", theirs[0]), _ev(4, "tool_finished", theirs[1]),
    ]
    ledger = ledger_from_events(events, worker_id="w-1", path_args=PATHS)
    assert ledger.files_touched == frozenset({"/a.py"})


# --- the checks --------------------------------------------------------------


def test_a_green_test_run_claimed_with_no_test_run_is_invalidated() -> None:
    """B12. The worker reported "all 19 tests pass"; 2 of 19 failed, disproved by re-running
    the same fixture on the same commit. The journal already said so: the only shell command
    it ran exited 1."""
    ledger = _ledger(
        *_call(1, "fs_read", {"path": "/repo/parser.py"}),
        *_call(3, "shell_exec", {"command": "uv run pytest -q"}, ok=False, summary="exit=1\n2 failed"),
        _finished(5),
    )
    flags = verify(_report(answer="Fixed the parser; all 19 tests pass."), ledger)
    assert [f.code for f in flags] == ["tests_not_run"]
    assert "exited 1" in flags[0].detail
    assert validation_of(flags) == "invalidated"


def test_a_test_run_that_really_happened_is_not_flagged() -> None:
    ledger = _ledger(
        *_call(1, "shell_exec", {"command": "uv run pytest -q"}, summary="exit=0\n19 passed"),
        _finished(3),
    )
    assert verify(_report(answer="All 19 tests pass."), ledger) == ()


def test_reading_the_test_file_is_not_running_the_tests() -> None:
    """The check is keyed on a test *runner* in a command, not on the word "test": a worker
    that reads `tests/test_parser.py` and reports a green suite is the B12 shape exactly."""
    ledger = _ledger(*_call(1, "shell_exec", {"command": "cat tests/test_parser.py"},
                            summary="exit=0\nimport pytest"), _finished(3))
    flags = verify(_report(answer="The tests pass."), ledger)
    assert [f.code for f in flags] == ["tests_not_run"]


def test_a_file_cited_as_evidence_that_was_never_opened_is_invalidated() -> None:
    """B10. Eleven correct `shell_exec` calls found the real files; the report then cited
    three paths that do not exist."""
    ledger = _ledger(*_call(1, "fs_read", {"path": "/repo/src/agentd/agent/loop.py"}), _finished(3))
    flags = verify(
        _report(answer="Found it.", evidence=("src/agentd/agent/loop.py", "src/agentd/core/turn.py")),
        ledger,
    )
    assert [f.code for f in flags] == ["file_not_read"]
    assert "src/agentd/core/turn.py" in flags[0].detail
    # and the one it really read is not named
    assert "agent/loop.py" not in flags[0].detail


def test_a_path_the_brief_named_is_grounded_even_if_the_worker_never_opened_it() -> None:
    """The caller told the worker about it. Citing it back is not a fabrication, and a check
    that fired here would fire on every delegation that names a file."""
    ledger = _ledger(_finished(1, steps=1))
    flags = verify(
        _report(answer="Looks fine.", evidence=("config/policy.default.yaml",)),
        ledger,
        brief="Check config/policy.default.yaml for the daemon rule.",
    )
    assert "file_not_read" not in [f.code for f in flags]


def test_a_path_found_through_the_shell_is_grounded() -> None:
    """`shell_exec` has no path argument - the path is inside a command line - so a worker
    that works through the shell must not be flagged for citing what it grepped."""
    ledger = _ledger(*_call(1, "shell_exec", {"command": "rg -n delegate src/agentd/agent/loop.py"},
                            summary="exit=0\n42:delegate"), _finished(3))
    flags = verify(_report(answer="line 42", evidence=("src/agentd/agent/loop.py:42",)), ledger)
    assert flags == ()


def test_a_completed_report_from_a_turn_that_failed_is_invalidated() -> None:
    """B11, generalized: the worker's own turn and its report disagree, and the report is the
    only one of the two the orchestrator would otherwise see."""
    ledger = _ledger(*_call(1, "fs_read", {"path": "/a.py"}), _finished(3, status="failed"))
    flags = verify(_report(answer="Done."), ledger)
    assert [f.code for f in flags] == ["status_conflict"]
    assert validation_of(flags) == "invalidated"


def test_an_abandoned_turn_is_not_flagged_twice() -> None:
    """`results.validate_report` already forces a budget-exhausted worker to `uncertain`, so
    flagging it here as well would double-count one fact and report two contradictions."""
    ledger = _ledger(*_call(1, "fs_read", {"path": "/a.py"}), _finished(3, status="abandoned"))
    assert "status_conflict" not in [f.code for f in verify(_report(), ledger)]


def test_could_not_access_it_from_a_worker_that_never_tried_is_flagged() -> None:
    """B16. A researcher reported "no web access from that context" having made zero calls,
    in a run where the same role made ten successful web calls minutes earlier - and the
    orchestrator relayed it and changed its plan."""
    ledger = _ledger(_finished(1, steps=1))
    flags = verify(_report(status="blocked", answer="I couldn't access the docs from here."), ledger)
    assert [f.code for f in flags] == ["no_attempt"]
    assert validation_of(flags) == "uncertain"


def test_a_completion_with_no_tool_calls_at_all_is_flagged() -> None:
    """8d's four rows: tool-call markup emitted as prose, `status=completed`, `steps=1`, and
    nothing in the runtime noticed."""
    flags = verify(_report(answer="I read the file and it is fine."), _ledger(_finished(1, steps=1)))
    assert [f.code for f in flags] == ["completed_without_tools"]
    assert validation_of(flags) == "uncertain"


def test_a_claimed_edit_with_nothing_written_is_flagged() -> None:
    ledger = _ledger(*_call(1, "fs_read", {"path": "/a.py"}), _finished(3))
    flags = verify(_report(answer="Done.", actions_taken=("edited /a.py to add the guard",)), ledger)
    assert [f.code for f in flags] == ["write_not_performed"]


def test_a_claimed_edit_with_a_write_behind_it_is_not_flagged() -> None:
    ledger = _ledger(*_call(1, "fs_write", {"path": "/a.py"}), _finished(3))
    assert verify(_report(answer="Done.", actions_taken=("edited /a.py",)), ledger) == ()


def test_a_report_the_runtime_could_not_read_is_not_checked() -> None:
    """When `report_valid` is False the answer is a sentence `results.py` wrote. Running the
    text checks over it would be the verifier flagging its own prose."""
    ledger = _ledger(_finished(1, steps=1))
    unreadable = WorkerResult(status="uncertain", answer="No result: ...", report_valid=False)
    assert verify(unreadable, ledger) == ()


def test_a_blocked_worker_is_not_asked_to_prove_a_completion() -> None:
    """Most checks are about a claim of success. A worker that says it got nowhere is making
    a much weaker claim, and only the one check about *not trying* applies to it."""
    ledger = _ledger(*_call(1, "fs_read", {"path": "/a.py"}), _finished(3))
    assert verify(_report(status="blocked", answer="The file is not in this repo."), ledger) == ()


@pytest.mark.parametrize(
    ("flags", "expected"),
    [
        ((), "valid"),
        ((Flag("no_attempt", "d", hard=False),), "uncertain"),
        ((Flag("no_attempt", "d", hard=False), Flag("tests_not_run", "d", hard=True)), "invalidated"),
    ],
)
def test_one_hard_flag_invalidates_and_soft_flags_only_doubt(flags, expected) -> None:
    assert validation_of(flags) == expected


# --- end to end, through a real worker ---------------------------------------


def _worker_spec(**kwargs) -> SubagentSpec:
    defaults = dict(name="coder", prompt="be useful", tool_names=["fs_read"], max_steps=3)
    defaults.update(kwargs)
    return SubagentSpec(**defaults)


async def _run_worker(cfg, provider, task: str = "fix the parser") -> WorkerResult:
    session = await Session.create("test")
    return await run_subagent(
        _worker_spec(), TaskSpec("coder", task), parent_session_id=session.id,
        parent_turn_id=session.id, parent_autonomy="act", approver=AutoApprover(True),
        registry=build_registry(), cfg=cfg, provider=provider,
    )


async def test_a_worker_that_claims_a_green_suite_it_never_ran_comes_back_invalidated(
    cfg, journaled
) -> None:
    """The whole path, from the events a real worker writes to what its caller is handed."""
    from agentd.agent.results import WorkerReport

    target = cfg.paths.roots()[0] / "parser.py"
    target.write_text("x")
    provider = FakeProvider(
        turns=[[("fs_read", {"path": str(target)})], "I fixed it and ran the tests."],
        json_results=[WorkerReport(status="completed", answer="Fixed. All 19 tests pass.")],
    )
    result = await _run_worker(cfg, provider)

    assert result.status == "completed"
    assert result.validation == "invalidated"
    assert [f.code for f in result.flags] == ["tests_not_run"]

    verified = journaled("worker_verified")
    assert len(verified) == 1
    payload = verified[0].payload
    assert payload["validation"] == "invalidated"
    assert payload["flags"] == ["tests_not_run"]
    assert payload["tool_calls"] == 1 and payload["shell_runs"] == 0
    assert payload["last_shell_exit"] is None
    # ...and the finish carries the same conclusion, for a fold that reads finishes alone.
    assert journaled("worker_finished")[0].payload["validation"] == "invalidated"


async def test_the_verification_is_journaled_before_the_finish_it_concludes(cfg, journaled) -> None:
    """A crash between the two leaves the evidence rather than the verdict."""
    from agentd.agent.results import WorkerReport

    provider = FakeProvider(
        turns=["done"], json_results=[WorkerReport(status="completed", answer="ok")]
    )
    await _run_worker(cfg, provider)
    types = [e.type for e in journaled("worker_verified", "worker_finished")]
    assert types == ["worker_verified", "worker_finished"]


async def test_a_clean_worker_is_verified_too_and_says_so(cfg, journaled) -> None:
    """"Checked and clean" and "never checked" are different facts. A type only written on a
    failure could not tell them apart."""
    from agentd.agent.results import WorkerReport

    target = cfg.paths.roots()[0] / "note.txt"
    target.write_text("42")
    provider = FakeProvider(
        turns=[[("fs_read", {"path": str(target)})], "It says 42."],
        json_results=[
            WorkerReport(status="completed", answer="It says 42.", evidence=[str(target)])
        ],
    )
    result = await _run_worker(cfg, provider, task="what does the note say")
    assert result.validation == "valid" and result.flags == ()
    assert journaled("worker_verified")[0].payload["flags"] == []
