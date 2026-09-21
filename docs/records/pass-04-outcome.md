# Pass 4 — Checkpoints, Resume, Fork — outcome

Sessions completed: **4a**, **4b**. 4c (uncertain status, a human hard stop) and 4d (fork)
are untouched, so the pass's exit criteria are **partly** met: a run can be folded back out
of the journal, a process killed at any of the five boundaries resumes, and an interrupted
`unsafe_write` is closed as `uncertain` and never re-run. What is missing is the half a user
sees — `uncertain` is a status in the journal and in a CLI report, not yet an observation the
orchestrator acts on and phrases (4c) — and fork (4d). Checkpoints are still **off in the
shipped config**, and after 4b that is a cost decision rather than a blocker: resume folds
the journal and reads a snapshot only when there is one.

The pass's precondition held: 3c/3d plus the two rulings Dylan settled at the Pass 3/4
boundary mean `UNAUDITED_TOOLS` is empty, so `pre_effect` fires on tools whose class somebody
judged rather than on every tool in the registry. That was 3a's open question 1 and it is
closed.

---

## Session 4a — Checkpoint record and boundaries

### what shipped

| file | what it is | lines |
|---|---|---|
| `src/agentd/journal/checkpoints.py` | the record, the snapshot derivation, the five triggers, the flag | 627 |
| `src/agentd/journal/store.py` | schema v3 (the `checkpoint` table); prune takes checkpoints too | +46 −1 |
| `src/agentd/journal/events.py` | `checkpoint_written` joins `EMITTED_TYPES`; `memory_watermark` nullable | +21 −9 |
| `src/agentd/journal/__init__.py` | exports | +27 −2 |
| `src/agentd/journal/render.py` | a human line for `checkpoint_written` | +12 −3 |
| `src/agentd/agent/loop.py` | the `turn_end` boundary, inside `_TurnRecord.__exit__` | +21 −2 |
| `src/agentd/agent/subagents.py` | the `worker_finished` boundary | +5 |
| `src/agentd/tools/executor.py` | the `pre_effect` boundary; `_resolve_ledger` | +27 −3 |
| `src/agentd/config.py` | `CheckpointsConfig` + one field on `Config` | +21 |
| `config/default.toml` | the `[checkpoints]` block | +8 |
| `src/agentd/cli/app.py` | `agent journal checkpoints` and `agent journal mark` | +50 |
| `scripts/bench_checkpoints.py` | the overhead harness that produced the numbers below | 118 |
| `tests/test_checkpoints.py` | 21 tests | 527 |
| `tests/conftest.py` | `agentd.journal.checkpoints` in the monkeypatch list | +1 −1 |
| `tests/test_effect_ledger.py`, `tests/test_journal_events.py` | two assertions this session moved | +7 −3 |

Suite 661 → 682 passing. `.venv/bin/ruff check src tests scripts` clean. No tool was added,
removed or re-classified; with the flag off, a turn journals exactly what it journaled before
this session, and that is asserted rather than assumed
(`test_nothing_is_checkpointed_while_the_feature_is_off`).

### what deviated from the plan, and why

**1. The plan's `seq` is implemented as two columns, `covers_seq` and `event_seq`.** The
pass file's sketch has one. Session 2b had already named the event's field `covers_seq` —
"the last journal position the snapshot accounts for, always lower than the checkpoint
event's own seq" — and a record carrying a bare `seq` next to an event carrying `covers_seq`
is two names for one number plus an invitation to read it as the other one. Keeping both
makes the governing invariant a storage constraint: `CHECK (event_seq > covers_seq)`. A
checkpoint may lag the journal; it can now never claim a position the journal has not
reached, whatever future code does the writing.

**2. `open_workers[]` is the detector for the *Must not*, not just a field.** The plan
forbids mid-worker checkpoints and lists `open_workers[]` as a field. Rather than threading
"am I inside a worker" through the loop, the executor and `run_subagent` — which would have
meant a new `ToolContext` field and three call sites that could disagree — the snapshot
derives open workers from the run's own `worker_created` / `worker_finished` events, and
**refuses to write when any worker is open**. One rule, derived from the journal, covers all
three live boundaries: a worker's own `turn_end`, an `unsafe_write` made *by* a worker, and a
nested delegation's `worker_finished`.

The honest consequence: **every checkpoint written today has `open_workers == []`**, because
delegation is awaited inside a step, so no worker is ever open at a boundary. The field is
computed, not defaulted — the day Pass 6 runs workers in parallel it already knows — but
nobody should read "the slot is exercised" into a list that is currently always empty.

**3. `memory_watermark` in the event became required-*and*-nullable.** 2b declared it
`req(dict)`. Pass 7 owns the watermark, so 4a would have had to write `{}` — an empty object
that reads as a measured zero and is indistinguishable from a real watermark of two zeros.
That is the house bug in a field that is meant to be inert. One-word change to the field
spec, the same shape as `run_resumed.checkpoint_id`; the vocabulary is still seventeen types
and the drift guard is untouched.

