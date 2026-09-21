# Pass 4 — Checkpoints, Resume, Fork — outcome

Sessions completed: **4a**, **4b**, **4c**, **4d**. The pass's exit criteria are met: a run
can be folded back out of the journal, a process killed at any of the five boundaries
resumes, an interrupted `unsafe_write` is closed as `uncertain` and never re-run, as of 4c
that ambiguity is an observation with an explicit path and a user-visible sentence, and as of
4d a run can be rewound into a new run that discloses what the rewound part actually did and
undoes none of it. Checkpoints are still **off in the shipped config**, and after 4b that is a
cost decision rather than a blocker: resume and fork both fold the journal and read a snapshot
only when there is one.

(This paragraph is the only part of the record a later session rewrote: 4d updated it because
it said "4d is untouched", which stopped being true. The session sections below are
untouched.)

The sections below were written by the session that did the work and are not rewritten by
later ones; where 4c found something 4b's section overstates, it says so in its own
deviations rather than editing 4b's.

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

---

## Session 4c — `uncertain` as an observation, and the words it says

### what shipped

| file | what it is | lines |
|---|---|---|
| `src/agentd/agent/observations.py` | the vocabulary, the paths, the user's sentence, the model's block, the tool messages a resumed list carries | 632 |
| `src/agentd/journal/resume.py` | `AnnouncedCall` + `Reconciliation.announced`; `Orphan.call_id`; the subject leads with the identifying argument | +61 −7 |
| `src/agentd/journal/render.py` | `LEADING_ARGS` and `brief_args(..., lead=)` | +18 −2 |
| `src/agentd/cli/app.py` | `agent journal resume` prints the prompt instead of its own report of the same calls; `--notice` | +24 −16 |
| `tests/test_uncertain.py` | 18 tests | 435 |

Suite **711 → 729 passing** (4b's record says 710; the collected count at the start of this
session was 711). `.venv/bin/ruff check src tests scripts` clean. No tool was added, removed
or re-classified; no journal event type, no migration, no table, no config flag, and nothing
in this session writes to the journal or the ledger.

### the two rulings 4b handed over

**1. `never_dispatched` stays `uncertain`. It does not become `blocked`.** (4b open
question 3.) The tempting argument is sound as far as it goes - `dispatched()` commits the
row to `started` before the handler is awaited, so a row left at `intended` says this call
was never entered. It is still the wrong status, for one concrete reason that is not a
durability argument:

> **The ledger row is keyed by idempotency key, and a re-intent resets it to `intended`.**
> `EffectLedger.intend` does `ON CONFLICT (idempotency_key) DO UPDATE SET state =
> excluded.state`. So a second attempt at the same logical call - 3b #5, two attempts both
> run - overwrites a row that had reached `committed`. A crash before the second attempt's
> dispatch leaves a row reading `intended` for a call whose first attempt already went out.

`blocked` means "the work did not happen", and `paths_for(BLOCKED)` puts `retry` first,
because re-making a call that did not happen duplicates nothing. Reading `never_dispatched`
as `blocked` therefore authorises a silent retry of a notification that already fired. The
evidence is kept and surfaced instead: `Orphan.attempt` is on the line, and when it is
greater than 1 the statement carries `EARLIER_ATTEMPT` -
`test_a_second_attempt_that_never_started_still_warns_about_the_first`.

*Rejected alternative, with its reasoning:* map `never_dispatched → blocked` when
`attempt == 1`, which closes the hole above. Rejected because it makes a user-facing claim
about the world out of the state of a derived table written by the process that died, and
because the win is small - a `blocked` fetch and an `uncertain` one read almost the same to
a person; only the machine treats them differently, and that is the direction where being
wrong is expensive.

**2. A resumed turn's message list does carry a tool message for an orphaned call.** (4b
open question 2.) `closing_messages(plan)` returns one `ClosingMessage` per interrupted call
the list is still waiting on. The journal is unchanged - resume still writes no
`tool_failed`, because inventing a terminal tool event would put a claim in the record that
the loop never made. The message list is a reconstruction rather than a record, and the
alternative is a model handed an assistant message whose tool call nothing answers: the
provider errors, or the model invents the result. The message says the one true thing
("interrupted; whether it completed is not known; it has not been re-run"), it is flagged
`synthetic=True`, and its text begins with `RUNTIME_PREFIX` so nothing downstream can read
it as something a tool returned - the same rule 4b applied when it refused to hand a
200-character preview to a model as a message body.

*Rejected alternative:* leave the call dangling and let Pass 5 decide. Rejected because the
dangling call is not visible as a bug - it is visible as a model that describes a fetch it
never saw.

### what deviated from the plan, and why

**1. There is a third status, `result_lost`, and a test found it rather than a design
meeting.** The pass file names two words. The journal holds a third shape: `effect_*` events
are synchronous and the loop's `tool_*` events are buffered, so a call whose effect
committed milliseconds before the kill leaves *exactly* the same unanswered tool call as an
orphan does. It happened; only its result went. Calling that `uncertain` asks the user a
question with a known answer; calling it `blocked` invites the duplicate this pass exists to
prevent. `paths_for(RESULT_LOST)` never contains `retry` for an `unsafe_write`.

**2. `blocked` got a producer, which the pass did not ask for.** "Distinct from `blocked`"
is hard to hold if nothing is ever blocked. A tool call the message list is waiting on with
**no `effect_intended` at all** is the one shape where "nothing outside was changed by it"
is evidence rather than hope: the intent is synchronous and precedes the handler, and a
`read` gets no effect ever and changes nothing outside by definition of its class. That is
the crash that lands while an approval is still on screen. It is also the only producer: a
*denied* call never reaches intent and already has a `tool_failed`, so denials are not
re-reported here.

**3. The binding "show the URL" requirement was not actually met by `brief_args`, and 4b's
record slightly overstates it.** 4b demonstrated one block naming every URL - with
single-argument fetches. `brief_args` renders arguments in call order and hard-truncates the
joined line at `ARG_LINE_CHARS = 120`, so a call with two long arguments ahead of the URL
drops it off the end, silently, in the one prompt Dylan ruled must contain it. `brief_args`
gained an optional `lead=`, `LEADING_ARGS` names the arguments that say *which* thing a call
was about, and both `Orphan.subject` and this module pass it. Default behaviour is
unchanged, so `agent journal show` and the Telegram renderer render what they rendered
before. Caught by
`test_the_url_survives_a_call_whose_other_arguments_are_long`, which needs two long
arguments to bite - the first version of it passed under the mutation.

