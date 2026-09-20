"""Before the agent does something it cannot take back, there is a record that it is about to.

That is the whole of the effect ledger, and the ordering is the substance of it. A row
written after the call has already gone out answers nothing: the crash that matters happens
*during* the call, and the question it leaves behind - "did that email go?" - is answerable
only if the record was on disk first.

So the tests here are mostly about order and about what survives. The row precedes the
dispatch; the journal event precedes the row; a killed process leaves the row at `started`,
which is what tells a later resume that an `unsafe_write` may have happened and must never
be retried automatically.

The ledger deliberately does *not* prevent anything yet - a second attempt at the same call
is recorded and still runs. Reconciliation is Pass 4, and this pass's "must not" says so.
"""

from __future__ import annotations

import json
import os
import signal
import sqlite3
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from agentd.journal import ledger as ledger_mod
from agentd.journal.ledger import EffectLedger, EffectStateError, get_ledger, ledgered
from agentd.journal.store import JournalStore
from agentd.journal.writer import JournalWriter
from agentd.policy.approvals import AutoApprover
from agentd.policy.engine import engine_from_config
from agentd.tools.base import Tool, ToolContext, ToolResult, obj
from agentd.tools.executor import ToolExecutor
from agentd.tools.registry import Registry


def _executor(cfg, *tools: Tool, approve: bool = True) -> ToolExecutor:
    registry = Registry()
    registry.add(*tools)
    return ToolExecutor(registry.tools, engine_from_config(cfg), AutoApprover(approve))


def _ctx(**kwargs) -> ToolContext:
    kwargs.setdefault("run_id", "run-1")
    kwargs.setdefault("step_id", "s1")
    kwargs.setdefault("autonomy", "act")
    return ToolContext(**kwargs)


def _tool(handler, *, name: str = "writes_something", effect_class: str = "unsafe_write",
          risk: str = "read", parameters: dict | None = None) -> Tool:
    return Tool(
        name=name, description="does a thing", parameters=parameters or obj(body={}),
        handler=handler, effect_class=effect_class, risk=risk,
    )


# --- the protocol, through the one door every call goes through ---------------


async def test_the_record_of_an_unsafe_call_is_on_disk_before_the_call_runs(cfg, journaled):
    """The promise the whole pass rests on, asserted from inside the handler.

    By the time the tool's own code runs, the intent is journaled, the row exists, and it
    already says `started` - so a machine that dies in the next instruction leaves behind
    the evidence that this may have happened.
    """
    seen: dict = {}

    async def handler(args, ctx):
        row = get_ledger().entries("run-1")[0]
        seen["state"] = row.state
        seen["events"] = [e.type for e in journaled(run_id="run-1")]
        seen["started_at"] = row.started_at
        return ToolResult(content="sent")

    result = await _executor(cfg, _tool(handler)).run(
        "writes_something", {"body": "hi"}, _ctx()
    )

    assert result.ok
    assert seen["state"] == "started"
    assert seen["events"] == ["effect_intended"]
    assert seen["started_at"] is not None

    row = get_ledger().entries("run-1")[0]
    assert row.state == "committed"
    assert row.tool == "writes_something"
    assert row.effect_class == "unsafe_write"
    assert row.step_id == "s1"
    assert row.attempt == 1
    assert row.result_ref and row.result_ref.startswith("action:")
    assert [e.type for e in journaled("effect_intended", "effect_committed")] == [
        "effect_intended",
        "effect_committed",
    ]


async def test_the_journal_and_the_ledger_describe_the_same_effect(cfg, journaled):
    """Two records of one call that disagree are worse than one record. They are written
    from the same values on purpose, and a fold of the journal has to be able to find the
    row it rebuilt."""
    async def handler(args, ctx):
        return ToolResult(content="sent")

    await _executor(cfg, _tool(handler)).run("writes_something", {"body": "hi"}, _ctx())

    row = get_ledger().entries("run-1")[0]
    intended, committed = journaled("effect_intended", "effect_committed")
    assert intended.payload["idempotency_key"] == row.idempotency_key
    assert intended.payload["args_digest"] == row.args_hash
    assert intended.payload["effect_class"] == row.effect_class
    assert intended.payload["tool_name"] == row.tool
    assert intended.payload["step_id"] == row.step_id
    assert committed.payload["effect_id"] == intended.payload["effect_id"] == row.effect_id
    assert committed.payload["status"] == "committed"
    assert committed.payload["result_digest"] is not None
    assert committed.payload["error"] is None