**4. `orchestrator_id` is the turn that opened the run, derived from the journal.** The
architecture lists `orchestrator_id` beside `run_id` and `worker_id` in observability and
never says what it is made of. Deriving it in one place (the run's first `agent_started` with
no `worker_id`) rather than passing it in from each boundary means the three call sites
cannot disagree, and it is what lets `agent journal mark` checkpoint a run from a process
that holds none of its state. A run with no such event — session 3b's `detached:<action_id>`
runs — raises `NoOrchestrator` rather than being given a placeholder.

On the live journal this is `run_id` for the channels that derive the run id from the turn,
and genuinely different for the four `kill-*` runs 2c named before their turns existed. 2c's
open question 2 ("whoever needs to subscribe first names the run") is therefore *not* closed
but is now survivable: both identities are recorded and they are allowed to differ.

**5. `effects_cursor` is a journal position plus a ledger reading, and says which is which.**
`last_effect_seq` is the seq of the last `effect_*` event at or before `covers_seq` — a
position that re-folds. `effects` and `open_keys` come from the `effect` table, which has no
position of its own (a second attempt updates the row it already had), so they are as of the
instant the snapshot was taken. Written down in the dataclass docstring rather than blurred
into one word that implies more than it has.

**6. Three of the five boundaries have a live producer; `handoff` has none and `manual` got
a CLI.** `handoff` cannot fire before Pass 5 builds the path that produces a handoff — the
trigger is accepted, tested and unproduced. `manual` would have been the same, so
`agent journal mark <run>` exists: it is the architecture's "user-requested marker", it is
the only writer a human can reach, and it exercises the from-outside-the-process path 4b's
resume will also need. So the exit criterion "a normal run produces checkpoints at every
boundary" is met only for the boundaries a normal run *has*: `pre_effect` and `turn_end` on a
turn that writes, `worker_finished` on a turn that delegates.

**7. `write()` flushes the journal buffer first, which costs an extra commit.** Without it
the snapshot would be derived from in-memory state that includes events still in the buffer,
while `covers_seq` excluded them — and because a synchronous append carries the buffer into
its own transaction, those events would land *below* the checkpoint's own seq and *above*
`covers_seq`. A resume would replay events the snapshot had already accounted for. One flush
buys the property that everything the snapshot claims is on disk at or before `covers_seq`.
Measured below; it is not the expensive part.

