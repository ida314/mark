# Pass 4 — Checkpoints, Resume, Fork — outcome

Sessions completed: **4a**. 4b (resume and reconciliation), 4c (uncertain status, a human
hard stop) and 4d (fork) are untouched, so the pass's exit criteria are **not** met: nothing
reads a checkpoint, `orphaned` and `uncertain` are still written by nobody, and there is no
fold and no resume. What 4a leaves behind is the thing those three sessions stand on — a
snapshot written at the boundaries, behind a flag that is **off in the shipped config**.

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
