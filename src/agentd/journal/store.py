"""SQLite storage for the run journal.

The governing invariant of the runtime is `state = fold(reduce, journal, initial)`. This
module owns the one table that invariant folds over, so its job is narrow and absolute:
every append is ordered, durable, and either fully present or fully absent.

Three properties are load-bearing, and each is enforced by the storage rather than by the
caller that happens to be writing:

1. `seq` increases per run. A `BEFORE INSERT` trigger rejects anything else, so a second
   process appending to the same run cannot interleave a lower `seq` even if it never goes
   through `JournalWriter`.
2. `seq` is assigned inside the same transaction that reads `MAX(seq)`, under
   `BEGIN IMMEDIATE`. There is no cached cursor anywhere, so there is no cursor to go
   stale when the CLI and the daemon both write.
3. A killed process loses a *suffix*, never a hole. Appends commit as whole batches, so
   what survives on disk is always a prefix of what happened.

Naming collision worth flagging once: SQLite's own write-ahead log is also called a
journal (`PRAGMA journal_mode`). That is the database's crash log, unrelated to the run
journal this module stores in it.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from ..config import Config
from ..ids import utcnow

# `id` is not in the Pass 2 schema sketch, which keys on (run_id, seq) alone. It is here
# because session 2c replays "from a last-seen event id" across a feed that spans runs, and
# (run_id, seq) gives no cross-run order. SQLite's implicit rowid would have served until
# the first prune: rowids are reused after a delete, so a frontend cursor would silently
# rewind. AUTOINCREMENT is what makes the cursor monotonic for the life of the file.
#
# (run_id, seq) keeps the uniqueness the pass asked for, and UNIQUE builds the index that
# every per-run read uses.
SCHEMA_V1 = """
CREATE TABLE IF NOT EXISTS journal (
  id      integer PRIMARY KEY AUTOINCREMENT,
  run_id  text    NOT NULL,
  seq     integer NOT NULL CHECK (seq > 0),
  ts      text    NOT NULL,
  type    text    NOT NULL,
  payload text    NOT NULL CHECK (json_valid(payload)),
  UNIQUE (run_id, seq)
);

CREATE TRIGGER IF NOT EXISTS journal_seq_monotonic
BEFORE INSERT ON journal
WHEN NEW.seq <= COALESCE((SELECT MAX(seq) FROM journal WHERE run_id = NEW.run_id), 0)
BEGIN
  SELECT RAISE(ABORT, 'journal: seq must increase within a run');
END;
"""

# Session 3b. The effect ledger shares this file with the journal it is derived from: one
# file to back up, one connection, one lock, and no ordering question between two databases
# that describe the same call. `ledger.py` owns what the columns *mean*; the DDL lives here
# because this module owns the file's schema and the upgrade ladder below.
#
# The two CHECKs are the storage refusing states this codebase's characteristic bug would
# produce: an effect class or a state outside its vocabulary (a typo becoming a fourth
# class nothing reconciles), and a finished effect with no pointer to its result (a NULL
# that reads as "done, outcome unknown").
SCHEMA_V2 = """
CREATE TABLE IF NOT EXISTS effect (
  idempotency_key text PRIMARY KEY,
  run_id          text NOT NULL,
  step_id         text NOT NULL,
  tool            text NOT NULL,
  effect_class    text NOT NULL
                  CHECK (effect_class IN ('read','idempotent_write','unsafe_write')),
  args_hash       text NOT NULL,
  state           text NOT NULL
                  CHECK (state IN ('intended','started','committed','failed','orphaned')),
  result_ref      text,
  effect_id       text NOT NULL,
  attempt         integer NOT NULL CHECK (attempt > 0),
  created_at      text NOT NULL,
  updated_at      text NOT NULL,
  started_at      text,
  CHECK (result_ref IS NOT NULL OR state NOT IN ('committed','failed'))
);

