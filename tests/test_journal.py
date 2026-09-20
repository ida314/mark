"""Pass 2a: the journal store and writer.

The pass's exit criterion is two sentences - "events append durably" and "process kill
mid-write leaves the journal readable and consistent" - and the second one is the reason
this file spawns real subprocesses and SIGKILLs them rather than simulating a crash. A
mocked crash tests the mock.

The property under all of it: **what survives a kill is a prefix of what happened.** Never
a hole, never a reordering, never a half-written row.
"""

from __future__ import annotations

import json
import os
import signal
import sqlite3
import subprocess
import sys
import textwrap
import threading
import time
from datetime import timedelta
from pathlib import Path

import pytest

from agentd.config import Config, JournalConfig, PathsConfig
from agentd.ids import utcnow
from agentd.journal import (
    Event,
    JournalError,
    JournalStore,
    JournalWriter,
    PendingEvent,
    Retention,
    SchemaTooNew,
    SeqConflict,
    default_path,
    is_synchronous,
    retention_from_config,
)


@pytest.fixture
def store(tmp_path: Path) -> JournalStore:
    s = JournalStore(tmp_path / "journal.db")
    yield s
    s.close()


@pytest.fixture
def writer(store: JournalStore) -> JournalWriter:
    w = JournalWriter(store)
    yield w
    w.close()


def _cfg(tmp_path: Path, **journal: object) -> Config:
    return Config(paths=PathsConfig(data_dir=tmp_path), journal=JournalConfig(**journal))


# --- the exit criterion: events append durably --------------------------------


def test_an_appended_event_reads_back_whole(writer: JournalWriter, store: JournalStore) -> None:
    written = writer.append(
        "run-1", "effect_intended", {"tool": "fs_write", "args_hash": "abc"},
        worker_id="w-1", step_id="s-3",
    )
    assert written is not None and written.seq == 1

    (event,) = store.read("run-1")
    assert event.run_id == "run-1"
    assert event.seq == 1
    assert event.type == "effect_intended"
    assert event.payload == {
        "tool": "fs_write", "args_hash": "abc", "worker_id": "w-1", "step_id": "s-3",
    }
    assert event.ts.startswith(str(utcnow().year))
    assert event.id > 0


def test_seq_starts_at_one_and_is_per_run(writer: JournalWriter, store: JournalStore) -> None:
    for i in range(3):
        writer.append("run-a", "message_appended", {"i": i})
    writer.append("run-b", "message_appended", {"i": 0})
    writer.flush()

    assert [e.seq for e in store.read("run-a")] == [1, 2, 3]
    assert [e.seq for e in store.read("run-b")] == [1]
    assert store.last_seq("run-a") == 3
    assert store.last_seq("run-never-seen") == 0


def test_a_reopened_journal_continues_the_run(tmp_path: Path) -> None:
    """A resumed run must not restart at 1 and collide with its own history."""
    path = tmp_path / "journal.db"
    with JournalStore(path) as s:
        JournalWriter(s, buffering=False).append("run-1", "agent_started", {})
    with JournalStore(path) as s:
        written = JournalWriter(s, buffering=False).append("run-1", "agent_finished", {})
        assert written is not None and written.seq == 2
        assert [e.seq for e in s.read("run-1")] == [1, 2]


def test_payload_survives_nesting_and_unicode(writer: JournalWriter, store: JournalStore) -> None:
    payload = {"z": [1, {"nested": None}], "a": "café ☕", "n": 1.5, "t": True}
    writer.append("run-1", "tool_finished", payload)
    writer.flush()
    assert store.read("run-1")[0].payload == payload


def test_an_empty_payload_is_legal(writer: JournalWriter, store: JournalStore) -> None:
    writer.append("run-1", "agent_started")
    writer.flush()
    assert store.read("run-1")[0].payload == {}


# --- monotonic seq enforcement ------------------------------------------------


