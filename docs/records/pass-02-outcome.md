# Pass 2 — Durable Run Journal — outcome

Sessions completed: **2a**. Sessions 2b (event coverage) and 2c (frontend subscription) are
not started, so **the pass's exit criteria are not met**: no complete run produces a journal,
because nothing emits events yet, and no frontend replays from one. Three of the five items
under "outcome record must capture" are answered below; the other two belong to 2b and 2c and
are marked open rather than guessed at.

What 2a means in practice: **the journal exists and nothing writes to it.** That is the scope
the session was given, and it is visible in the diff — no file under `src/agentd/agent/`,
`src/agentd/tools/`, `src/agentd/daemon/` or `src/agentd/cli/` was touched, and outside its
own package the string `journal` appears only in `config.py`. The pass's "do not build a
second event path" constraint is satisfied by there being no path at all yet.

---

## Session 2a — Store and writer

### what shipped

| file | what it is | lines |
|---|---|---|
| `src/agentd/journal/store.py` | schema, connection, seq assignment, reads, retention | 407 |
| `src/agentd/journal/writer.py` | the append path: ordering, durability class, buffering | 175 |
| `src/agentd/journal/__init__.py` | exports | 40 |
| `src/agentd/config.py` | `JournalConfig` + one field on `Config` | +30 |
| `config/default.toml` | the `[journal]` block | +12 |
| `tests/test_journal.py` | 51 tests, four of which SIGKILL a real subprocess | 601 |

Suite 511 → 562 passing, no existing test touched. `uv run ruff check .` clean.

The public surface is `JournalStore` (owns the file and the only transaction), `JournalWriter`
(owns ordering and the durability class), `Retention` (the pruning hook), and the value types
`Event`, `PendingEvent`, `RunInfo`, `PruneResult`.

### what deviated from the plan, and why

**The pass file's schema is not literally implementable in SQLite, and the implemented one
has a sixth column.** Three differences, in descending order of how much they matter.

1. **Added `id integer PRIMARY KEY AUTOINCREMENT`.** The plan keys on `(run_id, seq)` alone.
   `UNIQUE (run_id, seq)` preserves that constraint exactly and builds the index every
   per-run read uses, so nothing was given up. The column exists because session 2c replays
   "from a last-seen event id" across a feed that spans runs, and `(run_id, seq)` gives no
   cross-run ordering — without it, 2c has to invent a second ordering, which is the shape of
   the thing this pass exists to prevent. SQLite's implicit `rowid` would have served until
   the first prune: rowids are reused after a delete, so a frontend reconnecting with a saved
   cursor would silently skip live events. `AUTOINCREMENT` is what makes the cursor monotonic
   for the life of the file. `test_the_feed_cursor_never_rewinds_after_a_prune` is named for
   it.
2. **`ts timestamp` and `payload json` are `text`.** SQLite has neither type. `ts` is an
   ISO-8601 UTC string from `ids.utcnow`, chosen so a lexical comparison is a chronological
   one — the retention hook depends on that. `payload` is JSON text with a
   `CHECK (json_valid(payload))` constraint, which is the nearest thing to the declared type
   that the storage can actually enforce.
3. **`seq` enforcement is not only in the writer.** The plan says "writer enforces monotonic
   `seq` per run and rejects out-of-order appends", and it does — but a rule that lives only
   in `JournalWriter` is a rule that holds until something writes without it, and the CLI and
   the daemon are separate processes against one file. The same rule is a `BEFORE INSERT`
   trigger, so a raw `sqlite3` connection cannot get past it either. Enforcing it twice is
   deliberate; see the survivor discussion under *what is now true*.

**Retention got a second mode the plan did not ask for.** The plan says "keep everything,
with a documented pruning hook for later". The default is `keep_everything` and that is what
ships in `config/default.toml`. But `max_age_days` is implemented and tested rather than
sketched, so the first person who needs pruning is switching on a path that has run instead
of writing one from a docstring. It is ~10 lines and two tests. The judgement being recorded:
a "documented hook" that has never executed is a design sketch, and this codebase's
characteristic bug is the plausible-looking path that was never exercised.

**`append()` does not always return a `seq`,** which the plan's framing implies it would. It
returns the written `Event` when the event reached disk during the call and `None` when it was
buffered. This falls directly out of the decision to assign `seq` inside the committing
transaction: a buffered event genuinely has no position yet. Callers that need the number
immediately pass `sync=True`. This is a real ergonomic cost of the durability design and is
recorded rather than smoothed over.