async def test_a_read_only_call_leaves_no_effect_behind(cfg, journaled):
    """`read` changes nothing outside, so a crash has no question to ask about it. Rowing
    every `time_now` would bury the entries that matter in the ones that never do."""
    async def handler(args, ctx):
        return ToolResult(content="12:04")

    await _executor(cfg, _tool(handler, name="reads_something", effect_class="read")).run(
        "reads_something", {}, _ctx()
    )

    assert get_ledger().entries() == []
    assert journaled("effect_intended", "effect_committed") == []
    assert ledgered("read") is False
    assert ledgered("idempotent_write") and ledgered("unsafe_write")


async def test_a_call_that_raises_is_closed_as_failed_rather_than_left_open(cfg, journaled):
    """An open row means "may have happened". A tool that blew up before doing anything
    must not read as that, or every crash-free failure becomes an uncertain effect."""
    async def handler(args, ctx):
        raise RuntimeError("smtp said no")

    result = await _executor(cfg, _tool(handler)).run("writes_something", {}, _ctx())

    assert result.ok is False
    row = get_ledger().entries("run-1")[0]
    assert row.state == "failed"
    assert row.result_ref and row.result_ref.startswith("action:")
    committed = journaled("effect_committed")[0]
    assert committed.payload["status"] == "failed"
    assert "smtp said no" in committed.payload["error"]


async def test_a_tool_that_reports_failure_is_a_failed_effect_not_a_committed_one(cfg, journaled):
    """`ok=False` is the tool answering the ledger's only question with "no"."""
    async def handler(args, ctx):
        return ToolResult(content='{"error": "quota exceeded"}', ok=False)

    await _executor(cfg, _tool(handler)).run("writes_something", {}, _ctx())

    row = get_ledger().entries("run-1")[0]
    assert row.state == "failed"
    assert "quota exceeded" in journaled("effect_committed")[0].payload["error"]


async def test_a_failure_with_nothing_to_say_still_says_something(cfg, journaled):
    """An empty `error` is indistinguishable from a field nobody filled in, which is the
    shape of bug this codebase keeps finding."""
    async def handler(args, ctx):
        return ToolResult(content="", ok=False)

    await _executor(cfg, _tool(handler)).run("writes_something", {}, _ctx())

    assert journaled("effect_committed")[0].payload["error"] == (
        "the tool reported failure with no message"
    )


async def test_two_attempts_at_the_same_call_share_one_row_and_count_the_attempt(cfg, journaled):
    """The same logical call twice is one effect with two attempts - which is the
    canonicalization test, asserted where it actually has to hold.

    It is recorded, not prevented: refusing the second attempt is reconciliation, and
    nothing reads this table yet.

    The tool declares no properties, which is what lets the per-attempt fields through to
    the key derivation: for a tool that does declare them, the executor's own argument
    validation rejects an undeclared `request_id` long before this point.
    """
    calls: list[str] = []

    async def handler(args, ctx):
        calls.append(args["body"])
        return ToolResult(content="sent")

    executor = _executor(cfg, _tool(handler, parameters=obj()))
    await executor.run(
        "writes_something",
        {"body": "hi", "reason": "first try", "request_id": "req-1", "now": "18:00:00"},
        _ctx(),
    )
    await executor.run(
        "writes_something",
        {"now": "18:00:09", "request_id": "req-2", "body": "hi", "reason": "again"},
        _ctx(),
    )

    rows = get_ledger().entries("run-1")
    assert len(rows) == 1
    assert rows[0].attempt == 2
    assert rows[0].state == "committed"
    assert len(calls) == 2
    assert [e.payload["attempt"] for e in journaled("effect_intended")] == [1, 2]


async def test_a_different_call_in_the_same_step_is_a_different_effect(cfg):
    async def handler(args, ctx):
        return ToolResult(content="sent")

    executor = _executor(cfg, _tool(handler))
    await executor.run("writes_something", {"body": "one"}, _ctx())
    await executor.run("writes_something", {"body": "two"}, _ctx())

    assert len({row.idempotency_key for row in get_ledger().entries("run-1")}) == 2