**8. The turn_end boundary is inside `_TurnRecord.__exit__`, and it can raise during an
unwind.** It is there because "every way out of a turn" includes the consumer that walks away
(2b's reason for making `_TurnRecord` a context manager), and a checkpoint written only on
the happy path is one that is missing from exactly the runs a resume is asked about. The cost
is stated rather than hidden: if the checkpoint write itself fails while the turn is failing,
the checkpoint's exception replaces the turn's. `agent_finished` is already on disk by then —
it is what `covers_seq` covers — so the journal is intact either way, and a boundary that
cannot write is not something to learn about on the next crash.

### what is now true about the code that was not before

- **A run can be snapshotted, and the snapshot cannot outrun the log it summarises.** Three
  properties are enforced by storage rather than by this module: `event_seq > covers_seq`, a
  trigger outside the vocabulary is refused, and the JSON columns must be valid JSON.
- **Nothing conversational is copied.** `messages_ref` is `(run_id, through_seq,
  message_events)`. `test_a_checkpoint_points_at_the_conversation_instead_of_copying_it` puts
  a sentence in the user's message and asserts it is absent from every column of the row.
  `message_events` is named for what it counts: 2b decided a tool result is a `tool_finished`
  and not a `message_appended`, so rehydration needs both types and this number is not "how
  many messages there were".
- **The journal is written first and the row second**, as in `ledger.py`. A crash between
  them leaves an announcement with no row, which re-folding can rebuild; the other order
  would leave a row nothing announced.
- **Workers are atomic, and the rule is derived.** A checkpoint is refused whenever a worker
  is open — asserted end to end in
  `test_a_delegating_turn_checkpoints_when_the_worker_returns_and_not_before`, which also
  pins the announcement to the event immediately after `worker_finished`.
- **The journal file is at schema v3**, and the ladder was exercised on a copy of the live
  file (below) as well as on fixtures. `SCHEMA_VERSION` is asserted rather than pinned to a
  literal in `test_effect_ledger.py`, so the next step does not need that edit.
- **`checkpoint_written` is the first member of `EMITTED_TYPES` that a setting can switch
  off.** Recorded at the declaration, because "the type is reachable" and "this run reached
  it" are now different statements.
- **Nothing about agent behaviour, prompts, the tool surface or the policy decisions
  changed.** With `[checkpoints] enabled = false` — the shipped default — the only difference
  from the end of Pass 3 is two unused CLI commands and a table with no rows in it.

**Mutation-checked rather than trusted for being green.** Six mutations, all caught:

- drop the pre-flush in `write()` → 14 failures (the buffered `agent_started` makes the run
  look orchestrator-less, which is the loud version of the bug).
- write `memory_watermark: {}` instead of null → caught by the announcement test.
- stop refusing a mid-worker checkpoint → caught by both worker tests.
- let `orchestrator_id` fall back to `run_id` when there is no `agent_started` → caught by the
  detached-run test.
- fire `pre_effect` for every effecting class rather than only `unsafe_write` → caught by the
  `idempotent_write` turn.
- set `covers_seq = event_seq` (the snapshot leading the journal) → 16 failures, including
  the storage CHECK.

**Live-data check (the house rule: read the real rows, do not trust the code).** A copy of
`~/.local/share/agent/journal.db` — copied without its `-wal`, so 34 of the file's 61 events —
was opened by this build: it migrated `user_version` 1 → 3, kept all 34 events,
`PRAGMA integrity_check` returned `ok`, and a `manual` checkpoint was written for each of its
six real runs. Every column came back populated from real payloads (`agent_started.turn_id`,
`worker_created.role`/`task_preview` have the shapes this code reads), and **the only NULLs
in any row were `handoff_object` and `memory_watermark`** — the two Pass 5/7 slots, which are
declared nullable. The live file itself was not touched and is still at `user_version = 1`;
it will migrate on the next open by any process.

### schemas exactly as implemented

```sql
-- journal.db, schema v3. MIGRATIONS = {1: SCHEMA_V1, 2: SCHEMA_V2, 3: SCHEMA_V3}
CREATE TABLE IF NOT EXISTS checkpoint (
  checkpoint_id      text PRIMARY KEY,           -- uuid7
  run_id             text NOT NULL,
  covers_seq         integer NOT NULL CHECK (covers_seq > 0),
  event_seq          integer NOT NULL CHECK (event_seq > 0),
  created_at         text NOT NULL,              -- ISO-8601 UTC, from ids.utcnow
  trigger            text NOT NULL
                     CHECK (trigger IN ('turn_end','worker_finished','pre_effect',
                                        'handoff','manual')),
  orchestrator_id    text NOT NULL,
  messages_ref       text NOT NULL CHECK (json_valid(messages_ref)),
  open_workers       text NOT NULL CHECK (json_valid(open_workers)),
  effects_cursor     text NOT NULL CHECK (json_valid(effects_cursor)),
  handoff_object     text CHECK (handoff_object IS NULL OR json_valid(handoff_object)),
  worker_results     text NOT NULL CHECK (json_valid(worker_results)),
  pending_promotions text NOT NULL CHECK (json_valid(pending_promotions)),
  memory_watermark   text CHECK (memory_watermark IS NULL OR json_valid(memory_watermark)),
  CHECK (event_seq > covers_seq)
);
CREATE INDEX IF NOT EXISTS checkpoint_run ON checkpoint (run_id, event_seq);
```

The record as Python sees it (`journal/checkpoints.py`), with the inert slots marked:

```
Checkpoint
  checkpoint_id      str
  run_id             str
  covers_seq         int      the journal position this snapshot accounts for
  event_seq          int      where its own announcement landed; always > covers_seq
  created_at         str
  trigger            str ∈ {turn_end, worker_finished, pre_effect, handoff, manual}
  orchestrator_id    str      the turn that opened the run
  messages_ref       MessagesRef(run_id, through_seq, message_events)
  open_workers       tuple[WorkerRef(worker_id, role, task_spec, status), ...]
  effects_cursor     EffectsCursor(last_effect_seq, effects, open_keys)
  handoff_object     dict | None = None    INERT — Pass 5
  worker_results     tuple[dict, ...] = () INERT — Pass 6
  pending_promotions tuple[dict, ...] = () INERT — Pass 7
  memory_watermark   dict | None = None    INERT — Pass 7
```

An empty list and a null are different statements and stay different: `[]` is "there were
none", `null` is "this pass recorded none". `WorkerRef.task_spec` is the 200-character
`task_preview` from `worker_created`, because that is what the journal holds; the full task is
the `subagent_message` row in the archive, and a re-delegation must not be built from the
truncated string.

`checkpoint_written`, as written (2b's shape, unchanged except for the nullability noted
above):

```
checkpoint_written  checkpoint_id: str; trigger ∈ CHECKPOINT_TRIGGERS; covers_seq: int
                    memory_watermark: dict | null      (null in every row 4a writes)
                    messages: int    — message *events* covered
                    bytes: int       — serialized size of the record
                    duration_ms: int — building the snapshot only, not the two commits
                    location: str    — the journal file the row is in
```

The API:

```python
Checkpointer(writer | callable, *, cfg=None)
  .write(run_id, *, trigger) -> Checkpoint      # raises; see below
  .get(checkpoint_id) / .latest(run_id) / .entries(run_id=None)
checkpoints.checkpoint_at(trigger, *, run_id, writer=None, cfg=None) -> Checkpoint | None
checkpoints.enabled(cfg=None) -> bool           # the flag, defined once
checkpoints.get_checkpointer(cfg=None)
```

`checkpoint_at` returns `None` for exactly three reasons, all of them rules: the flag is off,
there is no run (a detached tool call), or a worker is in flight. Everything else raises —
`MidWorkerCheckpoint`, `NoOrchestrator`, `CheckpointError` for an unknown trigger or an
announcement that did not reach disk.

Config, as it now exists:

```toml
[checkpoints]
enabled = false     # off until the resume path that reads them lands (4b)
```

**The five boundaries, and who writes them:**

| trigger | call site | fires when |
|---|---|---|
| `turn_end` | `agent/loop.py`, `_TurnRecord.__exit__` | after `agent_finished`, on all four exits |
| `worker_finished` | `agent/subagents.py` | after the worker's result is journaled |
| `pre_effect` | `tools/executor.py` | before `_intend`, for `unsafe_write` only |
| `handoff` | nobody | Pass 5 owns the path |
| `manual` | `agent journal mark <run>` | a person asks |

### checkpoint write overhead, measured

`scripts/bench_checkpoints.py`, this machine, `synchronous=FULL` + WAL, 200-event run with 5
open effect rows, 40 checkpoints per trigger. Four runs of the harness:

| | median | p90 | max |
|---|---|---|---|
| any trigger, FULL | **0.6 – 2.2 ms** (≈1.3 ms typical) | 1.2 – 3.2 ms | 7.1 ms worst seen |
| any trigger, `synchronous=OFF` | **0.10 – 0.12 ms** | 0.12 ms | 0.29 ms |

**The per-trigger differences are noise, and saying so is the point of reporting all five:**
the work is identical for every trigger — one flush, one synchronous append, one row insert —
and the spread between triggers (±0.8 ms) is smaller than the spread between repeats of the
same trigger. Roughly **90% of the cost is the fsync**, not the derivation; the three SQLite
queries that build the snapshot plus the insert are ~0.11 ms.

Storage: **573 bytes per checkpoint on disk**, measured as the delta over 100 checkpoints
after `wal_checkpoint(TRUNCATE)` and `VACUUM` — that is the row, its `checkpoint_written`
event and the index entries together. The serialized record alone is 795–845 bytes with 5
open effect keys and ~525 bytes with one (an open effect key is a 64-character sha256, so
`open_keys` is most of the variable size).

What that means in a turn: an interactive turn that makes one unsafe write pays two
checkpoints, ≈2.6 ms and ≈1.1 KiB. A turn that makes ten pays eleven, ≈14 ms. That is on top
of the four synchronous commits per effecting call 3b already added, and it is on the event
loop thread (2a #4, still open).

### deferred items, and where they went

- **Everything that reads a checkpoint — 4b.** `latest()` and `entries()` exist because the
  tests and the CLI need them; no fold, no `reduce`, no rehydration, no reconciliation. The
  effect ledger's `orphaned` state and the `uncertain` effect status are still written by
  nobody.
- **`handoff_object`, `worker_results[]`, `pending_promotions[]`, `memory_watermark` —
  Passes 5, 6 and 7**, per the *Must not*. They are columns, dataclass fields and round-trip
  tested, and no code path can set them: a later pass adds a parameter, not a migration.
- **The `handoff` trigger — Pass 5.** Accepted and tested, produced by nobody.
- **Retention refusing to prune a run with no checkpoint (2a #7).** Not built. Pruning now
  deletes a run's checkpoints along with its events and effects, so the file stays
  consistent; the refusal needs a policy that reads this table, which is 4b's side of the
  fence.
- **`agent journal show` does not render checkpoints inline** beyond the one-line renderer
  every frontend now shares (`· checkpoint turn_end @ seq 12`). `agent journal checkpoints`
  is the table.
- **`backup.create` still does not cover `journal.db`** (2a recorded why), so checkpoints are
  not backed up either. Correct under the vocabulary — execution state is disposable — and
  worth re-reading when 4b makes resume a thing users rely on.

### open questions for later passes

**1. `open_workers[]` has never been non-empty in a written checkpoint, by construction.**
Delegation is awaited, so no worker is open at a boundary, and when one is the checkpoint is
refused. The field is derived and tested against a hand-built journal
(`test_a_worker_in_flight_refuses_a_snapshot`), but Pass 6 is the first session that can make
it carry a real workload. If parallel workers arrive, the *Must not* has to be revisited at
the same time: "refuse while any worker is open" means "never checkpoint" once two workers
overlap.

**2. A checkpoint of a turn is not a checkpoint of a session.** `covers_seq` covers one run,
and today a run is one turn. 4b's resume therefore resumes a turn; the conversation around it
comes from the Postgres archive, which no checkpoint references. Nobody has decided whether
that is a gap or the correct split, and 2c's open question 2 (`run_id != turn_id` for the
REPL and Telegram) is the same seam.

**3. The turn_end checkpoint can mask a failing turn's exception.** Deviation 8. It has never
happened — the only in-practice failure mode is a broken journal file, which fails the turn
anyway — but a boundary that raises inside `__exit__` is worth one deliberate decision in 4b
rather than a discovery.

**4. `duration_ms` in the event measures the snapshot build, not the boundary.** The two
commits that follow cannot be timed from inside the event that starts them. The full cost is
in `checkpoints.OVERHEAD` (per-process, in memory, folded by nothing) and in the bench script.
Pass 10 owns the reporting and should take the number from one of those, not from the event.

**5. Checkpoints are off in the shipped config, so no live turn has written one.** Every
number above comes from the harness and from a copy of the live journal. The first thing 4b
should do is run one real turn with `[checkpoints] enabled = true` and count the columns,
because that is the check that finds the payload field this code reads by hand.

**6. The 4b/4c requirements Dylan filed at the Pass 3/4 boundary are untouched and still
binding:** an orphaned `web_fetch` must show its URL in the reconciliation prompt, and several
orphaned fetches must be grouped into one prompt. Nothing in this session's record makes
either harder — `effects_cursor.open_keys` names the effects, and the `effect` table holds the
tool and the args hash for each, but **not the arguments themselves**: 4c will have to reach
the `actions` row via `result_ref`, and an effect that never reached `committed`/`failed` has
`result_ref = NULL`. The URL for an orphaned fetch therefore has to come from the
`tool_requested` event in the journal, not from the ledger. That is the one concrete thing 4b
should check early.

**7. Still open, untouched by 4a:** power-loss durability is reasoned rather than measured
(2a #2); a degraded turn (memory retrieval failed) is invisible to a fold (2c #1); a detached
call's events land in a run with no `agent_started` (3b #1) — now also the one run shape a
checkpoint refuses; the ledger holds one bit the journal cannot reproduce (3b #3); effect
events carry no `worker_id` (3b #4); two attempts at one logical call both run (3b #5), which
is what 4b exists to stop. And from Pass 1, token accounting is still broken upstream.

---

## Session 4b — Resume and reconciliation

### what shipped

| file | what it is | lines |
|---|---|---|
| `src/agentd/journal/resume.py` | the fold, the reconciliation, the rehydration, the announcement | 660 |
| `src/agentd/journal/ledger.py` | `orphan()` + `_store_orphaned()` + `reading()`; the docstring the protocol now has | +91 −14 |
| `src/agentd/journal/checkpoints.py` | `Checkpointer.reading()`; asking a read-only one to write raises | +18 −2 |
| `src/agentd/journal/events.py` | `run_resumed` joins `EMITTED_TYPES`; `effect_committed.duration_ms` nullable | +17 −8 |
| `src/agentd/journal/render.py` | a human line for `run_resumed` | +14 −2 |
| `src/agentd/journal/__init__.py` | exports (and why two of them are deliberately absent) | +40 −2 |
| `src/agentd/cli/app.py` | `agent journal resume <run> [--apply] [--reason]` | +70 |
| `src/agentd/agent/loop.py` | 4a's open question 3, settled in a comment at the boundary | +6 |
| `src/agentd/config.py`, `config/default.toml` | why the flag is still off now that something reads a checkpoint | +9 −5 |
| `tests/test_resume.py` | 28 tests | 646 |
| `tests/test_checkpoints.py` | one test: a failing turn keeps its error when the boundary cannot write | +41 |
| `tests/test_journal_events.py` | the emitted/unemitted assertion this session moved | +2 −2 |

Suite 682 → 710 passing. `.venv/bin/ruff check src tests scripts` clean. No tool was added,
removed or re-classified, no prompt changed, and nothing resumes on its own: `resume()` is
reached only from `agent journal resume`, and only `--apply` writes.

### what deviated from the plan, and why

**1. Orphans are found by folding the journal, not by scanning the ledger.** The pass file
says "any ledger entry at `started` with no terminal state becomes `orphaned` on resume". A
scan of the `effect` table would miss the case that matters most: `intend()` journals
`effect_intended` and *then* writes the row, so a crash in that window leaves an announced
call — one that may have run — with no row to find. Reconciliation therefore folds
`effect_intended` / `effect_committed` and consults the row only for the one bit the journal
cannot reproduce (3b #3). It also covers rows at `intended`, which the pass's sentence does
not mention and which are equally unresolved.

**2. The disposition is decided by effect class alone; how far the call got is carried
beside it as `evidence`.** Not in the plan, and it is the difference between two sentences a
person would react to differently:

```
never_dispatched  the row is at `intended`; `dispatched()` commits before the handler is
                  awaited, so the call was never entered
may_have_run      the row is at `started`
unknown           there is no row: the announcement reached disk and the insert did not
```

An `unsafe_write` is `uncertain` in all three cases — never auto-retried, whatever the
evidence says — but "I recorded it and never got as far as trying" is not the same warning as
"it may already have gone out", and collapsing them here would make 4c choose one of them for
both.

**3. `read` is in the disposition map and is unreachable, deliberately.** The map is total
over `effects.EFFECT_CLASSES` so a fourth class cannot inherit whichever branch was written
first (`disposition()` raises instead). But `ledgered()` excludes `read`, so a read produces
no ledger row and no `effect_*` events and cannot become an orphan.
**Checked rather than argued**: a live turn calling `memory_search` wrote no effect event at
all. The runtime's most-called tool can never be surfaced to the user as uncertain, which is
what the Pass 3/4 ruling was for.

**4. "Rehydrate full message list from journal" is delivered as the message *spine*, and no
`model_messages()` is shipped.** The journal does not hold bodies — `events.preview` is 200
characters with the whitespace collapsed — and this is the pass's own instruction to rebuild
from the journal. So `Rehydration` returns the ordered messages with `chars`, `preview`,
`trust`, `step_id`, exact tool-call arguments, and a pointer to where the bodies are. A
function that returned previews as `content` would be a laundering channel for text nobody
said: the turn would read perfectly and remember a version of events that never happened.
What the durable record *can* and cannot supply is in open question 1.

**5. `resume()` writes nothing for a finished run with nothing open.** The sketch is
`load / reconcile / rehydrate / continue`, but a process killed at `turn_end` died after
`agent_finished`: resuming it correctly is doing nothing, and a `run_resumed` after
`agent_finished` is a record of a resume that resumed nothing. `force=True` exists for a
caller that wants the event anyway.

**6. An orphan's row goes to `orphaned` and never to `failed`, even when the evidence says
the call was never entered.** Not a judgement: `CHECK (result_ref IS NOT NULL OR state NOT IN
('committed','failed'))` means a `failed` row must point at a result row, and an interrupted
call has none. The storage decided this in 3b; the distinction survives in the journaled
note.

**7. `effect_committed.duration_ms` became required-*and*-nullable.** An interrupted call was
never timed, and `0` there reads as a call that returned instantly — a measurement that never
happened, in the field whose whole job is to hold an unknown. One-word change, the same shape
as 4a's `memory_watermark`, and `None` is only ever written by `orphan()`.

**8. `plan()` is read-only and had to be given a door to read through.** `Checkpointer` and
`EffectLedger` were both built around a writer. Both gained a `reading(store)` constructor,
and asking one of those for a writer raises rather than quietly opening an append path — a
dry run that opens the write path is not a dry run. The cost is that `plan()` sees what is on
disk, so a caller inside a live process must flush first; `resume()` does, for the same reason
`Checkpointer.write` does.

**9. `plan` and `resume` are not re-exported from `agentd.journal`.** Binding a function named
`resume` on the package shadows the module of the same name, so
`from agentd.journal import resume` would hand back a function. The types are exported; the
two functions are imported from `agentd.journal.resume`. Found by writing the first test.

**10. `run_resumed` got a renderer, unlike the other types the recovery passes write.**
`RENDERED_TYPES` grew by one. A resume is a change in what the agent is doing that the person
watching did not ask for in this turn, and the count of uncertain effects is on the same line
— "I picked this run back up" without "and two calls may already have happened" is the half of
the sentence that reads as reassurance.

**11. 4a's open question 3 is settled rather than inherited.** The `turn_end` boundary stays
inside `_TurnRecord.__exit__` and still raises during an unwind. Python chains the two, so a
turn that was already failing keeps its own error as `__context__`; swallowing it would make
the one checkpoint a resume needed the one nobody knew was missing. Asserted in
`test_a_failing_turn_keeps_its_own_error_when_the_boundary_cannot_write`.

### what is now true about the code that was not before

- **A killed run can be picked up, and the thing it is picked up from is the journal.** A
  checkpoint is read when there is one and is never required. Every run in the live journal
  today was recorded with `[checkpoints] enabled = false`, and all six of them plan correctly
  (below), which is the property that would have been lost by building resume on the snapshot.
- **An interrupted `unsafe_write` is closed, named, and not repeated.** `orphaned` has exactly
  one writer (`EffectLedger.orphan`), reachable only from `resume()`.
- **The arguments of an interrupted call survive the crash that lost its result.** They come
  from `tool_requested`, matched by tool *and* step, nearest before the intent — which is
  exact given that the loop runs a batch's calls one after another. `call_id` looks like the
  obvious key and is not one: it comes from the model, and in the live turn this session ran,
  two calls in two different steps both called themselves `fake_0`.
- **Both requirements Dylan filed at the Pass 3/4 boundary are reachable, and were exercised
  end to end.** `agent journal resume` on a run holding two interrupted fetches and one
  interrupted `goal_upsert` printed:

  ```
  2 interrupted web_fetch calls, never retried automatically:
    · web_fetch(url=https://example.com/a?token=1)  may_have_run
    · web_fetch(url=https://example.com/b)  never_dispatched
    · goal_upsert(title=renew the lease): safe to re-run
  ```

  One block for the fetches, every URL in it. 4c owns the wording; nothing about it has to be
  retrofitted to reach the URL or the grouping.
- **A worker's transcript does not come back.** Rehydration skips every event carrying a
  `worker_id` and counts them, so "worker transcripts never enter orchestrator context" holds
  in the one place nobody would have looked for it.
- **Resuming is repeatable.** `uncertain` is terminal to the fold, so a second resume finds
  nothing open; `_store_orphaned` is guarded on the open states so a committed row can never
  be walked back.
- **A retryable orphan can actually be re-run.** The idempotency key is unchanged, so the
  second attempt lands on the same row with `attempt = 2`, and the first attempt's `uncertain`
  closure stays in the journal — nobody ever did learn how it ended.

**Mutation-checked rather than trusted for being green.** Seven mutations; six were caught at
once, and the seventh is the useful one:

- `unsafe_write` → `retry` in the disposition map → 6 failures.
- fold the worker's messages into the orchestrator's list → caught.
- `duration_ms = 0` instead of `None` on an orphan → caught.
- treat a previous resume's `uncertain` closure as not closing the effect → caught (the same
  email would be reported at every restart).
- delete the ledger row before planning (the announced-but-rowless window) → the orphan is
  still found, because the fold is over the journal.
- **drop the step from the argument match → nothing failed.** The first version of the
  argument tests used two steps, where ordering alone is enough. Two tests were rewritten: two
  calls of one tool *in one step*, and an effect announced in a step holding no request. Both
  now fail under the mutation, and the second is the one that matters — without the step
  match, an orphan borrows the URL of a fetch that completed perfectly well, which is a
  confident answer to the wrong question.
- take the *first* matching request rather than the nearest → caught by the one-step test.

**Live-data checks (the house rule: read the real rows).**

1. **4a's open question 5, closed.** A real turn was run with `[checkpoints] enabled = true`,
   through `build_registry()` and the real tool surface (a scripted provider, no network; the
   scratch Postgres, not the live one). Two checkpoints, `pre_effect` and `turn_end`; **every
   column populated, the only NULLs `handoff_object` and `memory_watermark`** — the two
   inert slots. `tool_requested.args` carried the full arguments including `reason`; the
   `fs_write` effect committed; `memory_search` produced no effect row and no effect event.
2. **A copy of `~/.local/share/agent/journal.db`** (copied without its `-wal`, so 34 of its
   events) was planned over read-only: six runs, two `complete`, and the four `kill-*` runs
   session 2c left behind — genuinely SIGKILLed processes — read as `interrupted`, rehydrated
   their messages, and reported no orphans, which is correct because those turns called `read`
   tools only. The live file was not opened.

### resume cost, measured

Same machine and settings as 4a's bench. A 241-event run with 5 interrupted `unsafe_write`
calls:

| | |
|---|---|
| `plan()` — the whole read-only fold | **median 0.68 ms**, max 1.12 ms |
| `resume(..., apply)` — 5 orphans | **12.7 ms** |

`plan()` is three SQLite reads and a walk; it is cheap and it does not fsync. The apply cost
is fsyncs and nothing else: one synchronous `run_resumed`, then per orphan one synchronous
`effect_committed` and one row update — eleven commits at ≈1.15 ms each, which is 4a's number
for the same hardware. **A resume is cheap in proportion to what it has to admit**, which is
the right shape: a run with nothing open costs one read.

### schemas exactly as implemented

`run_resumed`, as written (2b's shape, unchanged):

```
run_resumed  from_seq: int            the last seq that survived; this event is from_seq + 1
             checkpoint_id: str|null  null = folded from the start, and that is a statement
             reason: str              the caller's, journaled verbatim
             replayed_events: int     how many events the fold read
             uncertain_effects: list  every orphan's effect_id, both dispositions
```

An orphan's closure, written once per orphan immediately after `run_resumed`:

```
effect_committed  status: "uncertain"      (the third member of EFFECT_STATUSES, first use)
                  duration_ms: null        never timed; not 0
                  result_digest: null
                  error: one of resume.NOTES, by evidence
```

```python
NOTES = {
  "never_dispatched": "orphaned on resume: recorded, and the call was never started",
  "may_have_run":     "orphaned on resume: the call was started and never reported back",
  "unknown":          "orphaned on resume: no ledger row, so whether the call was started is unknown",
}
```

The ledger row moves `intended|started → orphaned`. No schema change: `orphaned` has been in
the `state` CHECK since 3b, and no migration was written this session — the file stays at
**schema v3**.

The record as Python sees it (`journal/resume.py`):

```
ResumePlan
  run_id          str
  state           str ∈ {interrupted, complete, no_orchestrator}
  checkpoint      Checkpoint | None        None = there is none, not "not looked for"
  rehydration     Rehydration
  reconciliation  Reconciliation
  .from_seq       int   = rehydration.through_seq
  .needs_resume   bool  state != complete or there are orphans

Rehydration
  run_id, from_seq (0), through_seq, replayed_events
  messages        tuple[RehydratedMessage, ...]   orchestrator only
  worker_events   int    how many events were left out for being a worker's
  turn_id, session_id, archive: ArchiveRef|None, steps, status
  .truncated      the messages whose bodies the journal does not hold in full

RehydratedMessage
  seq, role, actor, chars, preview, source, step_id, trust, tool_call_id
  calls           tuple[ToolCallRef(call_id, name, arguments, seq, visible, known), ...]
  .truncated      chars > len(preview)

Reconciliation
  run_id, orphans: tuple[Orphan, ...]
  .retryable / .uncertain / .groups -> tuple[UncertainGroup(tool, effect_class, orphans), ...]

Orphan
  effect_id, idempotency_key, tool, effect_class, step_id, attempt, intended_seq
  ledger_state    str | None      None = no row
  arguments       dict | None     None = no tool_requested; never {}
  arguments_seq   int | None
  .disposition    retry | uncertain     by effect class
  .evidence       never_dispatched | may_have_run | unknown
  .subject        "tool(arg=…, arg=…)" | None

Resumed
  plan, applied: bool, event_seq: int|None
  orphaned_keys: tuple[str, ...]      rows moved
  rowless_effects: tuple[str, ...]    announced effects that had no row to move
```

The API:

```python
resume.plan(run_id, *, store) -> ResumePlan           # read-only; raises NoSuchRun
resume.resume(run_id, *, writer, reason, force=False) -> Resumed
resume.disposition(effect_class) -> str               # raises ResumeError, never buckets
resume.summary(plan) -> str                           # one line; not a prompt
Checkpointer.reading(store) / EffectLedger.reading(store)
EffectLedger.orphan(*, run_id, step_id, effect_id, key, attempt, note) -> bool
```

CLI:

```
agent journal resume <run_id> [--apply] [--reason TEXT]
```

Reports by default. `--apply` is what writes, because reconciliation moves ledger rows and
appends to the run, and the person asking "what happened to that run" has not agreed to
either. The report is not the prompt: 4c owns the wording a user is asked to act on.

### deferred items, and where they went

- **Continuing the run — 4c and Pass 5.** Nothing here feeds a rehydrated turn back to an
  `AgentLoop`. That needs the `uncertain` observation the orchestrator can act on (4c) and,
  for a run too long to replay, the handoff (Pass 5). The message spine and the arguments are
  the input both will take.
- **Filling the bodies from the archive — whoever continues.** `Rehydration.archive` is the
  pointer (`session_id`, `turn_id`); no query was written, and `db/repo_archive.py` is
  untouched. See open question 1 for what it can and cannot supply.
- **Automatic resume — not built, deliberately.** No daemon sweep, no resume at startup, no
  resume from the loop. A run stays interrupted until a person asks. That is the feature flag
  the pass asked for, made out of who calls it rather than a setting.
- **Re-execution of retryable orphans — not built, and not an omission.** "May be re-executed"
  is a permission recorded against the orphan; the only thing that can re-run a tool call is a
  turn.
- **Turn-id resolution in `agent journal resume`.** `agent journal show` resolves a turn id to
  its run; this command does not, so a REPL or Telegram run has to be named by its run id.
- **No CLI test.** The repo has no CLI test harness (no `CliRunner` anywhere), so the two 4a
  commands and this one are exercised by hand. Both were, against a scratch journal and a copy
  of the live one.
- **Retention refusing to prune a run with unfinished business (2a #7).** Still not built. It
  now has a reader that could decide — `plan(run).needs_resume` — but pruning policy is not
  this session's.

### open questions for later passes

**1. Mid-turn assistant text is not held in full anywhere, so an exact message list cannot be
rebuilt.** The user's message, the tool results and the final answer are in the archive; the
tool call arguments are exact in the journal; the *system* block is rebuilt every turn. What
is lost is the assistant's prose on a step that also called tools: the journal has 200
characters and `actions.output.text` has 500. It is usually empty — the live turn this session
ran had `chars=0` on both tool-calling steps — but "usually" is not a property. Pass 5 owns
the decision: archive it, accept the loss and say so in the resumed context, or treat any
run with a non-empty mid-turn assistant message as handoff-only.

**2. The tool call around an orphaned effect is left open.** Resume closes the *effect*
(`effect_committed(status="uncertain")`) and does not write a `tool_failed`, because the tool
events are the loop's and inventing one would put a terminal event in the journal for a call
the loop never finished. The consequence is real: a fold over tool events still shows a
`tool_started` with no terminal partner for that call. 4c has to decide whether the resumed
turn's message list carries a tool message for it, since the model's message list wants one
per tool call.

**3. `never_dispatched` may not deserve the word "uncertain".** A call the ledger says was
never entered did not happen, and 4c's vocabulary has a better word for it: `blocked` — "the
work did not happen". This session kept it `uncertain` because the disposition is class-driven
and the pass says an `unsafe_write` is never auto-retried, but the evidence is carried
precisely so 4c can make that call.

**4. Nothing notices that a run needs resuming.** There is no sweep, so an interrupted run
with an uncertain email sits in the journal until somebody runs the command. The pieces exist
(`store.runs()` plus `plan(run).needs_resume` over the file is ~0.7 ms per run); who is
allowed to *act* on that is a policy question, and an automatic resume that prompts on
startup is exactly the kind of prompt the Pass 3/4 rulings warn about.

**5. A run can be resumed repeatedly and its journal keeps growing.** `run_resumed` is written
into the run being resumed, so each attempt adds an event. Nothing decides when an interrupted
run is finally abandoned rather than resumable — the same seam as 4a's open question 2 (a
checkpoint of a turn is not a checkpoint of a session).

**6. Orphans group by tool, and effect events carry no `worker_id` (3b #4).** So a
reconciliation cannot say *who* made the call — the orchestrator or a worker it delegated to —
and an `UncertainGroup` would mix them. Nothing produces that shape today (a worker is awaited
inside a step, and a mid-worker checkpoint is refused), and Pass 6 is where it becomes
reachable.

**7. Still open, untouched by 4b:** power-loss durability is reasoned rather than measured
(2a #2); a degraded turn is invisible to a fold (2c #1); `run_id != turn_id` for the REPL and
Telegram (2c #2); `open_workers[]` has never been non-empty (4a #1); `duration_ms` in
`checkpoint_written` measures the build and not the boundary (4a #4). 3b #5 — two attempts at
one logical call both run — is **narrowed rather than closed**: resume now records that the
first attempt's outcome is unknown and refuses to repeat it for an `unsafe_write`, but nothing
suppresses a second attempt made inside a live turn.