**`worker_id` and `step_id` are payload keys, not columns.** 2b requires them on every event
that has them. They are *named arguments* on `JournalWriter.append` rather than free-form
payload entries, so a later decision to promote either to a column is a migration plus one
module rather than an edit to every call site. The plan does not decide this; 2b may.

**No `async def append`, and no CLI command.** Two coroutines handing appends to a thread
pool would let the pool decide the order of a run's history, so the append path is a
synchronous call from the calling thread. No `agent journal` command was built; 2c surfaces
the journal, and a reader shaped around a store nothing writes to yet is work done twice —
the same reasoning 1a used for not building a telemetry reader.

### what is now true about the code that was not before

- **There is an append-only, per-run ordered event log with a durable write path**, and three
  properties hold that are enforced by storage rather than by convention:
  1. `seq` increases per run, checked in `JournalStore.append` *and* by the trigger.
  2. `seq` is assigned inside the same transaction that reads `MAX(seq)`, under
     `BEGIN IMMEDIATE`. There is no cached cursor anywhere in the module. The tempting
     alternative — cache `next_seq` per run in the writer — is only correct while one process
     writes a run, which is the condition baseline finding 3 already caught this codebase
     violating (one request, two write paths, two open loops).
  3. A killed process loses a **suffix, never a hole**. Appends commit as whole batches, so
     what survives on disk is always a prefix of what happened.
- **`BEGIN IMMEDIATE` rather than a bare `BEGIN` is load-bearing.** Two writers can both read
  `MAX(seq)` under a shared lock and then deadlock on upgrade, and SQLite resolves that by
  failing one of them *at COMMIT* — after it has already been told its append succeeded.
- **A journal write failure raises.** This is the deliberate opposite of `obs/telemetry.py`,
  which counts write failures and carries on. Correct there: a measurement that can take a
  turn down is worse than no measurement. Wrong here: an `effect_intended` that silently
  failed to write *is* the duplicate-side-effect bug. A rejected append leaves the file
  unchanged and the connection outside a transaction; a batch whose flush fails is **not**
  retried on the next flush, because the caller has already been handed the exception and
  quietly writing those events later would journal history the caller was told was lost.
- **`SeqConflict` means a position conflict and nothing else.** The trigger's abort and the
  unique index raise it; any other constraint violation is a malformed event and surfaces as
  `JournalError`, so the next person reading a traceback is not sent hunting for a
  concurrency bug that is not there.
- **Nothing in the runtime changed.** No agent behavior, no prompts, no tool surface, no
  second event path. `Config` gained a `journal` field and `config/default.toml` gained a
  block.

**The suite was mutation-checked rather than trusted for being green** (22 runs, 19 distinct
valid breaks). It caught a defect that had already passed `ruff check` and `pytest`:
`JournalStore` held no lock while each `JournalWriter` held its own — fine for one writer per
store, which is not the daemon's shape. Four writers sharing a connection failed instantly
with "cannot start a transaction within a transaction". Serializing moved to the store, with
the connection.

Three mutations survived the first round, and they were three different things:

- **A real gap.** Removing the `ROLLBACK` from the rejected-batch path survived because an
  earlier guard made the path unreachable from any test: the in-Python `seq` check fires
  first, so nothing ever reached the storage-level rejection. Reaching it requires being a
  writer working from a stale `MAX(seq)` — the second process. Three tests now stage that by
  monkeypatching `_last_seq`, and they cover the rollback, batch atomicity, and the
  `SeqConflict`/`JournalError` split.
- **An invalid mutation.** "Drop the monotonic trigger" survived because renaming a SQLite
  trigger does not disable it. Re-run properly (abort replaced with `SELECT 1`; trigger
  re-pointed at `AFTER DELETE`) it is caught both ways.
- **Legitimate redundancy, left in place.** Removing the in-Python `seq` check still survives,
  because with the trigger present the mutation is observationally identical — same exception
  type, same atomicity. The check is the fast path with the precise message; the trigger is
  the guarantee that binds writers this module never sees. Recorded here rather than papered
  over with a brittle assertion on an error string.

### schemas as actually implemented