async def test_a_call_whose_arguments_have_no_stable_key_is_refused_rather_than_run(cfg):
    """A key derived from a repr is a key that never matches itself, so the crash it exists
    for would find nothing. The call is refused loudly instead of running unrecorded."""
    ran: list[int] = []

    async def handler(args, ctx):
        ran.append(1)
        return ToolResult(content="sent")

    result = await _executor(cfg, _tool(handler)).run(
        "writes_something", {"body": object()}, _ctx()
    )

    assert ran == []
    assert result.ok is False
    assert "canonical" in result.content
    assert get_ledger().entries() == []


async def test_a_call_refused_by_policy_never_becomes_an_effect(cfg, journaled):
    """Intent is recorded after the last gate, not before it. A denied call did not happen,
    and an `intended` row for one would be an uncertain effect a resume has to ask about."""
    async def handler(args, ctx):
        raise AssertionError("a denied call must not reach the handler")

    result = await _executor(
        cfg, _tool(handler, risk="external"), approve=False
    ).run("writes_something", {"body": "hi", "reason": "because"}, _ctx())

    assert result.ok is False
    assert result.data["denied"] is True
    assert get_ledger().entries() == []
    assert journaled("effect_intended") == []


async def test_a_call_with_no_run_opens_one_of_its_own_rather_than_sharing_a_bucket(cfg):
    """`policy/replay.execute_approved` runs a queued approval long after its turn ended,
    with no run and no step. A placeholder shared by every such call would make all of them
    hash alike - one bucket, and every detached call a retry of every other one."""
    async def handler(args, ctx):
        return ToolResult(content="sent")

    executor = _executor(cfg, _tool(handler))
    await executor.run("writes_something", {"body": "hi"}, ToolContext(autonomy="act"))
    await executor.run("writes_something", {"body": "hi"}, ToolContext(autonomy="act"))

    rows = get_ledger().entries()
    assert len(rows) == 2
    assert len({r.idempotency_key for r in rows}) == 2
    assert all(r.run_id.startswith("detached:") for r in rows)
    assert len({r.run_id for r in rows}) == 2


async def test_a_call_with_a_run_but_no_step_is_treated_as_detached(cfg):
    """Filling a missing step in with "s1" would collide with the step the loop hands out
    under that same name - two different calls, one key, one of them silently suppressed."""
    async def handler(args, ctx):
        return ToolResult(content="sent")

    await _executor(cfg, _tool(handler)).run(
        "writes_something", {"body": "hi"}, ToolContext(run_id="run-1", autonomy="act")
    )

    row = get_ledger().entries()[0]
    assert row.run_id.startswith("detached:")


# --- the state machine, directly ---------------------------------------------


@pytest.fixture
def ledger(tmp_path: Path) -> EffectLedger:
    writer = JournalWriter(JournalStore(tmp_path / "journal.db"))
    yield EffectLedger(writer)
    writer.close()


def _intend(ledger: EffectLedger, **over):
    kwargs = {
        "run_id": "run-1", "step_id": "s1", "tool": "gmail_send",
        "effect_class": "unsafe_write", "args": {"to": "a@example.com"},
    }
    return ledger.intend(**{**kwargs, **over})


def test_the_protocol_refuses_a_transition_it_does_not_define(ledger: EffectLedger) -> None:
    """A ledger that accepts `committed -> started` is a ledger whose states mean nothing
    to the pass that has to reconcile them."""
    effect = _intend(ledger)
    with pytest.raises(EffectStateError):
        effect.committed(result_ref="action:1")  # never dispatched

    effect.dispatched()
    with pytest.raises(EffectStateError):
        effect.dispatched()
    effect.committed(result_ref="action:1")
    with pytest.raises(EffectStateError):
        effect.failed("changed my mind", result_ref="action:1")


def test_an_effect_can_fail_before_it_was_ever_dispatched(ledger: EffectLedger) -> None:
    effect = _intend(ledger)
    effect.failed("refused downstream", result_ref="action:1")
    row = ledger.get(effect.key)
    assert row is not None and row.state == "failed" and row.started_at is None


def test_the_states_include_the_one_only_a_resume_can_write(ledger: EffectLedger) -> None:
    """`orphaned` is Pass 4's word for a row a crash left at `started`. It is in the
    vocabulary and in the CHECK constraint now so that pass fills a slot."""
    assert ledger_mod.ORPHANED in ledger_mod.EFFECT_STATES
    assert ledger_mod.ORPHANED not in {s for t in ledger_mod.ALLOWED.values() for s in t}


