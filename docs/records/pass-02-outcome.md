# Pass 2 — Durable Run Journal — outcome

Sessions completed: **2a**, **2b**. Session 2c (frontend subscription) is not started, so
**the pass's exit criteria are not met** as a whole: no frontend replays from the journal, and
the kill-mid-run exercise has been done at the store level (2a) but not against a real turn.
2b's own exit criterion — "a complete run produces a journal from which the sequence of what
happened is readable without reference to any other source" — is met, and is asserted as a
sequence rather than a set in `tests/test_journal_events.py`.

Four of the five items under "outcome record must capture" are now answered: the schema (2a),
the full event list with payload shapes (2b), the synchronous/buffered split (2a, extended
once by 2b), and retention (2a). The frontend replay mechanism belongs to 2c and is still
marked open rather than guessed at.

What 2a meant in practice: **the journal existed and nothing wrote to it.** 2b is the session
that connected it — one path, through `RunJournal`, from `agent/loop.py` and
`agent/subagents.py`. The pre-existing in-process `UIEvent` stream is untouched and still
feeds the CLI and Telegram; removing it is 2c's job, and until then the two coexist by the
pass's own sequencing rather than by accident.

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

---

## Session 2b — Event coverage

### what shipped

| file | what it is | lines |
|---|---|---|
| `src/agentd/journal/events.py` | the vocabulary: 17 types, their payload shapes, the validator | 431 |
| `src/agentd/journal/runtime.py` | the process's writer, and `RunJournal` — the door the runtime writes through | 129 |
| `src/agentd/journal/writer.py` | type checking on append; `agent_finished` added to `SYNC_TYPES` | +18 |
| `src/agentd/agent/loop.py` | `_TurnRecord` and nine emission points | +226 |
| `src/agentd/agent/subagents.py` | `worker_created` / `worker_finished`, run and worker plumbing | +47 |
| `src/agentd/tools/base.py` | `ToolContext.run_id`, `ToolContext.step_id` | +7 |
| `src/agentd/tools/executor.py` | a denial carries its `rule` and `queued_id` on the result | +14 |
| `src/agentd/tools/builtin_delegate.py` | threads the run and the step into the worker | +2 |
| `tests/conftest.py` | journal writer in the monkeypatch list, and closed at teardown | +6 |
| `tests/test_journal_events.py` | 20 tests | 526 |

Suite 562 → 582 passing, no existing test modified. `ruff check src tests scripts` clean.

**Nine of the seventeen types are emitted today.** `agent_started`, `agent_finished`,
`message_appended`, `tool_requested`, `tool_started`, `tool_finished`, `tool_failed`,
`worker_created`, `worker_finished`. The set is exported as `journal.EMITTED_TYPES` and
asserted in a test, so the gap between the vocabulary and what is reachable is data rather
than a paragraph that goes stale.

**Eight are defined and written by nobody:** `tool_progress`, `handoff_started`,
`handoff_finished`, `checkpoint_written`, `effect_intended`, `effect_committed`,
`run_resumed`, `run_forked`. Each is *constructed* once in
`test_the_types_later_passes_write_already_have_a_shape_that_validates`, so the later passes
inherit a shape something has actually built.

### what deviated from the plan, and why

**The pass file says "the last six" are written by Passes 3, 4 and 5. The real number is
eight, and the membership differs.** `handoff_started` / `handoff_finished` are Pass 5's and
cannot be written now either — the pass file's own list puts them ahead of the six and then
does not count them. Going the other way, **`message_appended` is in the pass's "last six"
and is emitted now**, because 2b's exit criterion is that the sequence of what happened is
readable without another source, and a journal that shows tool calls but never the messages
that caused them does not meet it. So: the vocabulary is complete as specified, but the
defined-but-unwritten set is `{tool_progress, handoff_*, checkpoint_written, effect_*,
run_*}`, not the six the plan names.