```sql
CREATE TABLE journal (
  id      integer PRIMARY KEY AUTOINCREMENT,
  run_id  text    NOT NULL,
  seq     integer NOT NULL CHECK (seq > 0),
  ts      text    NOT NULL,                        -- ISO-8601 UTC, from ids.utcnow
  type    text    NOT NULL,
  payload text    NOT NULL CHECK (json_valid(payload)),
  UNIQUE (run_id, seq)
);

CREATE TRIGGER journal_seq_monotonic
BEFORE INSERT ON journal
WHEN NEW.seq <= COALESCE((SELECT MAX(seq) FROM journal WHERE run_id = NEW.run_id), 0)
BEGIN
  SELECT RAISE(ABORT, 'journal: seq must increase within a run');
END;
```

One SQLite file at `<paths.data_dir>/journal.db` (WAL puts `-wal` and `-shm` beside it),
`PRAGMA user_version = 1`, `journal_mode = WAL`, `synchronous = FULL`, `busy_timeout = 5000`.

`seq` is monotonic, **not contiguous**: a gap is legal, going backwards is not. In practice
auto-assigned seqs are contiguous, because a rolled-back transaction leaves no gap, and the
kill tests assert contiguity for that reason.

Payload is serialized `sort_keys=True, ensure_ascii=False, separators=(",", ":")` — the same
event serializes identically every time, which Pass 3 will want when it hashes canonical
arguments into idempotency keys.

**Which events are synchronous on the write path, and which are buffered:**

| class | rule | why |
|---|---|---|
| synchronous | `effect_*`, `checkpoint_written` | below |
| buffered | everything else, subject to `[journal] buffering` | |

`writer.is_synchronous(event_type)` is the single definition, backed by `SYNC_PREFIXES =
("effect_",)` and `SYNC_TYPES = {"checkpoint_written"}`. 2b extends those rather than
reclassifying at call sites.

`effect_*` is synchronous because Pass 3's ledger is a promise that the record precedes the
action: the whole point of `effect_intended` is to be on disk *before* the side effect runs,
so a crash in between is detectable afterwards. `checkpoint_written` is synchronous because a
checkpoint may lag the journal but may never disagree with it, and a checkpoint whose own
announcement was lost is a snapshot nothing points at.

**Ordering is call order, not flush order.** A buffered event still in memory when a
synchronous event arrives is flushed *in the same transaction, ahead of it*. An effect can
never overtake the events that led to it. The buffer flushes on: a synchronous event,
`buffer_max` (32) events, a tail older than `buffer_max_age_s` (1.0s), an explicit `flush()`,
or `close()`. The age check runs **on append, not on a timer** — there is no background
thread — so a quiet process holds its tail until something else happens.

**Retention and pruning policy as implemented:** the initial and default policy is
`keep_everything`. The hook is
`Retention.prunable(runs: Sequence[RunInfo], *, now) -> list[str]`; it receives runs, not
events, and returns run ids. `RunInfo` is `(run_id, events, first_ts, last_ts, last_seq)`.

**Pruning is whole-run only, and that is an invariant rather than an implementation
shortcut.** Deleting a prefix or a suffix of one run leaves a journal that still folds without
error and folds to *the wrong state* — a silently wrong answer, which is this codebase's
characteristic failure mode and the one it is least able to detect. A run is either entirely
present or entirely gone. The second mode, `max_age_days`, judges a run by its **last** event,
so a long-running run is never cut in half.

**Config, as it now exists:**

```toml
[journal]
# path = "~/.local/share/agent/journal.db"   # unset means exactly this
synchronous = "FULL"       # fsync every commit; NORMAL survives a kill but not a power cut
buffering = true           # effect_* and checkpoint_written ignore this and always go now
buffer_max = 32
buffer_max_age_s = 1.0
retention = "keep_everything"    # the other mode is "max_age_days"; pruning drops whole runs
# retention_max_age_days = 90
```

There is **no `enabled` flag**, unlike `[telemetry]`. The journal is the source of truth for
execution state, so a switch that turns it off is a switch that makes checkpoints, resume and
the effect ledger silently wrong. A journal that cannot be written is a fatal condition, not
a disabled feature.

### deferred items, and where they went

- **The event vocabulary and every payload shape — session 2b.** This is the single largest
  thing this record does *not* contain, and the pass explicitly wants the last six of its
  list — `message_appended`, `checkpoint_written`, `effect_intended`, `effect_committed`,
  `run_resumed`, `run_forked`, which Passes 3, 4 and 5 write — specified in 2b so those
  passes do not re-litigate the schema. 2a constrains them only to "a JSON object". What 2a
  did settle and 2b inherits: the durability classification is centralized in
  `writer.is_synchronous`, and `worker_id` / `step_id` already have named arguments.