def test_a_finished_effect_always_points_at_its_result(ledger: EffectLedger) -> None:
    """Enforced by the storage, not by the caller: a terminal row with a null `result_ref`
    reads as "done, outcome unrecorded", which is this codebase's favourite kind of wrong."""
    effect = _intend(ledger)
    effect.dispatched()
    with pytest.raises(sqlite3.IntegrityError):
        with ledger.store.transaction() as conn:
            conn.execute(
                "UPDATE effect SET state = 'committed' WHERE idempotency_key = ?",
                (effect.key,),
            )


# --- the file ----------------------------------------------------------------


def test_an_existing_journal_is_upgraded_rather_than_silently_left_at_v1(tmp_path: Path) -> None:
    """2a's open question 6: `_migrate` bumped the version whether or not a step ran. The
    ledger is the first schema change, so the ladder is exercised on a file that predates
    it - with its events still in it afterwards."""
    path = tmp_path / "journal.db"
    raw = sqlite3.connect(str(path))
    from agentd.journal.store import SCHEMA_V1

    raw.executescript(SCHEMA_V1)
    raw.execute(
        "INSERT INTO journal (run_id, seq, ts, type, payload) VALUES ('run-0',1,'t','x','{}')"
    )
    raw.execute("PRAGMA user_version=1")
    raw.commit()
    raw.close()

    with JournalStore(path) as store:
        assert store.count("run-0") == 1
        assert int(store.query("PRAGMA user_version")[0][0]) == 2
        assert store.query(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='effect'"
        )


def test_pruning_a_run_takes_its_effects_with_it(tmp_path: Path) -> None:
    """A run is entirely present or entirely gone. An effect row whose journal was pruned
    is a claim with nothing behind it."""
    from agentd.journal.store import Retention

    writer = JournalWriter(JournalStore(tmp_path / "journal.db"))
    try:
        ledger = EffectLedger(writer)
        _intend(ledger).dispatched()
        writer.flush()
        assert ledger.entries("run-1")
        writer.store.prune(Retention(mode="max_age_days", max_age_days=0))
        assert ledger.entries("run-1") == []
    finally:
        writer.close()


# --- the exit criterion: kill it mid-call ------------------------------------

CRASHER = """
import sys, time
sys.path.insert(0, {src!r})
from agentd.journal import JournalStore, JournalWriter
from agentd.journal.ledger import EffectLedger

writer = JournalWriter(JournalStore({path!r}, synchronous="FULL"), buffering=True)
ledger = EffectLedger(writer)
effect = ledger.intend(
    run_id="run-1", step_id="s1", tool="gmail_send", effect_class="unsafe_write",
    args={{"to": "a@example.com", "body": "the money is sent"}},
)
effect.dispatched()
print("dispatched", flush=True)
while True:            # stands in for a tool call that never returns
    time.sleep(0.05)
"""


def test_killing_the_process_mid_call_leaves_the_effect_stuck_at_started(tmp_path: Path) -> None:
    """The pass's exit criterion, against a real SIGKILL rather than a simulated one.

    What survives is the whole point: an `unsafe_write` that was dispatched and never
    resolved. Pass 4 reads exactly this and reports it as uncertain instead of re-sending.
    """
    path = tmp_path / "journal.db"
    src = str(Path(__file__).resolve().parents[1] / "src")
    script = textwrap.dedent(CRASHER).format(src=src, path=str(path))
    proc = subprocess.Popen(
        [sys.executable, "-c", script], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert proc.stdout is not None
        assert proc.stdout.readline().strip() == "dispatched"
        time.sleep(0.2)
        os.kill(proc.pid, signal.SIGKILL)
    finally:
        proc.wait(timeout=10)
    assert proc.returncode == -signal.SIGKILL

    with JournalStore(path) as store:
        rows = EffectLedger(JournalWriter(store)).entries("run-1")
        assert len(rows) == 1
        row = rows[0]
        assert row.state == "started"
        assert row.started_at is not None
        assert row.result_ref is None
        assert row.effect_class == "unsafe_write"
        # And the announcement that preceded it is there, with no terminal event after it.
        types = [e.type for e in store.read("run-1")]
        assert types == ["effect_intended"]
        raw = sqlite3.connect(str(path))
        try:
            assert raw.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            payload = json.loads(
                raw.execute("SELECT payload FROM journal").fetchone()[0]
            )
            assert payload["idempotency_key"] == row.idempotency_key
        finally:
            raw.close()