**`tool_progress` is defined and not wired, and that was a *Must not* call.** A tool handler
has no channel to report progress on. Giving it one means changing `Handler` or `ToolResult`,
which is "change the tool surface". The shape is specified (`call_id`, `name`, `message`,
optional `pct`) and the slot is empty.

**Payload validation is not in `JournalWriter.append`; the event *type* check is.** Two
levels, deliberately. The type is checked at the one door every append goes through, because
a type `reduce` has no case for is a journal that folds to the wrong state rather than to an
error. The payload is checked one level up, in `RunJournal.emit`, because 2a's durability
tests append deliberately arbitrary payloads (`{"i": 0}`, `{}`) to prove that a killed
process loses a suffix and not a hole — making those schema-correct would have tested the
schema instead of the kill. The cost is honest: a caller who goes to `JournalWriter` directly
gets its type checked and its payload not.

**`agent_finished` was added to `SYNC_TYPES` instead of calling `flush()` at turn end.** 2a's
open question 5 asked 2b to call `flush()`. Classifying the turn's terminal event as
synchronous is strictly stronger and cannot be forgotten by a future call site: a synchronous
append carries the buffered tail in the same transaction, so one fsync per turn puts the whole
turn on disk, in call order. This changes 2a's durability table by one row.

**A worker does not get a run of its own.** The alternative — a sub-agent opens a new run and
the parent references it — was rejected: a fold of the caller's run would then show a gap
where the delegation happened, and `run_forked` would have had to mean two different things.
A worker's events are in its caller's run with `worker_id` set, bracketed by `worker_created`
and `worker_finished`. `run_id` for a top-level turn is `str(turn_id)`; `run_subagent` falls
back to `str(parent_turn_id)` when no `parent_run_id` is threaded, which is the same
derivation and therefore lands in the run that turn opened rather than in an orphan.

**`ToolContext` gained `run_id` and `step_id`.** Not free-form `ctx.extra` keys: Pass 3's
idempotency key is `hash(run_id, step_id, tool_name, canonical_args)`, so both are things a
handler is entitled to know, and `extra` would have made them optional by convention. They
are `None` only where there genuinely is no run — `policy/replay.execute_approved` runs a
queued call long after its turn ended.

**A denial now carries `rule` and `queued_id` on `ToolResult.data`.** The information was
already in the JSON body the model reads back, and `tool_failed` would otherwise have had to
re-parse a string this process had just serialized. That is precisely the shape that becomes
a quietly-null field the first time the body changes.

**`_TurnRecord` is a context manager, not a pair of calls.** Because "every way out of a
turn" includes the one nobody writes code for: a consumer that stops iterating gets
`GeneratorExit` thrown at the suspended `yield`, and in that case neither the telemetry record
nor the `actions` row is written at all (Pass 1, open question 4). A `with` block unwinds
there, so the run still ends with `agent_finished` — `status: "cancelled"`.

### what is now true about the code that was not before

- **A run has one event path and it is the journal.** Everything emitted goes through
  `RunJournal.emit` → `JournalWriter.append` → `JournalStore.append`. There is no second
  store, no queue, no background thread. The `UIEvent` generator still exists and still
  renders the CLI; it is 2c's to remove, and nothing was built alongside it.
- **A turn cannot end silently.** Four exits — a normal finish, budget exhaustion, an
  `LLMError`, and a consumer that walked away — all produce `agent_finished`, with
  `completed` / `abandoned` / `failed` / `cancelled` respectively. `status` starts as `None`
  and every deliberate exit sets it; `None` at `__exit__` is recorded as `cancelled` rather
  than defaulted to `completed`.
- **The Pass 1 budget-exhaustion crash is now legible in the journal, and is otherwise
  untouched.** `loop.py` still appends `FINAL_NUDGE` as a trailing `system` message on the
  last step and this backend still rejects it. What the journal now shows is
  `message_appended(role=system)` followed by `agent_finished(status="failed", error="HTTP
  400 System message must be at the beginning.")` — so the turn that 2a's open question 1
  worried would produce nothing produces a complete, readable sequence.
  `test_a_turn_that_the_model_kills_still_says_how_it_ended` is named for it. Fixing the
  underlying bug is a behaviour change and was left alone.