- **Frontend replay mechanism — session 2c.** The substrate is
  `JournalStore.read_all(after_id=...)`, and `id` is monotonic for the life of the file
  including across prunes. The mechanism itself — transport, subscription, reconnect
  handshake — does not exist. The pre-existing in-memory event bus has not been located or
  removed; that is 2c's "do not leave it running alongside", and it is untouched.
- **Anything that reads the journal for recovery** — Pass 4, per the pass's *Must not*.
  Nothing folds it, nothing resumes from it, and `checkpoint_written` is a string in a
  `frozenset` and nothing more.
- **`agent journal` CLI.** Not built; see deviations.
- **The journal is not in `backup.create`,** which covers Postgres and the memory repo. Per
  the vocabulary in `CLAUDE.md` that is correct rather than an oversight: the journal is
  execution state, which is run-scoped and disposable, while memory is what is user-scoped
  and durable. Recorded so it is a decision rather than a gap.

### open questions for later passes

**1. A turn that exhausts its step budget still crashes, and 2b hits it on contact. → 2b.**
Unchanged from Pass 1 (open question 7, baseline finding 1): `agent/loop.py:213` appends a
`system` message to the end of the message list on the last step, and this backend rejects any
system message that is not first. Three of twenty-two graded baseline rows died there. That
turn must still produce `agent_finished`, or 2b's exit criterion — "the sequence of what
happened is readable without reference to any other source" — is false for precisely the case
the durability spine exists to handle. This is the first thing 2b should check, not something
to discover after the vocabulary is wired.

**2. Power-loss durability is asserted, not demonstrated.** The kill tests prove
process-death durability: four of them SIGKILL a real subprocess, reopen the file, and assert
contiguous `seq` from 1, intact payloads, `PRAGMA integrity_check = ok`, and a run that
continues rather than restarting. `synchronous = NORMAL` would pass every one of them, because
a SIGKILLed process leaves its data in the OS page cache. `FULL` is a reasoned default for the
effect ledger's sake, not a measured one, and nothing here has been tested against a real
power interruption.

**3. The fsync cost is unmeasured, and Pass 10 is committed to reporting it.** Phase 10 lists
"checkpoint write overhead (latency and storage)". Nobody has timed an `effect_*` append on
this hardware, so the buffering defaults (32 events / 1.0s) are guesses chosen to be
obviously-safe rather than tuned. Pass 11 lists journal retention as a thing to revisit; this
belongs next to it.

**4. A synchronous append blocks the event loop, and 2b is where that lands. → 2b.** The
append path is a synchronous call by design — a thread pool would reorder a run's history —
but the runtime is asyncio, so an fsync happens on the loop thread. Unmeasured, probably
sub-millisecond, and worth knowing before someone wraps it in `asyncio.to_thread` and
silently breaks ordering. If it does need offloading, it needs a single dedicated writer
thread with a queue, not a pool.

**5. A quiet process holds its buffered tail indefinitely, which is a liveness question for
2c.** There is no timer thread; the age check only runs on the next append. 2b must call
`flush()` at turn end. 2c must not assume the tail is on disk, or a frontend will appear to
stall at the end of a run that has genuinely stopped producing events.

**6. `_migrate` cannot actually migrate. → whichever pass first needs schema v2.** It runs
`CREATE TABLE / TRIGGER IF NOT EXISTS` and bumps `user_version` to 1, and refuses to open a
file from the future (`SchemaTooNew`). On an existing v1 file, a v2 `SCHEMA` string with an
added column is a silent no-op — `IF NOT EXISTS` sees the table and does nothing, and the
version is then bumped anyway. The first change to this schema must add a real versioned
upgrade step. Likely triggers: promoting `worker_id` / `step_id` to columns (2b), or anything
Pass 3's effect ledger wants indexed.

**7. Retention has two extension points nobody can use yet. → Passes 3 and 4.** `prunable`
receives `RunInfo`, so Pass 3 can refuse to prune a run holding an uncommitted effect and
Pass 4 can require that a checkpoint exist first. Neither concept exists, so neither was
guessed at. A retention policy that deletes a run whose `unsafe_write` was never reconciled
destroys the only record that it might have happened.

**8. Still open from Pass 1, untouched by this session:** token accounting (the SIR router
drops the usage chunk, so `usage.reported` is `false` on every turn), and the approval-wall
question for Passes 6 and 8a. Both block what they blocked.