**4. Observations are read off the rebuilt *message list*, not off the ledger.** A dangling
call matters because the list handed back wants one tool message per tool call, so the
question is about that list and not about the file. That needed two additions to 4b's
module: `Orphan.call_id` (from the same `tool_requested` match 4b already did) and
`Reconciliation.announced` - every effect the run announced *and how it ended*, not only the
open ones. Without the second, a call whose effect committed or failed reads as "never
announced", which is the one sentence that is wrong about both.

**5. The CLI's own report of the same calls is gone, replaced by `prompt()`.** 4b printed a
yellow group block and a dim retryable line and noted that 4c owned the wording; keeping
both would be the two-sources drift it warned about. The counts, the checkpoint line and the
preview line are still the CLI's.

**6. The prompt is printed with `markup=False`.** The arguments are in that text and a URL
or a filename may contain a square bracket. Rich would read `[bold red]` in a URL as a style
tag: at best characters vanish, at worst a fetched value forges a colour the runtime never
chose. Hand-checked with a URL containing literal `[bold red]`; it renders verbatim.

**7. Nothing here is journaled, and no event type was added.** Which path was taken for an
uncertain call is not recorded anywhere, because the thing that takes a path is a turn and
there is no resumed turn until Pass 5. Recording a resolution now would mean a second
terminal event on an effect 4b already closed.

**8. `notice()` and `closing_messages()` have no runtime producer.** Same shape as 4a's
`handoff` trigger: defined, tested, reachable by hand (`agent journal resume --notice`),
produced by nobody until a resumed turn exists.

### what is now true about the code that was not before

- **`uncertain` and `blocked` are different objects, and the difference has teeth.** `retry`
  is in a `blocked` call's paths and is never in an uncertain `unsafe_write`'s. Both are
  computed by one total function that raises on a status it has not been taught.
- **Every interrupted call produces exactly one observation and every observation carries at
  least one path.** The silent drop and the pathless status are the two failure modes, and
  both are asserted rather than argued.
- **A second resume no longer loses the call the first one reported.** An effect a previous
  resume closed as `uncertain` is not an orphan any more, and its tool call is still
  unanswered; it is now observed as `uncertain / closed_uncertain`. Before this session that
  call would have read as "never announced" - i.e. as `blocked`.
- **The identifying argument survives truncation**, in both the orphan path and the
  never-announced path.
- **A resumed message list can be made complete** without writing anything to the journal.
- **Nothing about agent behaviour changed.** No prompt in `agent/prompts` was touched, no
  tool was added, and the only reachable surface is two CLI paths.

**Mutation-checked rather than trusted for being green.** Eight mutations, all caught after
the sixth was fixed:

- `committed` reads as `blocked` → 2 failures.
- an uncertain `unsafe_write` may retry → 7 failures.
- a previous resume's closure reads as `blocked` → caught.
- `verify` offered for every tool → caught by the `reminder_set` test.
- the orphan subject rendered in call order → caught.
- **the never-announced subject rendered in call order → nothing failed**, because the test's
  second call had only one long argument ahead of the URL and the line still fitted in 120
  characters. Rewritten with two; it now fails under the mutation. The truncation, not the
  ordering, is what drops the URL.
- no closing message for a blocked call → caught.
- `never_dispatched` loses the earlier-attempt warning → caught.

**Live-data check (the house rule: read the real rows).** A copy of
`~/.local/share/agent/journal.db` - copied without its 185 KB `-wal`, so a partial view, and
the live file was not opened - was planned and observed read-only. Six runs; the four
`kill-*` runs session 2c left behind are genuinely SIGKILLed processes, and **`kill-2` holds
a real dangling call**: `tool_search(query=calendar)`, a `read`, which is observed as
`blocked` and rendered as one informational line with no question attached. That is the
Pass 3/4 ruling holding on real data - the runtime's read tools never become something the
user is asked to decide. `agent journal resume kill-2` was then run end to end against a
scratch `AGENT_PATHS__DATA_DIR` holding that copy.

### schemas exactly as implemented

```
Observation
  status        str ∈ {uncertain, blocked, result_lost}
  tool          str
  subject       str | None    "web_fetch(url=…)"; None = the journal holds no arguments
  evidence      str ∈ {may_have_run, never_dispatched, unknown,      (4b's, for an orphan)
                       never_announced, committed, effect_failed, closed_uncertain}
  effect_class  str | None    None = no effect was ever announced, so the run does not say
  seq           int           where in the journal this observation is about
  paths         tuple[str, ...]   never empty; strongest first
  effect_id / call_id / step_id   str | None
  attempt       int = 1
  .path         paths[0]          the runtime's recommendation, not its decision
  .may_retry    RETRY in paths
  .statement    one clause about how far the call got
  .name         subject, or "<tool> (arguments not recorded)"

ObservationGroup(tool, status, observations)  .paths  .readback

ClosingMessage(tool_call_id, tool, status, content, step_id, synthetic=True)

# added to journal/resume.py
AnnouncedCall(effect_id, tool, effect_class, step_id, call_id, status|None)
Reconciliation.announced: tuple[AnnouncedCall, ...]   status None = still open
Orphan.call_id: str | None
```

```python
STATUSES = ("uncertain", "blocked", "result_lost")
PATHS    = ("verify", "ask", "proceed_without", "retry")   # retry is resume.RETRY, one word

paths_for(status, *, tool, effect_class) -> tuple[str, ...]
  blocked                          -> (retry, proceed_without)
  uncertain, unsafe_write          -> (verify if READBACK else -) + (ask, proceed_without)
  uncertain, idempotent_write      -> (retry, proceed_without)
  result_lost, unsafe_write        -> (verify if READBACK else -) + (proceed_without,)
  result_lost, idempotent_write    -> (retry, proceed_without)
  anything else                    -> raises ObservationError

SETTLED = {None: (blocked, never_announced), "committed": (result_lost, committed),
           "failed": (blocked, effect_failed), "uncertain": (uncertain, closed_uncertain)}

READBACK = {"fs_write": …fs_read…, "memory_remember": …memory_search…,
            "open_loop_add": …open_loops_list…}
```