- **An unmeasured turn is not journaled as a free one.** `agent_finished.usage_reported` is
  `False` whenever the router dropped the usage chunk, which is every streamed turn today.
  Without it the zeros would read as a measurement.
- **The schema refuses the house bug.** A missing required field, a `None` in a field not
  declared nullable, a misspelled key, a `bool` where a count is declared, and a string
  outside its enum all raise `EventSchemaError` before anything reaches disk. `nullable` is
  separate from `required` on purpose: "resumed with no checkpoint behind it" is a statement,
  and an absent key is not.
- **One writer per process per file.** `journal.runtime.get_writer` caches by resolved path.
  A sub-agent builds a second `AgentLoop` inside its caller's turn, and two writers on one
  file would race each other for `seq` positions in the run they are both writing.

**The suite was mutation-checked rather than trusted for being green.** Eight mutations, all
caught: drop the `cancelled` path; accept an unannounced `None`; make `step_id` ignore the
worker; give a worker its own run; remove the type check from `append`; move `agent_finished`
back to buffered; hardcode `usage_reported = True`; drop the denial rule. One observation
worth recording: with `step_id` mutated, the worker-run test still passed because it compares
against `jevents.step_id(...)` — the collision test is the one that bites, and it asserts the
strings literally for that reason.

### schemas as actually implemented

Every event carries `run_id`, `seq`, `ts` and `type` as columns (2a's schema, unchanged — no
migration, and `SCHEMA_VERSION` is still 1). `worker_id` and `step_id` remain payload keys
fed by named arguments on `append`; **neither was promoted to a column**, so 2a's open
question 6 (`_migrate` cannot migrate) has not been triggered yet.

Notation: `?` = optional, `| null` = explicitly nullable, `∈ {}` = enum.

```
agent_started      session_id, turn_id, role, actor, origin, channel, autonomy, model: str
                   max_steps, input_chars: int; input_preview: str
                   parent_turn_id: str | null      (required — a top-level turn states None)
agent_finished     turn_id: str; status ∈ {completed, abandoned, failed, cancelled}
                   steps, duration_ms, answer_chars: int; usage: dict; usage_reported: bool
                   answer_preview?: str; error?: str | null
tool_requested     call_id, name: str; args: dict; visible, known: bool
tool_started       call_id, name: str
tool_progress      call_id, name, message: str; pct?: int | float | null      (unwritten)
tool_finished      call_id, name: str; duration_ms, result_chars: int
                   trust ∈ {trusted, untrusted}; summary?: str; private?, has_undo?: bool
tool_failed        call_id, name, error: str; duration_ms: int
                   denied, invalid_args: bool; attempt?: int
                   rule?: str | null; queued_id?: str | null
worker_created     worker_id, name, role, autonomy, task_preview: str
                   max_steps, task_chars: int; tools: list; parent_step_id?: str | null
worker_finished    worker_id, name: str; status ∈ {ok, partial, failed, budget_exhausted}
                   summary_chars, artifacts, citations, candidates, tokens, duration_ms: int
                   tainted: bool; summary_preview?: str
message_appended   role ∈ {user, assistant, tool, system}; actor, preview: str; chars: int
                   trust? ∈ {trusted, untrusted}; private?: bool
                   tool_call_id?: str | null; tool_calls?: int
checkpoint_written checkpoint_id: str; trigger ∈ {turn_end, worker_finished, pre_effect,
                   handoff, manual}; covers_seq: int; memory_watermark: dict
                   messages?, bytes?, duration_ms?: int; location?: str | null   (unwritten)
effect_intended    effect_id, step_id, tool_name, idempotency_key, args_digest: str
                   effect_class ∈ {read, idempotent_write, unsafe_write}
                   attempt?: int                                                 (unwritten)
effect_committed   effect_id, step_id, idempotency_key: str
                   status ∈ {committed, failed, uncertain}; duration_ms: int
                   result_digest?: str | null; error?: str | null; attempt?: int (unwritten)
run_resumed        from_seq, replayed_events: int; checkpoint_id: str | null
                   reason: str; uncertain_effects: list                          (unwritten)
run_forked         parent_run_id, reason: str; fork_point_seq: int
                   checkpoint_id?: str | null; handoff_id?: str | null           (unwritten)
handoff_started    handoff_id, reason: str; messages: int
                   context_tokens?, ceiling_tokens?: int                         (unwritten)
handoff_finished   handoff_id: str; status ∈ {ok, failed}
                   summary_chars, kept_messages, dropped_messages, duration_ms: int
                   error?: str | null; successor_run_id?: str | null             (unwritten)
```