def test_an_explicit_seq_that_does_not_advance_is_rejected(writer: JournalWriter) -> None:
    writer.append("run-1", "agent_started", seq=1)
    for bad in (1, 0, -5):
        with pytest.raises(SeqConflict):
            writer.append("run-1", "message_appended", seq=bad)


def test_a_rejected_append_leaves_no_row(writer: JournalWriter, store: JournalStore) -> None:
    writer.append("run-1", "agent_started", seq=1)
    with pytest.raises(SeqConflict):
        writer.append("run-1", "message_appended", seq=1)
    assert store.count("run-1") == 1
    # and the run is still writable afterwards - a rejection is not a poisoned connection
    assert writer.append("run-1", "message_appended", sync=True).seq == 2


def test_an_explicit_seq_may_skip_forward(writer: JournalWriter, store: JournalStore) -> None:
    """Monotonic, not contiguous. A gap is legal; going backwards is not."""
    writer.append("run-1", "agent_started", seq=1)
    writer.append("run-1", "agent_finished", seq=50)
    assert [e.seq for e in store.read("run-1")] == [1, 50]
    assert writer.append("run-1", "message_appended", sync=True).seq == 51


def test_a_storage_level_rejection_rolls_back_and_leaves_the_store_usable(
    store: JournalStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The path a *second process* takes, which no explicit `seq` is needed to reach.

    A writer that read MAX(seq) before another one committed will offer a seq that is no
    longer free, and the in-Python check cannot see it - by construction, it is working
    from the stale number. Only the trigger catches this, and what happens next matters
    more than the rejection itself: the aborted statement leaves a transaction open, and
    without the rollback every later append on this connection dies on "cannot start a
    transaction within a transaction".
    """
    JournalWriter(store, buffering=False).append("run-1", "agent_started", {})
    monkeypatch.setattr(store, "_last_seq", lambda run_id: 0)  # what a stale reader saw

    with pytest.raises(SeqConflict):
        store.append([PendingEvent(run_id="run-1", type="tool_started")])

    monkeypatch.undo()
    assert store.count("run-1") == 1
    assert JournalWriter(store, buffering=False).append("run-1", "tool_started", {}).seq == 2


def test_a_batch_rejected_part_way_through_lands_none_of_itself(
    store: JournalStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Atomic per batch: this is what makes a killed process lose a suffix, not a hole."""
    JournalWriter(store, buffering=False).append("run-1", "agent_started", {})
    monkeypatch.setattr(store, "_last_seq", lambda run_id: 0)

    with pytest.raises(SeqConflict):
        store.append([
            PendingEvent(run_id="other-run", type="agent_started"),
            PendingEvent(run_id="run-1", type="tool_started"),
        ])

    monkeypatch.undo()
    assert store.count("other-run") == 0
    assert store.count("run-1") == 1


def test_a_malformed_event_is_not_reported_as_a_seq_conflict(store: JournalStore) -> None:
    with pytest.raises(JournalError) as caught:
        store.append([PendingEvent(run_id=None, type="agent_started")])  # type: ignore[arg-type]
    assert not isinstance(caught.value, SeqConflict)
    assert store.count() == 0
    assert JournalWriter(store, buffering=False).append("run-1", "agent_started", {}).seq == 1


def test_the_trigger_rejects_a_write_that_bypasses_the_writer(tmp_path: Path) -> None:
    """The invariant lives in the storage, not in the one class that happens to hold it.

    The CLI and the daemon are separate processes against one file; a writer-only rule is
    a rule that holds until the second process shows up.
    """
    path = tmp_path / "journal.db"
    with JournalStore(path) as s:
        JournalWriter(s, buffering=False).append("run-1", "agent_started", {})
        JournalWriter(s, buffering=False).append("run-1", "message_appended", {})

    raw = sqlite3.connect(str(path))
    try:
        with pytest.raises(sqlite3.IntegrityError, match="seq must increase"):
            raw.execute(
                "INSERT INTO journal (run_id, seq, ts, type, payload) VALUES (?,?,?,?,?)",
                ("run-1", 1, utcnow().isoformat(), "tool_started", "{}"),
            )
        with pytest.raises(sqlite3.IntegrityError):
            raw.execute(
                "INSERT INTO journal (run_id, seq, ts, type, payload) VALUES (?,?,?,?,?)",
                ("run-1", 0, utcnow().isoformat(), "tool_started", "{}"),
            )
    finally:
        raw.close()


def test_a_non_json_payload_cannot_be_stored(tmp_path: Path) -> None:
    path = tmp_path / "journal.db"
    JournalStore(path).close()
    raw = sqlite3.connect(str(path))
    try:
        with pytest.raises(sqlite3.IntegrityError):
            raw.execute(
                "INSERT INTO journal (run_id, seq, ts, type, payload) VALUES (?,?,?,?,?)",
                ("run-1", 1, utcnow().isoformat(), "tool_started", "not json"),
            )
    finally:
        raw.close()


def test_concurrent_writers_on_one_run_do_not_collide(store: JournalStore) -> None:
    """No cached cursor anywhere: seq comes from MAX(seq) inside the write transaction."""
    writers = [JournalWriter(store, buffering=False) for _ in range(4)]
    errors: list[BaseException] = []

    def hammer(w: JournalWriter) -> None:
        try:
            for i in range(25):
                w.append("run-1", "tool_progress", {"i": i})
        except BaseException as exc:  # noqa: BLE001 - reported, not swallowed
            errors.append(exc)

    threads = [threading.Thread(target=hammer, args=(w,)) for w in writers]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    assert [e.seq for e in store.read("run-1")] == list(range(1, 101))


# --- synchronous vs buffered --------------------------------------------------


@pytest.mark.parametrize(
    "event_type,expected",
    [
        ("effect_intended", True),
        ("effect_committed", True),
        ("effect_failed", True),
        ("checkpoint_written", True),
        ("agent_started", False),
        ("tool_progress", False),
        ("message_appended", False),
        ("worker_finished", False),
    ],
)
def test_the_durability_class_of_each_event_type(event_type: str, expected: bool) -> None:
    assert is_synchronous(event_type) is expected


def test_effect_and_checkpoint_events_are_on_disk_before_append_returns(
    writer: JournalWriter, store: JournalStore
) -> None:
    for event_type in ("effect_intended", "effect_committed", "checkpoint_written"):
        writer.append("run-1", event_type, {})
        assert writer.pending == 0
        assert store.read("run-1")[-1].type == event_type


def test_other_event_types_may_wait(writer: JournalWriter, store: JournalStore) -> None:
    assert writer.append("run-1", "tool_progress", {"pct": 10}) is None
    assert writer.pending == 1
    assert store.count("run-1") == 0
    assert writer.flush() == 1
    assert store.count("run-1") == 1


def test_a_synchronous_event_carries_the_buffered_tail_with_it(
    writer: JournalWriter, store: JournalStore
) -> None:
    """Ordering is call order. An effect cannot overtake the events that preceded it."""
    writer.append("run-1", "agent_started", {})
    writer.append("run-1", "tool_requested", {})
    writer.append("run-1", "effect_intended", {"tool": "shell_exec"})

    assert writer.pending == 0
    assert [(e.seq, e.type) for e in store.read("run-1")] == [
        (1, "agent_started"), (2, "tool_requested"), (3, "effect_intended"),
    ]


def test_the_buffer_flushes_when_it_fills(store: JournalStore) -> None:
    w = JournalWriter(store, buffer_max=3, buffer_max_age_s=999)
    for i in range(2):
        w.append("run-1", "tool_progress", {"i": i})
    assert store.count("run-1") == 0
    w.append("run-1", "tool_progress", {"i": 2})
    assert store.count("run-1") == 3
    assert w.pending == 0


def test_the_buffer_flushes_when_the_tail_gets_old(store: JournalStore) -> None:
    w = JournalWriter(store, buffer_max=1000, buffer_max_age_s=0.05)
    w.append("run-1", "tool_progress", {"i": 0})
    assert store.count("run-1") == 0
    time.sleep(0.06)
    w.append("run-1", "tool_progress", {"i": 1})
    assert store.count("run-1") == 2


def test_buffering_off_writes_everything_immediately(store: JournalStore) -> None:
    w = JournalWriter(store, buffering=False)
    assert w.append("run-1", "tool_progress", {}).seq == 1
    assert store.count("run-1") == 1


def test_close_flushes_the_tail(tmp_path: Path) -> None:
    path = tmp_path / "journal.db"
    with JournalStore(path) as s:
        with JournalWriter(s) as w:
            w.append("run-1", "tool_progress", {})
            assert w.pending == 1
    with JournalStore(path) as s:
        assert s.count("run-1") == 1


def test_a_write_failure_raises_rather_than_being_swallowed(store: JournalStore) -> None:
    """The opposite of obs/telemetry, deliberately. A lost effect record is the
    duplicate-side-effect bug, not a missing measurement."""
    w = JournalWriter(store, buffering=False)
    store.close()
    with pytest.raises(sqlite3.ProgrammingError):
        w.append("run-1", "effect_intended", {})


def test_a_failed_flush_does_not_resurrect_its_batch(store: JournalStore, tmp_path: Path) -> None:
    w = JournalWriter(store, buffer_max=1000, buffer_max_age_s=999)
    w.append("run-1", "tool_progress", {"i": 0})
    store.close()
    with pytest.raises(sqlite3.ProgrammingError):
        w.flush()
    assert w.pending == 0


# --- reads --------------------------------------------------------------------


def test_read_resumes_from_a_seq(writer: JournalWriter, store: JournalStore) -> None:
    for i in range(5):
        writer.append("run-1", "message_appended", {"i": i})
    writer.flush()
    assert [e.payload["i"] for e in store.read("run-1", after_seq=2)] == [2, 3, 4]
    assert [e.payload["i"] for e in store.read("run-1", after_seq=2, limit=2)] == [2, 3]


def test_read_all_is_a_cross_run_feed_ordered_by_id(writer: JournalWriter, store: JournalStore) -> None:
    writer.append("run-a", "agent_started", {})
    writer.append("run-b", "agent_started", {})
    writer.append("run-a", "agent_finished", {})
    writer.flush()

    events = store.read_all()
    assert [(e.run_id, e.seq) for e in events] == [("run-a", 1), ("run-b", 1), ("run-a", 2)]
    ids = [e.id for e in events]
    assert ids == sorted(ids)
    assert [e.id for e in store.read_all(after_id=ids[0])] == ids[1:]


def test_the_feed_cursor_never_rewinds_after_a_prune(writer: JournalWriter, store: JournalStore) -> None:
    """Why `id` is AUTOINCREMENT and not the implicit rowid: rowids are reused after a
    delete, so a frontend that reconnected with a saved cursor would skip live events."""
    for i in range(5):
        writer.append("old-run", "message_appended", {"i": i})
    writer.flush()
    high_water = max(e.id for e in store.read_all())

    store.prune(Retention(mode="max_age_days", max_age_days=1), now=utcnow() + timedelta(days=2))
    assert store.count() == 0

    writer.append("new-run", "agent_started", {})
    writer.flush()
    assert store.read_all()[0].id > high_water


def test_runs_summarizes_what_retention_decides_on(writer: JournalWriter, store: JournalStore) -> None:
    writer.append("run-a", "agent_started", {})
    writer.append("run-a", "agent_finished", {})
    writer.append("run-b", "agent_started", {})
    writer.flush()

    by_id = {r.run_id: r for r in store.runs()}
    assert by_id["run-a"].events == 2
    assert by_id["run-a"].last_seq == 2
    assert by_id["run-a"].first_ts <= by_id["run-a"].last_ts
    assert by_id["run-b"].events == 1
    assert store.count() == 3


# --- retention ----------------------------------------------------------------


def test_the_default_policy_keeps_everything(writer: JournalWriter, store: JournalStore) -> None:
    writer.append("run-1", "agent_started", {})
    writer.flush()
    result = store.prune()
    assert result.runs == [] and result.events == 0
    assert store.count() == 1


def test_the_default_policy_keeps_everything_however_old(store: JournalStore) -> None:
    ancient = (utcnow() - timedelta(days=4000)).isoformat()
    store.append([PendingEvent(run_id="run-1", type="agent_started", ts=ancient)])
    assert store.prune(Retention()).runs == []
    assert store.count() == 1


def test_the_age_hook_drops_whole_runs_and_only_stale_ones(store: JournalStore) -> None:
    old = (utcnow() - timedelta(days=10)).isoformat()
    recent = (utcnow() - timedelta(hours=1)).isoformat()
    store.append([
        PendingEvent(run_id="old-run", type="agent_started", ts=old),
        PendingEvent(run_id="old-run", type="agent_finished", ts=old),
        PendingEvent(run_id="live-run", type="agent_started", ts=old),
        PendingEvent(run_id="live-run", type="tool_started", ts=recent),
        PendingEvent(run_id="new-run", type="agent_started", ts=recent),
    ])

    result = store.prune(Retention(mode="max_age_days", max_age_days=7))
    assert result.runs == ["old-run"]
    assert result.events == 2
    assert store.count("old-run") == 0
    # judged on the run's last event, so a long-running run is not cut in half
    assert store.count("live-run") == 2
    assert store.count("new-run") == 1


def test_a_dry_run_reports_without_deleting(store: JournalStore) -> None:
    old = (utcnow() - timedelta(days=10)).isoformat()
    store.append([PendingEvent(run_id="old-run", type="agent_started", ts=old)])
    result = store.prune(Retention(mode="max_age_days", max_age_days=7), dry_run=True)
    assert result.runs == ["old-run"] and result.events == 1
    assert store.count("old-run") == 1


def test_an_unset_max_age_prunes_nothing(store: JournalStore) -> None:
    old = (utcnow() - timedelta(days=4000)).isoformat()
    store.append([PendingEvent(run_id="old-run", type="agent_started", ts=old)])
    assert store.prune(Retention(mode="max_age_days")).runs == []
    assert store.count() == 1


def test_a_pruned_run_can_be_written_again(store: JournalStore) -> None:
    old = (utcnow() - timedelta(days=10)).isoformat()
    store.append([PendingEvent(run_id="old-run", type="agent_started", ts=old)])
    store.prune(Retention(mode="max_age_days", max_age_days=7))
    w = JournalWriter(store, buffering=False)
    assert w.append("old-run", "agent_started", {}).seq == 1


# --- configuration ------------------------------------------------------------


def test_the_default_path_sits_under_the_data_dir(tmp_path: Path) -> None:
    assert default_path(_cfg(tmp_path)) == tmp_path.resolve() / "journal.db"


def test_an_explicit_path_wins(tmp_path: Path) -> None:
    elsewhere = tmp_path / "elsewhere" / "j.db"
    assert default_path(_cfg(tmp_path, path=elsewhere)) == elsewhere


def test_a_writer_opened_from_config_honours_its_settings(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path, buffering=False, synchronous="NORMAL", buffer_max=7)
    with JournalWriter.open(cfg) as w:
        assert w.buffering is False and w.buffer_max == 7
        w.append("run-1", "agent_started", {})
        assert (tmp_path.resolve() / "journal.db").exists()
        mode = w.store._conn.execute("PRAGMA synchronous").fetchone()[0]
        assert mode == 1  # NORMAL


def test_retention_comes_from_config(tmp_path: Path) -> None:
    assert retention_from_config(_cfg(tmp_path)) == Retention()
    cfg = _cfg(tmp_path, retention="max_age_days", retention_max_age_days=30)
    assert retention_from_config(cfg) == Retention(mode="max_age_days", max_age_days=30)


def test_a_journal_from_the_future_is_not_opened(tmp_path: Path) -> None:
    path = tmp_path / "journal.db"
    JournalStore(path).close()
    raw = sqlite3.connect(str(path))
    raw.execute("PRAGMA user_version=99")
    raw.close()
    with pytest.raises(SchemaTooNew):
        JournalStore(path)


def test_opening_an_existing_journal_twice_is_harmless(tmp_path: Path) -> None:
    path = tmp_path / "journal.db"
    with JournalStore(path) as a:
        JournalWriter(a, buffering=False).append("run-1", "agent_started", {})
    with JournalStore(path) as b:
        assert b.count("run-1") == 1


# --- the other exit criterion: survive a kill ---------------------------------


CRASHER = """
import sys, time
sys.path.insert(0, {src!r})
from agentd.journal import JournalStore, JournalWriter

store = JournalStore({path!r}, synchronous="FULL")
writer = JournalWriter(store, buffering={buffering}, buffer_max=8, buffer_max_age_s=999)
i = 0
while True:
    i += 1
    writer.append("run-1", {event_type!r}, {{"i": i, "filler": "x" * 400}})
    if i == 1:
        print("started", flush=True)
"""


def _kill_mid_write(tmp_path: Path, *, event_type: str, buffering: bool) -> Path:
    path = tmp_path / "journal.db"
    src = str(Path(__file__).resolve().parents[1] / "src")
    script = textwrap.dedent(CRASHER).format(
        src=src, path=str(path), event_type=event_type, buffering=str(buffering)
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", script], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
    )
    try:
        assert proc.stdout is not None
        assert proc.stdout.readline().strip() == "started"
        time.sleep(0.4)
        os.kill(proc.pid, signal.SIGKILL)
    finally:
        proc.wait(timeout=10)
    assert proc.returncode == -signal.SIGKILL
    return path


def _assert_prefix(path: Path) -> list[Event]:
    """Readable, consistent, and a prefix: contiguous seqs from 1, payloads intact."""
    with JournalStore(path) as s:
        events = s.read("run-1")
        assert events, "nothing survived the kill"
        assert [e.seq for e in events] == list(range(1, len(events) + 1))
        assert [e.payload["i"] for e in events] == list(range(1, len(events) + 1))
        assert all(e.payload["filler"] == "x" * 400 for e in events)
        # still writable after recovery, and it continues rather than restarting
        JournalWriter(s, buffering=False).append("run-1", "run_resumed", {})
        assert s.last_seq("run-1") == len(events) + 1
        return events


def test_a_killed_process_leaves_synchronous_events_readable_and_whole(tmp_path: Path) -> None:
    path = _kill_mid_write(tmp_path, event_type="effect_intended", buffering=True)
    _assert_prefix(path)


def test_a_killed_process_leaves_buffered_events_as_a_clean_prefix(tmp_path: Path) -> None:
    """A buffered tail is lost - that is what buffering means - but what landed is exact."""
    path = _kill_mid_write(tmp_path, event_type="tool_progress", buffering=True)
    _assert_prefix(path)


def test_a_killed_unbuffered_process_leaves_no_partial_row(tmp_path: Path) -> None:
    path = _kill_mid_write(tmp_path, event_type="tool_progress", buffering=False)
    _assert_prefix(path)


def test_the_killed_file_is_not_corrupt(tmp_path: Path) -> None:
    path = _kill_mid_write(tmp_path, event_type="effect_committed", buffering=True)
    raw = sqlite3.connect(str(path))
    try:
        assert raw.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        rows = raw.execute("SELECT payload FROM journal").fetchall()
        assert rows and all(json.loads(r[0]) for r in rows)
    finally:
        raw.close()