`READBACK`'s absences are a finding, not an oversight: **no registered tool lists watchers**,
so `reminder_set` and `watcher_add` cannot be verified by reading back and their only honest
path is to ask. `web_fetch` leaves no trace to read (fetching again is a new fetch),
`notify_user`'s read-back is the user's own eyes, and `shell_exec` and `delegate` do
arbitrary things.

API and CLI:

```python
observations(plan) -> tuple[Observation, ...]      # one per interrupted call, journal order
groups(found) -> tuple[ObservationGroup, ...]      # one per (status, tool)
prompt(plan) -> str                                # the user's sentence; "" when nothing broke
notice(plan) -> str                                # the block a resumed orchestrator is given
closing_messages(plan) -> tuple[ClosingMessage, ...]
paths_for(status, *, tool, effect_class) -> tuple[str, ...]
```

```
agent journal resume <run_id> [--apply] [--reason TEXT] [--notice]
```

### the proposed user-facing wording — **unreviewed**

Written under the autonomous standing policy (`docs/plans/orchestrator-prompt-auto.md`:
"user-facing wording (4c): propose it in the outcome record. I review at pass end"), which
replaces this session's opening instruction to show the wording before finalizing it.
**Dylan has not seen any of this.** It is in the code and under test; changing it is a string
edit and a test edit.

The pass file's worked example is an orphaned *email send*. Session 3d established that **no
send tool exists in this registry** - the Gmail scope is read-only, `gmail send` and
`calendar create` are not registered - so the wording below is written against the
`unsafe_write` tools that do exist. A hypothetical email version is at the end, marked as
such. No send tool was added.

Rendered from a scripted run holding seven interrupted calls (two fetches, an `fs_write`, a
`goal_upsert`, a `notify_user` whose effect committed, a `reminder_set`, and an `fs_read`
that never reached intent):

```
Picking this run back up. Some of it was interrupted, and I have not re-run anything on my
own - here is what I know and what I need from you.

I was interrupted partway through 2 web_fetch calls and I cannot tell whether they happened:

  - web_fetch(url=https://leases.example.com/renew?token=abc123)
      started, and never reported back - it may have completed
  - web_fetch(url=https://example.com/b)
      recorded, and this attempt was never started

Nothing I can call will tell me whether they happened, so this one is yours: say "leave it"
and I carry on without them and say so in what I write, or tell me to run them again if you
know they did not happen.

I was interrupted partway through 1 fs_write call and I cannot tell whether it happened:

  - fs_write(path=/home/dylan/notes/lease.md, content=xxxxxxxxxxxxxxxxxxxxxxxxxxxx…)
      started, and never reported back - it may have completed

Say "check" and I will read the file back with fs_read and see whether it holds the new
content; "leave it" and I carry on without it and say so in what I write; or tell me to run
it again if you know it did not happen.

1 notify_user call went through and I no longer have what it returned - the record that it
happened survived the restart and the result did not. I am not running it again:

  - notify_user(title=Lease renewal, body=submitted)

I was interrupted partway through 1 reminder_set call and I cannot tell whether it happened:

  - reminder_set(text=chase the landlord, at=18:00)
      started, and never reported back - it may have completed

Nothing I can call will tell me whether it happened, so this one is yours: say "leave it"
and I carry on without it and say so in what I write, or tell me to run it again if you know
it did not happen.

1 fs_read call did not get far enough to change anything: the run ended before the runtime
recorded an effect for it. Running it again duplicates nothing.

  - fs_read(path=/home/dylan/notes/lease.md)

(goal_upsert(title=renew the lease) was interrupted too. Running it again changes nothing,
so there is nothing for you to decide.)
```

The three evidence clauses, in full, and the fourth that is appended:

```
started, and never reported back - it may have completed
recorded, and this attempt was never started
announced, with no record of whether it was started
interrupted by an earlier restart, and never established either way
completed - the effect is recorded, and only its result went with the process
reported a failure, and the report went with the process
never started: the process exited before the call was announced
  (+) - an earlier attempt at the same call may still have run
```

The model-facing block, in full:

```
Calls interrupted by the restart you are picking this run up from. Read this before you do
anything else.

`uncertain` is not `blocked`. Blocked means the work did not happen. Uncertain means nobody
knows whether it happened, and the run cannot find out by itself.

2 uncertain:
  - web_fetch(url=https://leases.example.com/renew?token=abc123)
      started, and never reported back - it may have completed
      you may: ask the user, naming the call and its arguments; proceed without it, and say
      in your answer that you did
  - fs_write(path=/home/dylan/notes/lease.md, content=xxxx…)
      started, and never reported back - it may have completed
      you may: verify - read the file back with fs_read and see whether it holds the new
      content; ask the user, naming the call and its arguments; proceed without it, and say
      in your answer that you did

1 happened, with the result lost rather than the call. Do not run these again; say what they
did if it matters:
  - notify_user(title=Lease renewal, body=submitted)
      you may: proceed without it, and say in your answer that you did

1 blocked - none of these changed anything outside, and running one again duplicates
nothing:
  - fs_read(path=/home/dylan/notes/lease.md)

You must not re-run an uncertain call, and you must not pass over one without saying so.
Take one of the paths listed for each of them. If you proceed without a call, the answer you
give has to say that you did.
```

The tool messages a resumed list carries, in full:

```
[runtime, on resume - not output from the tool] This call was interrupted by the process
exiting. Whether it completed is not known, and it has not been re-run. Do not re-run it
yourself.

[runtime, on resume - not output from the tool] This call went through - the runtime holds
the record that it did - and what it returned was lost when the process exited. Do not run
it again.

[runtime, on resume - not output from the tool] This call was interrupted by the process
exiting. The runtime never recorded it as started, so it changed nothing outside and no
result came back. Making the call again duplicates nothing.
```

**Variants considered and rejected.**

1. `Confirm this fetch? [y/N]` — the shape Dylan's ruling exists to forbid. No URL, no
   evidence, and a default. Rejected: a prompt the user cannot act on trains blind
   confirmation.