**Decisions inside those shapes that later passes should not re-litigate:**

- `checkpoint_written.covers_seq` is the last journal position the snapshot accounts for,
  always lower than the checkpoint event's own `seq`. Named for what it means so nobody reads
  it as the checkpoint's position: "a checkpoint may lag the journal" is exactly this number.
- `effect_intended` and `effect_committed` **require `step_id`**, because the idempotency key
  is `hash(run_id, step_id, tool_name, canonical_args)` and a key computed from a missing step
  collides across steps.
- `effect_committed.status` includes `uncertain`, which is what an `unsafe_write` interrupted
  between intent and commit surfaces as. It is never retried automatically.
- `run_resumed.checkpoint_id` is required **and** nullable: folding a whole journal from seq 0
  is a legitimate resume and has to be stated.
- `run_forked` is emitted as the **first event of the new run**, naming the parent. The parent
  run gets no event — a completed run's journal must not keep growing.
- Enums were imposed only where `CLAUDE.md` / the architecture doc already fix the values
  (effect classes, checkpoint triggers, effect statuses). `handoff_started.reason` and
  `run_forked.reason` are free strings, because an enum invented here for an unwritten pass is
  a constraint that pass would have to migrate away from.
- `message_appended` covers the messages no other event describes — the user's message, the
  assembled system block, each step's assistant output, and the mid-turn system nudges. **A
  tool result is deliberately not one of them**: `tool_finished` / `tool_failed` already carry
  its size, trust and summary, and emitting both would put the same body in the journal twice
  under two names. A Pass 4 fold that wants the message list must read the tool events too.
- Previews are truncated to 200 characters and whitespace-collapsed (`journal.events.preview`).
  The journal is a record of what happened, not a second copy of the conversation; the archive
  in Postgres holds the content.

**The sequence a complete one-tool turn produces**, verbatim from
`test_a_complete_turn_is_readable_from_the_journal_alone`:

```
seq 1  agent_started
seq 2  message_appended   role=user
seq 3  message_appended   role=system     (the assembled prompt, incl. the memory block)
seq 4  message_appended   role=assistant  step_id=s1  tool_calls=1
seq 5  tool_requested     step_id=s1      visible=true known=true
seq 6  tool_started       step_id=s1
seq 7  tool_finished      step_id=s1      trust=trusted
seq 8  message_appended   role=assistant  step_id=s2
seq 9  agent_finished     status=completed steps=2
```

A delegating turn interleaves `worker_created` … the worker's own `agent_started` … its tool
events … `agent_finished` … `worker_finished`, all in the same run, every one of them tagged
`worker_id`, with the worker's steps scoped as `<worker_id>.s<N>` so they cannot collide with
the caller's `s<N>`.

**Durability classes, as amended:** `effect_*` and `checkpoint_written` (2a) plus
`agent_finished` (2b) are synchronous; everything else is buffered subject to `[journal]`.
`writer.is_synchronous` is still the single definition.