CREATE INDEX IF NOT EXISTS effect_run ON effect (run_id, created_at);
CREATE INDEX IF NOT EXISTS effect_state ON effect (state);
"""

# The upgrade ladder, applied in order from whatever version the file is at. 2a's open
# question 6 was that `_migrate` could not migrate: it ran one `IF NOT EXISTS` script and
# bumped `user_version` regardless, so a v2 change to an existing file would have been a
# silent no-op with the version moved anyway. A step is now a numbered entry here, and the
# version is only bumped for steps that actually ran. Steps stay additive and are never
# edited once shipped - the same rule the Postgres migrations follow, for the same reason.
MIGRATIONS: dict[int, str] = {1: SCHEMA_V1, 2: SCHEMA_V2}
SCHEMA_VERSION = max(MIGRATIONS)


class JournalError(RuntimeError):
    """Base for journal failures. These are raised, never swallowed - see writer.py."""


class SeqConflict(JournalError):
    """An append carried a `seq` that did not increase within its run."""


class SchemaTooNew(JournalError):
    """The file was written by a later version of this code."""


@dataclass(frozen=True)
class Event:
    """One journaled event, as read back."""

    id: int
    run_id: str
    seq: int
    ts: str
    type: str
    payload: dict[str, Any]

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Event:
        return cls(
            id=row["id"], run_id=row["run_id"], seq=row["seq"],
            ts=row["ts"], type=row["type"], payload=json.loads(row["payload"]),
        )


@dataclass(frozen=True)
class PendingEvent:
    """An event on its way in.

    `seq=None` means "assign the next one for this run", which is the normal path and the
    only one that cannot conflict. A caller that supplies `seq` is asserting a position it
    worked out elsewhere, and gets `SeqConflict` if it was wrong.
    """

    run_id: str
    type: str
    payload: dict[str, Any] = field(default_factory=dict)
    ts: str = ""
    seq: int | None = None


@dataclass(frozen=True)
class RunInfo:
    """What retention policies get to decide on. Runs, never individual events."""

    run_id: str
    events: int
    first_ts: str
    last_ts: str
    last_seq: int


@dataclass(frozen=True)
class Retention:
    """The pruning hook. The initial policy is to keep everything.

    Pruning is whole-run only, and that is not an implementation shortcut. Deleting a
    prefix or a suffix of one run leaves a journal that still folds without error and folds
    to the wrong state - a silently wrong answer, which is the failure mode this codebase
    is least able to detect. A run is either entirely present or entirely gone.

    `max_age_days` is implemented and tested rather than sketched, so that the first person
    who needs pruning is turning on a path that has run, not writing one. It is still off
    by default: `mode = "keep_everything"`.

    Extension point for later passes: `prunable` receives `RunInfo`, so Pass 3 can refuse
    to prune a run holding an uncommitted effect, and Pass 4 can require a checkpoint to
    exist first. Neither exists yet, so neither is guessed at here.
    """

    mode: str = "keep_everything"
    max_age_days: int | None = None

    def prunable(self, runs: Sequence[RunInfo], *, now: datetime | None = None) -> list[str]:
        """Which whole runs this policy would drop. `now` is injectable so the age path is
        testable without waiting a day for it."""
        if self.mode == "keep_everything":
            return []
        if self.mode == "max_age_days":
            if self.max_age_days is None:
                return []
            # `last_ts` is an ISO-8601 UTC string from `ids.utcnow`, so a lexical compare is
            # a chronological one. Judged on a run's *last* event: a long run is only stale
            # once nothing has happened in it for the whole window.
            cutoff = ((now or utcnow()) - timedelta(days=self.max_age_days)).isoformat()
            return [r.run_id for r in runs if r.last_ts < cutoff]
        raise JournalError(f"unknown retention mode: {self.mode!r}")


@dataclass(frozen=True)
class PruneResult:
    runs: list[str]
    events: int


def retention_from_config(cfg: Config) -> Retention:
    return Retention(mode=cfg.journal.retention, max_age_days=cfg.journal.retention_max_age_days)


def default_path(cfg: Config) -> Path:
    from ..config import expand

    return cfg.journal.path or (expand(cfg.paths.data_dir) / "journal.db")


def _dumps(payload: dict[str, Any]) -> str:
    # Sorted keys so the same event serializes the same way every time: Pass 3 hashes
    # canonical arguments into idempotency keys and will want that to be true here too.
    return json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


class JournalStore:
    """One SQLite file, opened for the life of a process.

    Every write goes through `append`, which is the only method that starts a transaction.
    Reads are consistent without one because WAL readers never block on the writer.
    """

    def __init__(
        self,
        path: Path | str,
        *,
        synchronous: str = "FULL",
        busy_timeout_ms: int = 5000,
    ) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False plus the writer's lock: the daemon runs an event loop and
        # a scheduler, and both may journal. isolation_level=None hands transaction control
        # to us, because `append` needs BEGIN IMMEDIATE specifically.
        self._conn = sqlite3.connect(
            str(self.path), check_same_thread=False, isolation_level=None,
            timeout=busy_timeout_ms / 1000,
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        # FULL, not NORMAL. NORMAL survives a killed process - the data is already in the
        # OS page cache - but not a power cut, and the effect ledger in Pass 3 writes
        # `effect_intended` precisely so that a machine that dies mid-side-effect can tell
        # afterwards that it might have happened. That record is worthless if it was still
        # in a buffer. Chatty event types pay for this in batches; see JournalWriter.
        self._conn.execute(f"PRAGMA synchronous={synchronous}")
        self._conn.execute(f"PRAGMA busy_timeout={busy_timeout_ms}")
        # One connection, and transaction control is per connection: two threads inside
        # BEGIN IMMEDIATE on the same handle is "cannot start a transaction within a
        # transaction", not a busy wait. Several JournalWriters may share one store - the
        # CLI's loop and the daemon's scheduler inside one process - so serializing belongs
        # here, with the connection, rather than in any one writer's lock.
        self._lock = threading.RLock()
        self._migrate()

    # --- schema --------------------------------------------------------------

    def _migrate(self) -> None:
        version = int(self._conn.execute("PRAGMA user_version").fetchone()[0])
        if version > SCHEMA_VERSION:
            raise SchemaTooNew(
                f"{self.path} is at schema {version}, this build understands {SCHEMA_VERSION}"
            )
        # Each step, in order, and the version moves one step at a time. A file interrupted
        # between steps reopens at the last version whose script completed, so the next
        # open resumes the ladder instead of assuming the whole thing ran.
        for step in range(version + 1, SCHEMA_VERSION + 1):
            self._conn.executescript(MIGRATIONS[step])
            self._conn.execute(f"PRAGMA user_version={step}")

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> JournalStore:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # --- write ---------------------------------------------------------------

    def append(self, events: Sequence[PendingEvent]) -> list[Event]:
        """Append a batch atomically, assigning `seq` where it was left open.

        All of it lands or none of it does. That is what keeps a killed process from
        leaving a hole: the survivors are always a prefix.
        """
        if not events:
            return []
        now = utcnow().isoformat()
        conn = self._conn
        with self._lock:
            return self._append_locked(conn, events, now)

    def _append_locked(
        self, conn: sqlite3.Connection, events: Sequence[PendingEvent], now: str
    ) -> list[Event]:
        # IMMEDIATE takes the write lock up front. Without it two writers can both read
        # MAX(seq) under a shared lock and then deadlock upgrading, and SQLite resolves
        # that by failing one of them at COMMIT - after it has already been told its append
        # succeeded.
        conn.execute("BEGIN IMMEDIATE")
        try:
            cursors: dict[str, int] = {}
            written: list[Event] = []
            for ev in events:
                run_id = ev.run_id
                if run_id not in cursors:
                    cursors[run_id] = self._last_seq(run_id)
                last = cursors[run_id]
                if ev.seq is None:
                    seq = last + 1
                elif ev.seq <= last:
                    raise SeqConflict(
                        f"run {run_id}: seq {ev.seq} does not follow {last}"
                    )
                else:
                    seq = ev.seq
                ts = ev.ts or now
                payload = _dumps(ev.payload)
                cur = conn.execute(
                    "INSERT INTO journal (run_id, seq, ts, type, payload) VALUES (?,?,?,?,?)",
                    (run_id, seq, ts, ev.type, payload),
                )
                cursors[run_id] = seq
                written.append(
                    Event(id=int(cur.lastrowid or 0), run_id=run_id, seq=seq,
                          ts=ts, type=ev.type, payload=dict(ev.payload))
                )
            conn.execute("COMMIT")
            return written
        except sqlite3.IntegrityError as exc:
            conn.execute("ROLLBACK")
            # The trigger's ABORT and the UNIQUE constraint arrive here, and both mean the
            # same thing to a caller: the position is not free. This is the path a second
            # process takes - it read MAX(seq) before the first one committed - so it is
            # reachable without anyone having passed an explicit `seq` at all.
            #
            # Anything else that violates a constraint is a malformed event, not a race,
            # and saying "seq conflict" about it would send the next reader hunting for a
            # concurrency bug that is not there.
            text = str(exc)
            if "seq must increase" in text or "journal.run_id, journal.seq" in text:
                raise SeqConflict(text) from exc
            raise JournalError(text) from exc
        except BaseException:
            conn.execute("ROLLBACK")
            raise

    # --- the other table in this file ----------------------------------------
    #
    # The effect ledger (`ledger.py`) writes through these two, rather than opening a
    # second connection to the same file. One connection means one lock and one
    # transaction scope, so a ledger write can never sit behind a busy timeout waiting for
    # the journal write it is supposed to follow.

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """`BEGIN IMMEDIATE` … `COMMIT`, or `ROLLBACK` and re-raise.

        Not for journal appends - those go through `append`, which assigns `seq` inside its
        own transaction. This is for the tables that ride along in the same file.
        """
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            self._conn.execute("COMMIT")

    def query(self, sql: str, args: Sequence[Any] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return list(self._conn.execute(sql, tuple(args)).fetchall())

    # --- read ----------------------------------------------------------------

    def _last_seq(self, run_id: str) -> int:
        row = self._conn.execute(
            "SELECT COALESCE(MAX(seq), 0) AS s FROM journal WHERE run_id = ?", (run_id,)
        ).fetchone()
        return int(row["s"])

    def last_seq(self, run_id: str) -> int:
        """0 for a run with no events, which is why seq starts at 1."""
        with self._lock:
            return self._last_seq(run_id)

    def read(self, run_id: str, *, after_seq: int = 0, limit: int | None = None) -> list[Event]:
        sql = "SELECT * FROM journal WHERE run_id = ? AND seq > ? ORDER BY seq"
        args: list[Any] = [run_id, after_seq]
        if limit is not None:
            sql += " LIMIT ?"
            args.append(limit)
        with self._lock:
            return [Event.from_row(r) for r in self._conn.execute(sql, args).fetchall()]

    def read_all(self, *, after_id: int = 0, limit: int | None = None) -> list[Event]:
        """The cross-run feed, ordered by the monotonic `id`. This is 2c's replay cursor."""
        sql = "SELECT * FROM journal WHERE id > ? ORDER BY id"
        args: list[Any] = [after_id]
        if limit is not None:
            sql += " LIMIT ?"
            args.append(limit)
        with self._lock:
            return [Event.from_row(r) for r in self._conn.execute(sql, args).fetchall()]

    def last_id(self, run_id: str | None = None) -> int:
        """The highest feed id in the file, or 0 when it is empty.

        Where a subscriber starts when it wants "everything from now on" rather than the
        whole history. `id` and not `seq`: a feed spans runs, and `seq` only orders within
        one.
        """
        with self._lock:
            if run_id is None:
                row = self._conn.execute(
                    "SELECT COALESCE(MAX(id), 0) AS n FROM journal"
                ).fetchone()
            else:
                row = self._conn.execute(
                    "SELECT COALESCE(MAX(id), 0) AS n FROM journal WHERE run_id = ?", (run_id,)
                ).fetchone()
            return int(row["n"])

    def count(self, run_id: str | None = None) -> int:
        with self._lock:
            return self._count_locked(run_id)

    def _count_locked(self, run_id: str | None = None) -> int:
        if run_id is None:
            row = self._conn.execute("SELECT COUNT(*) AS n FROM journal").fetchone()
        else:
            row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM journal WHERE run_id = ?", (run_id,)
            ).fetchone()
        return int(row["n"])

    def runs(self) -> list[RunInfo]:
        with self._lock:
            return self._runs_locked()

    def _runs_locked(self) -> list[RunInfo]:
        rows = self._conn.execute(
            """
            SELECT run_id, COUNT(*) AS n, MIN(ts) AS first_ts, MAX(ts) AS last_ts,
                   MAX(seq) AS last_seq
              FROM journal GROUP BY run_id ORDER BY MIN(id)
            """
        ).fetchall()
        return [
            RunInfo(run_id=r["run_id"], events=r["n"], first_ts=r["first_ts"],
                    last_ts=r["last_ts"], last_seq=r["last_seq"])
            for r in rows
        ]

    # --- retention -----------------------------------------------------------

    def prune(
        self,
        policy: Retention | None = None,
        *,
        now: datetime | None = None,
        dry_run: bool = False,
    ) -> PruneResult:
        """Apply a retention policy. Under the default policy this touches nothing."""
        policy = policy or Retention()
        conn = self._conn
        with self._lock:
            targets = policy.prunable(self._runs_locked(), now=now)
            removed = self._count_runs(targets)
            if not targets or dry_run:
                return PruneResult(runs=list(targets), events=removed)
            conn.execute("BEGIN IMMEDIATE")
            try:
                for run_id in targets:
                    conn.execute("DELETE FROM journal WHERE run_id = ?", (run_id,))
                    # The ledger is derived from those events, so it goes with them. A run
                    # is entirely present or entirely gone; an effect row whose journal is
                    # gone is a claim with nothing behind it.
                    conn.execute("DELETE FROM effect WHERE run_id = ?", (run_id,))
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise
        return PruneResult(runs=list(targets), events=removed)

    def _count_runs(self, run_ids: Iterable[str]) -> int:
        return sum(self._count_locked(run_id) for run_id in run_ids)