2. `Some calls may not have completed. Retry? [y/N]` — one question for a mixed batch, and
   it pre-authorises re-running a call that may already have happened. Rejected on both
   counts.
3. One block per *call* rather than per tool. Rejected: Dylan's second ruling, and four
   questions are four chances to confirm out of habit.
4. Repeating "I have not re-run them and I will not re-run them on my own" under every
   heading. This was the first version, and it is in this session's history. Rejected after
   reading it rendered: four identical paragraphs is a wall, and a wall is skipped. The rule
   is now stated once at the top.
5. Offering `retry` as a listed option for an uncertain call ("say `redo` and I will run it
   again"). Rejected: offering to repeat an action that may already have happened is the
   thing the pass forbids, phrased as a convenience. The user can still ask for it in their
   own words, and the wording says so - "tell me to run it again if you know it did not
   happen" - which puts the claim of knowledge on the person who has it.
6. `status: uncertain (2)` with the calls behind a `--verbose`. Rejected: the arguments are
   the whole content of the message.
7. Saying nothing at all about `blocked` and `result_lost` calls, on the grounds that
   neither needs a decision. Rejected: "I did not do X" and "I did X and lost what it said"
   are both things the person asking about this run needs; they are stated, not asked.

**The email example, hypothetical.** If a send tool existed and were classed `unsafe_write`,
the same code would render:

```
I was interrupted partway through 1 gmail_send call and I cannot tell whether it happened:

  - gmail_send(to=landlord@example.com, subject=Lease renewal)
      started, and never reported back - it may have completed

Say "check" and I will look in your sent mail; "leave it" and I carry on without it and say
so in what I write; or tell me to run it again if you know it did not happen.
```

The "check" clause is the `READBACK` entry such a tool would need; without one it would get
the "nothing I can call will tell me" wording. No such tool exists and none was added.

### deferred items, and where they went

- **Continuing the run — Pass 5.** `notice()` and `closing_messages()` are the two inputs a
  resumed turn needs and nothing consumes them yet. Wiring them into an `AgentLoop` needs the
  handoff path.
- **Recording which path was taken — whoever continues.** No event, no column. The effect is
  already terminal from 4b, so this would be a new event type on a later pass, not a second
  closure.
- **Verification as an action — not built.** `verify` is a path with a sentence naming the
  tool that would answer it; nothing calls that tool. The only thing that can call a tool is
  a turn.
- **A watcher-listing tool — not this session's.** Its absence is why `reminder_set` and
  `watcher_add` can only ask. Filed here rather than against Pass 8, which is tool-surface
  reduction and would be the wrong place to add one.
- **A sweep that notices a run needs resuming (4b #4).** Still nobody's.
- **No CLI test.** Unchanged: the repo has no CLI harness, so `--notice` and the new prompt
  path were exercised by hand against a scratch data dir and a copy of the live journal.

### open questions for later passes

**1. The wording is unreviewed.** Every string above is proposed under the autonomous
standing policy. It is also the only part of this session that a later pass will want to
change without changing behaviour, so it is deliberately all in one module.

**2. `result_lost` is a third word in a two-word vocabulary.** The pass file defines
`blocked` and `uncertain`; this session found a shape that is honestly neither and named it.
If that name is wrong it should be renamed before Pass 5 gives it a consumer.

**3. A journal read without its `-wal` can turn an uncertain call into a `blocked` one.**
`blocked` rests on the absence of a synchronous `effect_intended`, and a copy taken without
the WAL is missing recent events. Every live-data check in this pass reads such a copy. The
inference is sound about the file it is given; it is only as sound as the file.

**4. `blocked` for a `read` is a claim about effect, not about execution.** An interrupted
`read` may have run and lost its buffered `tool_finished`. The wording says "did not get far
enough to change anything", which is true either way, but the status word is stronger than
the evidence for that one class.

**5. Observations are derived on every read and stored nowhere.** Two resumes of the same run
produce them twice, and nothing can tell whether a human already answered. Pass 5 is the
first place that could hold an answer.

**6. Still open, untouched by 4c:** effect events carry no `worker_id` (3b #4), so a group
could mix a worker's calls with the orchestrator's once Pass 6 makes that reachable (4b #6);
mid-turn assistant text is not held in full anywhere (4b #1); nothing notices that a run needs
resuming (4b #4); a run can be resumed repeatedly (4b #5); `open_workers[]` has never been
non-empty (4a #1); power-loss durability is reasoned rather than measured (2a #2); token
accounting is still broken upstream.

---

## Session 4d — Fork, and the disclosure that makes it honest

### what shipped

| file | what it is | lines |
|---|---|---|
| `src/agentd/journal/fork.py` | the fork point, the new run, the disclosure as facts | 416 |
| `src/agentd/agent/observations.py` | `disclosure()` — the words, beside 4c's other recovery wording | +103 |
| `src/agentd/journal/resume.py` | `plan(..., through_seq=)`; `announced_calls()`; `AnnouncedCall` grew three fields | +99 −25 |
| `src/agentd/journal/checkpoints.py` | `Checkpointer.at()`; `_open_workers` → public `open_workers_at` | +28 −6 |
| `src/agentd/journal/events.py` | `run_forked` joins `EMITTED_TYPES`; `fork_point_seq` → `forked_from_seq` | +28 −9 |
| `src/agentd/journal/render.py` | a human line for `run_forked` | +24 −6 |
| `src/agentd/journal/__init__.py` | exports, and why `fork()` is deliberately not one | +37 −5 |
| `src/agentd/cli/app.py` | `agent journal fork <run> --at N [--apply] [--reason] [--as]` | +75 |
| `tests/test_fork.py` | 22 tests | 538 |
| `tests/test_journal_events.py` | the emitted/unemitted assertion this session moved, and one payload key | +11 −8 |

Suite **729 → 751 passing**. `.venv/bin/ruff check src tests scripts` clean. No tool was added,
removed or re-classified; no migration, no new table, no config flag, no prompt change. The
journal file stays at **schema v3**: a fork's whole durable footprint is one event.

### what deviated from the plan, and why

**1. `fork_point_seq` was renamed to `forked_from_seq`, in the event.** 2b declared the
payload key as `fork_point_seq`; the pass file, the architecture's fork record and its
`fork_lineage` all call the same number `forked_from_seq`. Nothing had ever emitted the type,
so this is a one-line vocabulary edit and one test line rather than a migration, and it buys
the property 4a wrote down under `covers_seq`: one name for one integer. The alternative —
a record field named one thing next to a payload key named the other — is the invitation to
read one as the other that 4a refused.

**2. The disclosure is not only the `committed` list.** The pass file says "list every
`committed` effect after `seq`". An effect that never settled, or that an earlier resume
closed as `uncertain`, may also have changed the world; a fork disclosure that silently
dropped it would be a confident "here is everything I did" missing the one call the user
would most want to know about. So `Disclosure` has three lists — `committed`, `unresolved`,
`failed` — and the wording keeps them in three blocks. "I did this" and "this may have
happened" stay different sentences, which is the whole point of 4c's vocabulary.
`failed` is stated and not counted as work: 3b ruled that a call which returned a failure did
not produce its effect.

**3. "After `seq`" means intended after it *or* settled after it.** The call a rewind is most
likely to be about is the one that was in flight when the user rewound past it: announced
before the fork point, committed after it. Filtering on the intent alone would leave it out
of the disclosure entirely while it sat in the journal, committed.
(`test_a_call_in_flight_at_the_fork_point_that_landed_afterwards_is_disclosed`.)

**4. The pass file's worked example is illustrative and is not what this renders.** "sent 1
email / created 1 calendar event" names two tools that do not exist in this registry — 3d
established the Gmail scope is read-only and no send or calendar-create tool is registered,
and none was added. The disclosure renders whatever `committed` effects are actually there.
"modified 3 files in src/auth/" is the shape that *is* implemented, and only when it is
derived: the count is a count of announced effects, and the directory is
`commonpath` of the paths those calls really recorded. Where the calls share nothing but `/`,
or where any of them has no recorded path, **no location is claimed at all** — a line reading
"3 files in /" has told the reader nothing while sounding like it has.

**5. The rehydration is 4b's, reached by a new parameter rather than a second path.**
`resume.plan()` took `through_seq`. A fork-only fold would have been free to disagree with a
resume about what a run said, in the two places nobody would compare. The consequence is that
a fork inherits 4b's ruling in full: the message *spine*, with previews and exact tool-call
arguments, and no `model_messages()`. `test_a_fork_hands_back_previews_and_never_calls_them_
the_conversation` asserts the absence.

**6. A fork point inside an open worker is refused.** Not in the plan. Rehydrating a run with
a worker in flight hands the new run a delegation nothing can finish, and "workers are
re-delegated, not resumed" is the rule 4a already derived — so `checkpoints._open_workers`
became public `open_workers_at` and is asked the same question at the fork point, rather than
a second implementation being free to disagree. Same shape as the *Must not* on mid-worker
checkpoints, and the same single source.

**7. A fork point the run never reached is refused, not clamped.** `seq 0`, `seq 99` and an
unknown run all raise. Clamping to the last seq would rewind to somewhere the user did not
ask for and then disclose a stretch of history that does not exist.

**8. The wording lives in `agent/observations.py`, not in `journal/fork.py`.** 4c made that
module the one place this runtime phrases a recovery sentence for a person, and the CLI
prints it rather than composing its own — which is what 4b's record asked for and what 4c
enforced by deleting the CLI's own version. `fork.py` holds the facts (`Disclosure`,
`CallGroup.location`, `DisclosedCall.subject`); `observations.disclosure(plan)` is the
paragraph.

**9. `run_forked` got a renderer, unlike Pass 5's types.** It is the *first* event of the run
it opens: without a line, a forked run reads in `agent journal show` as a conversation that
began out of nothing, and where it came from is exactly what a person scrolling back needs.
The disclosure is not on that line — it is the paragraph printed at the moment of the fork,
and a one-line version would be a summary of a summary.

**10. Nothing continues the forked run**, in the same sense 4b built no continuation: `fork()`
writes one event and hands back the rehydrated state. A forked run therefore has no
`agent_started` until something continues it, so `resume.plan()` reports it as
`no_orchestrator` — the same shape as 3b's detached runs. Pass 5 owns the continuation.

### what is now true about the code that was not before

- **A run can be rewound, and the run it was rewound from is bit-for-bit what it was.**
  Asserted event by event and row by row
  (`test_forking_a_run_leaves_the_run_it_forked_from_exactly_as_it_was`): no event appended to
  the parent, no ledger row moved, no checkpoint taken, nothing deleted. The lineage lives in
  the child, because the child is the only run that needs it.
- **Nothing is undone, and the runtime says so in words.** No file is restored, no compensating
  call is made, and the closing sentence is the architecture's: "Reverting the conversation. I
  have not undone any of the above." The *Must not* was not approached, let alone crossed.
- **The disclosure's counts and paths come out of the journal.** The count is the number of
  `effect_intended` events with a `committed` closure after the fork point; the paths are the
  `path` arguments from the matching `tool_requested`; the shared directory is `commonpath` of
  those paths or nothing. A call the journal holds no arguments for is named
  `fs_write (arguments not recorded)` rather than given empty brackets.
- **The identifying argument survives the 120-character line**, in this renderer too:
  `DisclosedCall.subject` passes `LEADING_ARGS`, and
  `test_the_path_survives_a_call_whose_other_arguments_are_long` needs two long arguments ahead
  of the path to bite — which is the version of 4c's test that actually fails under the
  mutation.
- **A worker's effects are disclosed as the run's own.** Effect events carry no `worker_id`
  (3b #4) and a worker's events are in its caller's run, so work delegated inside the rewound
  stretch is in the disclosure. Who inside the run did it is not a distinction a disclosure is
  allowed to drop work behind.
- **A forked run does not inherit the parent's effect ledger, and that is the honest
  behaviour.** The idempotency key is `hash(run_id, step_id, tool_name, canonical_args)`, so a
  call the child repeats has a key the parent's row cannot answer for and will really happen
  again. Inheriting it would be the "cross-run result caching" the *Must not* forbids, and it
  is also why the disclosure has to be accurate before anybody decides to repeat a call.
- **An effect status this code has not been taught raises** rather than being described as
  "interrupted". Same rule as 4c's `SETTLED`.

**Mutation-checked rather than trusted for being green.** Nine mutations, all caught:

- disclosure filters on the intent only (drops the in-flight-then-committed call) → caught.
- `unresolved` folded into `committed` → 2 failures.
- `CallGroup.location` returns `/` as a shared directory → caught.
- the subject rendered in call order rather than `LEADING_ARGS` → caught.
- `Checkpointer.at` filters on `event_seq` instead of `covers_seq` → caught (the test forks at
  a snapshot's `covers_seq`, which is the only position where the two differ).
- a mid-worker fork allowed → caught.
- an unknown unsettled status gets a generic "interrupted" sentence → caught.
- `through_seq` ignored, so a fork rehydrates the whole run → caught.
- `fork()` also writes `run_forked` into the parent → caught by the untouched-parent test.

**Live-data checks (the house rule: read the real rows).**

1. A copy of `~/.local/share/agent/journal.db` — copied without its 185 KB `-wal`, so a partial
   view, and the live file was not opened — was planned read-only at the midpoint of each of
   its six runs. All six plan a fork; none holds an effecting call after its fork point, so all
   six render the empty disclosure, which is correct (those turns called `read` tools only, as
   4b found).
2. **The columns were counted, not assumed.** A scratch run holding six real ledgered effects
   (3 `fs_write` committed, 1 `notify_user` committed, 1 `web_fetch` failed, 1 `reminder_set`
   left open) was forked end to end through `agent journal fork --apply`, twice. The child's
   `run_forked` row came back with **`handoff_id` the only NULL** when a checkpoint stood at or
   before the fork point, and `checkpoint_id` + `handoff_id` NULL when none did — the two slots
   that are declared nullable and nothing else. The parent kept all its events and all six
   ledger rows in their original states (`committed` 4, `failed` 1, `started` 1), and every
   `effect.run_id` was still the parent's.

### schemas exactly as implemented

`run_forked`, as written (2b's shape, with the one rename above):

```
run_forked   parent_run_id: str
             forked_from_seq: int      the position the new run starts from
             reason: str               the caller's, journaled verbatim
             checkpoint_id: str|null   null = no snapshot stood at or before the fork point
             handoff_id: str|null      always null in 4d; Pass 5's slot
```

Emitted **as the first event of the new run**, synchronously, and nowhere else. The parent run
gets nothing. No table, no column, no migration.

The records as Python sees them (`journal/fork.py`):

```
ForkPlan
  parent_run_id    str
  forked_from_seq  int
  parent_last_seq  int
  state            resume.ResumePlan     the parent folded through the fork point
  disclosure       Disclosure
  .rehydration     4b's message spine, as of the fork point
  .checkpoint      Checkpoint | None     the latest with covers_seq <= fork point
  .open_at_fork    tuple[resume.Orphan]  open *at* the point; the parent's to reconcile

Disclosure
  parent_run_id, forked_from_seq
  committed / unresolved / failed   tuple[DisclosedCall, ...]
  .nothing_recorded  bool            no effecting call after the point at all
  .groups / .unresolved_groups       tuple[CallGroup, ...]   one per tool, intent order

DisclosedCall
  effect_id, tool, effect_class, step_id
  intended_seq   int
  settled_seq    int | None      None = still open
  status         str | None      committed | failed | uncertain | None
  arguments      dict | None     None = no tool_requested; never {}
  .subject       "fs_write(path=…)" | None
  .name          subject, or "<tool> (arguments not recorded)"
  .location      the absolute `path` argument, or None

CallGroup(tool, calls)   .location = commonpath of the calls' directories, or None

Forked(plan, run_id, event_seq)   .parent_run_id  .forked_from_seq
```

```python
# added to journal/resume.py
plan(run_id, *, store, through_seq: int | None = None) -> ResumePlan
announced_calls(events) -> tuple[AnnouncedCall, ...]     # a pure fold, no ledger, no store
AnnouncedCall.intended_seq: int; .settled_seq: int | None; .arguments: dict | None
AnnouncedCall.subject / .name
# added to journal/checkpoints.py
Checkpointer.at(run_id, through_seq) -> Checkpoint | None    # filtered on covers_seq
open_workers_at(store, run_id, seq) -> tuple[WorkerRef, ...]
```

The API:

```python
fork.plan_fork(run_id, *, at_seq, store) -> ForkPlan       # read-only
fork.fork(run_id, *, at_seq, writer, reason, new_run_id=None) -> Forked
fork.disclose(run_id, events, at_seq) -> Disclosure
fork.by_tool(calls) -> tuple[CallGroup, ...]
fork.summary(plan) -> str                                  # one line; not the disclosure
observations.disclosure(plan: ForkPlan) -> str             # the paragraph
```

Raises, never degrades: `NoSuchRun` (no such parent), `NoSuchForkPoint` (a position the run
never reached), `MidWorkerFork`, `RunExists` (the new run already holds events), `ForkError`
(forking a run into itself, or an announcement that did not reach disk).

CLI:

```
agent journal fork <run_id> --at <seq> [--apply] [--reason TEXT] [--as <run_id>]
```

Reports by default; `--apply` opens the new run. `--as` names it, otherwise it is a uuid7.
The disclosure is printed with `markup=False` for 4c's reason: a path or a URL may contain a
square bracket, and rich would read it as a style tag.

### the disclosure format, verbatim

**Superseded in four places by Dylan's review at the Pass 4/5 boundary — see
"Pass 4/5 boundary — the wording review" at the end of this record for what the code
says now. The block below is what was *proposed*, kept because the review is only
legible next to it.**

Rendered by `agent journal fork demo-fork --at 2` against a scratch data dir holding a real
run: three `fs_write` calls committed, one `notify_user` committed, one `web_fetch` that
failed, one `reminder_set` left open. Every line below came out of that journal.

```
Going back to seq 2 of this conversation. The run it came from is untouched - its journal is
not rewritten - and nothing it did has been undone.

Since that point I:
  - 3 fs_write calls, all under /home/dylan/notes/
      · fs_write(path=/home/dylan/notes/lease.md, content=xxxxxxxxxxxxxxxxxxxxxxxxxxxx…)
      · fs_write(path=/home/dylan/notes/landlord.md, content=xxxxxxxxxxxxxxxxxxxxxxxxxx…)
      · fs_write(path=/home/dylan/notes/inventory.md, content=xxxxxxxxxxxxxxxxxxxxxxxxx…)
  - 1 notify_user call
      · notify_user(title=Lease notes, body=three files written)

I may also have:
  - 1 reminder_set call
      · reminder_set(text=chase the landlord, at=18:00)
          interrupted, and never reported back - whether it happened is not known

(1 web_fetch call after that point returned a failure, so it changed nothing outside.)

Reverting the conversation. I have not undone any of the above, and I cannot: a fork rewinds
what was said, not what was done.
```

(The `content=xxxx…` runs are the real 60-character argument previews, shortened here to fit
the page; everything else is character for character what was printed.)

With nothing after the fork point — which is every run in the live journal today:

```
Going back to seq 2 of this conversation. The run it came from is untouched - its journal is
not rewritten - and nothing it did has been undone.

The journal records no effecting call after that point, so nothing outside this conversation
was changed by the part I am rewinding.
```

The two unsettled clauses, in full:

```
interrupted, and never reported back - whether it happened is not known
interrupted, and an earlier resume gave up on it - whether it happened is not known
```

**The pass file's example is illustrative, not rendered.** For the record, it reads:

```
Since that point I:
  - modified 3 files in src/auth/
  - sent 1 email
  - created 1 calendar event

Reverting the conversation. I have not undone any of the above.
```

The first line is the shape this code produces when the paths support it. The second and third
name tools that are not in this registry and were not added; if a send tool existed and were
classed `unsafe_write`, its committed calls would render as `1 gmail_send call` with the call
named underneath, from the same code path.

**Unreviewed, like 4c's.** Written under the same standing policy
(`docs/plans/orchestrator-prompt-auto.md`). Dylan has not seen it. Changing it is a string
edit and a test edit, and it is all in one module.

### deferred items, and where they went

- **Continuing a forked run — Pass 5.** `fork()` hands back the rehydrated state and writes
  one event; nothing feeds that back to an `AgentLoop`. A forked run has no `agent_started`
  until something does, so it reads as `no_orchestrator` to `resume.plan()` in the meantime.
- **A model-facing fork block — nobody's yet.** 4c has `notice()` for a resumed orchestrator;
  a forked run has no orchestrator to address, and inventing the block before there is a
  consumer would be a second place to keep this wording correct.
- **Undo — explicitly not built, and the *Must not*.** No file rollback, no compensating
  calls, no `has_undo` plumbing. The architecture's own note stands: content-addressed
  pre-images for the `coder` role are a plausible later addition; reversal of mail and
  calendar effects is not achievable in general and is not promised.
- **Forking a *session* rather than a run.** A run is one turn today (4a #2), so this rewinds
  a turn. The conversation around it is in the Postgres archive and no fork touches it.
- **Retention.** Pruning still does not know that a run has children; a pruned parent leaves a
  `run_forked` pointing at nothing. Same fence as 2a #7, which is still not built.
- **No CLI test.** Unchanged: the repo has no CLI harness, so `agent journal fork` was
  exercised by hand, twice, against a scratch data dir and read-only against a copy of the
  live journal.

### open questions for later passes

**1. A fork's disclosure is as good as the file it reads.** `nothing_recorded` rests on the
absence of `effect_intended`, which is synchronous — but a journal copied without its `-wal`
is missing recent events, and every live-data check in this pass reads such a copy (4c #3, the
same caveat in a new place). The claim "nothing outside this conversation changed" is the
strongest sentence this pass prints; it is worth re-reading the day anything reads a journal it
did not write.

**2. Nothing links a parent to its children.** `run_forked` names the parent, so lineage walks
one way only: to find a run's forks you scan. Fine at six runs; a `fork_lineage` view is the
architecture's own answer (§ observability) and nobody owns it yet.

**3. `location` only understands `path`.** A `shell_exec` that wrote three files, or a tool
whose target is named something else, discloses as three calls with no shared location. That is
the safe failure — no claim rather than a wrong one — but it means the "3 files in src/auth/"
line is reachable for `fs_write` and nothing else today.

**4. A fork of a fork is untested in anger.** It works mechanically — the child is a run like
any other — but the disclosure of the second fork covers only the *second* parent's journal, so
work done in the grandparent after the first fork point is not re-disclosed. That is arguably
right (it was disclosed once) and nobody has decided.

**5. Two attempts at one logical call, across a fork.** 3b #5, narrowed by 4b and widened again
here: the child mints new idempotency keys, so a call the parent already made can be made again
by the fork without the ledger noticing. This is deliberate (the *Must not* forbids cross-run
caching) and the disclosure is the only thing standing between the user and a duplicate.

**6. Still open, untouched by 4d:** power-loss durability is reasoned rather than measured
(2a #2); a degraded turn is invisible to a fold (2c #1); `run_id != turn_id` for the REPL and
Telegram (2c #2); `open_workers[]` has never been non-empty (4a #1) — and a fork now refuses
the one position where it would be; mid-turn assistant text is not held in full anywhere
(4b #1); nothing notices that a run needs resuming (4b #4); observations are derived on every
read and stored nowhere (4c #5); token accounting is still broken upstream.

---

## Pass 4 — closing

**The exit criteria are met.**

- *Kill the process at each boundary type and resume successfully.* 4b: all five boundaries,
  with the kill simulated the way the storage layer says a kill looks (a lost suffix, never a
  hole), plus the four real SIGKILLed `kill-*` runs in the live journal, planned read-only.
- *An orphaned `unsafe_write` surfaces as `uncertain` and is never silently retried.* 4b closes
  it in the journal and moves the row to `orphaned`; 4c gives it a status, a path and a
  sentence, and `retry` is absent from an uncertain `unsafe_write`'s paths by construction, not
  by convention.
- *Fork works and discloses honestly.* 4d, above: the parent is untouched, the disclosure is
  derived from the journal, and the separation between "I did this" and "this may have
  happened" survives all the way into the printed paragraph.

**What the pass did not do, deliberately:** no automatic reversal of anything, no mid-worker
checkpoints, no cross-run result caching, and `worker_results[]`, `handoff_object` and
`memory_watermark` are still empty slots. No tool was added, removed or re-classified in any of
the four sessions, and no prompt in `agent/prompts` was touched.

**Checkpoints remain off in the shipped config.** Nothing in the pass requires them: resume and
fork both fold the journal and read a snapshot only when one exists. Turning them on costs
≈1.3 ms and 573 bytes per boundary (4a) and is a decision about interactive latency, not about
correctness.

**What Pass 5 inherits.**

- Three surfaces with no producer: the `handoff` checkpoint trigger (4a), `notice()` and
  `closing_messages()` (4c), and now a forked run with no continuation (4d). Pass 5 is the
  session that gives all three a caller, and the message spine plus the exact tool-call
  arguments are the input each of them takes.
- One decision it cannot avoid: **mid-turn assistant text is not held in full anywhere**
  (4b #1). Archive it, accept the loss and say so in the resumed context, or treat any run with
  a non-empty mid-turn assistant message as handoff-only.
- One naming call: `result_lost` is a third word in a two-word vocabulary (4c #2). If it is
  wrong it should be renamed before it gets a consumer.
- One review owed to a human: **every user-facing string written in 4c and 4d is unreviewed.**
  Both sessions wrote it under the autonomous standing policy and both said so at the point of
  writing. It is in two functions in one module.


---

## Pass 4/5 boundary — the wording review

The one thing Pass 4 owed a human. Put to Dylan on 2026-09-21, before 5a was dispatched.

**4d's disclosure: stands, with four edits, applied.** **4c's wording: still held** — he had
not read it in full, and nothing in Pass 5 may consume `notice()`, `prompt()` or
`closing_messages()` until he has.

### the four edits, and what each is about

**1. The opening sentence stops explaining itself.**

```
was:  Going back to seq 2 of this conversation. The run it came from is untouched - its
      journal is not rewritten - and nothing it did has been undone.
now:  Going back to seq 2 of this conversation. The run it came from is kept exactly as it
      was.
```

The two clauses it drops were both true and both mechanism. "Its journal is not rewritten"
answers a question the reader did not ask, and "nothing it did has been undone" is the
closing sentence's job — saying it twice in one paragraph spends the reader's attention on
the part that needs it least.

**2. `I:` → `I made:`, in both headings.** "Since that point I made:" and "I may also have
made:". The list underneath is a list of *calls*, and the old heading grammatically governed
whatever followed — so the sentence only parsed if you read the call lines as verbs.

**3. A failed call is stated, never interpreted.**

```
was:  (1 web_fetch call after that point returned a failure, so it changed nothing outside.)
now:  (1 web_fetch call after that point returned an error.)
```

His reasoning, which is the load-bearing part: **a failure response does not prove the
effect did not land — a fetch can fail after the server acted, and that is exactly why
`web_fetch` is `unsafe_write`.** One rule for every tool, so the renderer has no per-class
branch. Filed as a Pass 10 collection entry in `docs/records/pass-03-outcome.md`, beside
`memory_search` and `web_fetch`: it is the same "property of the call, collapsed into a
property of something coarser" shape, one level down at the ledger instead of at the
registry.

**4. The empty case attributes its own claim.**

```
was:  ...so nothing outside this conversation was changed by the part I am rewinding.
now:  ...so as far as the journal shows, nothing outside this conversation was changed by
      the part I am rewinding.
```

This is 4d's own open question 1 answered in the wording rather than deferred: the claim
rests on the absence of a synchronous `effect_intended`, and every live-data check in Pass 4
read a journal copied without its `-wal`. The strongest sentence this pass prints now names
the thing it rests on.

**The closing sentence is unchanged**, at his instruction: "Reverting the conversation. I
have not undone any of the above, and I cannot: a fork rewinds what was said, not what was
done."

### what shipped for the review

| file | what changed |
|---|---|
| `src/agentd/agent/observations.py` | the four strings in `disclosure()` |
| `src/agentd/journal/fork.py` | `Disclosure`'s docstring: the `failed` reasoning, corrected and cross-referenced |
| `tests/test_fork.py` | four assertions retargeted; one added, pinning the new opening sentence, which had none |
| `docs/records/pass-03-outcome.md` | the Pass 10 collection entry |

Suite **751 passing**, unchanged in count. `.venv/bin/ruff check src tests scripts` clean.
No behaviour changed: no event, no column, no status, no path, no class. `Disclosure.failed`
is still a separate list and 4c's `effect_failed → blocked` mapping is untouched — the
collection entry is a thing to decide at Pass 10, not a ruling to apply now.

### rendered from a real journal after the edits

Not quoted from the diff. A scratch run with two committed `fs_write` calls, one `web_fetch`
that returned a failure, and one `reminder_set` left open:

```
Going back to seq 2 of this conversation. The run it came from is kept exactly as it was.

Since that point I made:
  - 2 fs_write calls, all under /home/dylan/notes/
      · fs_write(path=/home/dylan/notes/lease.md, content=xxxx…)
      · fs_write(path=/home/dylan/notes/landlord.md, content=xxxx…)

I may also have made:
  - 1 reminder_set call
      · reminder_set(text=chase the landlord, at=18:00)
          interrupted, and never reported back - whether it happened is not known

(1 web_fetch call after that point returned an error.)

Reverting the conversation. I have not undone any of the above, and I cannot: a fork rewinds
what was said, not what was done.
```

And the empty case:

```
Going back to seq 2 of this conversation. The run it came from is kept exactly as it was.

The journal records no effecting call after that point, so as far as the journal shows,
nothing outside this conversation was changed by the part I am rewinding.
```

### the one thing edit 3 surfaced, not yet ruled

**A failed call is now the only call in the disclosure that is not named.** Under the old
wording that was defensible — the line claimed the call changed nothing outside, so there
was nothing to act on. Edit 3 withdraws exactly that claim, which leaves a line saying
something *may* have happened while withholding the argument that would let the reader check:

```
(1 web_fetch call after that point returned an error.)
```

Every other block names its calls with `LEADING_ARGS` pulling the URL or path to the front,
because of Dylan's own ruling that a prompt the user cannot act on trains blind confirmation.
The wording above is his, verbatim, and is implemented verbatim; whether the failed block
should name its calls the way the other two do is his to settle. Nothing in Pass 5 depends on
the answer.