### deferred items, and where they went

- **Frontend replay — session 2c.** Unchanged from 2a: the substrate is
  `JournalStore.read_all(after_id=...)`. The in-memory `UIEvent` stream that 2c must remove is
  `agent/events.py` plus its consumers in `cli/chat.py`, `daemon/telegram.py`,
  `daemon/scheduler.py` and `daemon/heartbeat.py`; 2b left every one of them working
  unchanged, because removing them is 2c's exit criterion and doing it here would have left
  the CLI blind for a session.
- **`tool_progress` needs a progress channel on the tool surface.** Not built; *Must not*.
- **Anything that reads the journal** — Pass 4. Nothing folds it. `EMITTED_TYPES` is the list
  a `reduce` will have to handle first.
- **No `agent journal` CLI**, still. 2c surfaces the journal.
- **`worker_id` / `step_id` are still payload keys.** Promoting them to columns is the
  migration that will first hit 2a's open question 6.

### open questions for later passes

**1. Nothing has run a live turn against this.** Every assertion here is from the suite; the
journal file at `~/.local/share/agent/journal.db` does not exist, because no live turn has
been journaled yet. The suite covers the shapes, but a live run is what would expose a value
this code assumes is a `str` and the provider returns as something else. **Run one before 2c
builds a feed on top of it**, and count the payload fields — the `tool_finished.trust` and
`ToolCall.id` paths were read by hand and are the two most likely to surprise.

**2. A tool call outside a turn is journaled by nobody. → Pass 3.**
`policy/replay.execute_approved` runs a queued approval long after its turn ended, through the
executor, with `ctx.run_id = None`. It writes an `actions` row and no journal event. That is
the *same shape* as baseline finding 3 (an effect with no journal entry cannot be folded), and
it is where Pass 3's effect ledger has to decide whether a queued call rejoins its original run
or opens one.

**3. The tool events are emitted by `loop.py`, not by the executor.** The executor is the
chokepoint every tool call routes through, but it also serves MCP and the approval queue,
which have no run and no writer. The loop has the run, the step, the visibility and the denial
in one place, so that is where they are emitted. If Pass 3 wants `effect_intended` to be
unbypassable, it has to move the emission into the executor and hand the executor a
`RunJournal` — and at that point the events above should move with it rather than being
emitted twice.

**4. A synchronous append still happens on the event loop thread.** Unchanged from 2a's open
question 4, and now real: `agent_finished` fsyncs on the loop thread once per turn, and Pass 3
will add two `effect_*` fsyncs per write. Still unmeasured. If it ever needs offloading it
needs one dedicated writer thread with a queue, never a pool — a pool would decide the order
of a run's history.

**5. The journal now holds 200-character previews of message and tool content, in plaintext,
in `data_dir`.** Less than the Postgres archive already holds, on the same machine, so this is
not a new exposure — but it is a new *location*, and it is not covered by `backup.create` (2a
recorded why) and has no retention pressure under `keep_everything`. Whoever first exports or
ships a journal off-host needs to look at `message_appended.preview`,
`tool_finished.summary`, `tool_failed.error` and `agent_started.input_preview` first. The
`private` flag on `message_appended` and `tool_finished` is there so a redactor has something
to key on.

**6. `agent_finished.status` has four values and telemetry has three.** They agree on
`completed` / `abandoned` / `failed`; `cancelled` exists only in the journal, because the
telemetry record is the thing that is never written in that case. A Pass 10 comparison joining
the two records must not treat a missing telemetry row as a missing turn.

**7. Still open from 2a, untouched by 2b:** power-loss durability is reasoned rather than
measured (2a #2); the fsync cost is unmeasured (2a #3); `_migrate` cannot migrate (2a #6);
retention's two extension points are still unusable (2a #7). And from Pass 1: token accounting
is still broken upstream, which is why `usage_reported` exists.
