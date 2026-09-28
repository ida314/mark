"""A worker that hit the approval wall comes back with the ids, not just the story.

Session 01a0e9f6: the coder's `shell_exec` and `fs_write` were both queued, the worker's
report said "approve the fs_write", and the orchestrator told the user to approve something it
had no id for. The ids were in the worker's journal the whole time.
"""

from __future__ import annotations

from agentd.agent.results import WorkerResult
from agentd.agent.verification import ledger_from_events
from agentd.journal.store import Event


def _ev(seq: int, type_: str, payload: dict) -> Event:
    return Event(id=seq, run_id="run-1", seq=seq, ts="2026-09-28T00:00:00Z", type=type_, payload=payload)


def _queued(seq: int, name: str, approval_id: str) -> list[Event]:
    return [
        _ev(seq, "tool_requested", {"call_id": f"c{seq}", "name": name, "args": {"path": "x"},
                                    "visible": True, "known": True}),
        _ev(seq + 1, "tool_failed", {"call_id": f"c{seq}", "name": name, "duration_ms": 1,
                                     "error": "queued", "denied": True, "invalid_args": False,
                                     "attempt": 0, "rule": "risk_matrix:write/assist",
                                     "queued_id": approval_id}),
    ]


def test_the_ledger_carries_every_queued_approval_once_in_call_order() -> None:
    events = [
        *_queued(1, "shell_exec", "01a0e9f9-72c6-77a5-8d27-f2e57f650fe9"),
        *_queued(3, "fs_write", "01a0e9f9-8ad2-726a-b15f-c4bbd6b063ac"),
        *_queued(5, "fs_write", "01a0e9f9-8ad2-726a-b15f-c4bbd6b063ac"),
        _ev(7, "tool_requested", {"call_id": "c7", "name": "fs_read", "args": {"path": "x"},
                                  "visible": True, "known": True}),
        _ev(8, "tool_failed", {"call_id": "c7", "name": "fs_read", "duration_ms": 1,
                               "error": "No such file", "denied": False, "invalid_args": False,
                               "attempt": 0, "rule": None, "queued_id": None}),
    ]
    ledger = ledger_from_events(events)

    assert ledger.queued_approvals == (
        "01a0e9f9-72c6-77a5-8d27-f2e57f650fe9",
        "01a0e9f9-8ad2-726a-b15f-c4bbd6b063ac",
    )
    assert ledger.denials == 3 and ledger.failures == 4


def test_the_orchestrator_is_shown_the_ids_and_only_when_there_are_some() -> None:
    parked = WorkerResult(status="blocked", answer="waiting on you", queued_approvals=("id-1",))
    assert parked.for_orchestrator()["queued_approvals"] == ["id-1"]

    clean = WorkerResult(status="completed", answer="done")
    assert "queued_approvals" not in clean.for_orchestrator()
