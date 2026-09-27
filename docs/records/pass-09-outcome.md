# Pass 9 — Result Verification & Replanning — outcome

Sessions completed: **9a**, **9b**, **9c**, in one sitting. 9d is untouched and appends to
this file.

> The three were run together rather than as three `/clear`-to-`/clear` cycles, on Dylan's
> instruction at the planning boundary. The cost that buys is stated in the deviations
> below: none of the three was measured against live behaviour before the next one was
> built on it, which is exactly what 9d is for.

---

## what shipped

### Session 9a — the ledger and the verifier

**`src/agentd/agent/verification.py`** (new). A fold over journal events and six checks over
the result of that fold. No model call, no I/O, no network: `verify` is arithmetic and string
matching over events the worker already wrote.

```python
ENTRY_VERSION = 1
ledger_from_events(events, *, worker_id=None, path_args=None) -> WorkerLedger
verify(result, ledger, *, brief="")                          -> tuple[Flag, ...]
validation_of(flags)                                         -> "valid" | "uncertain" | "invalidated"
HARD_FLAGS = {tests_not_run, file_not_read, status_conflict}
```

`ledger_from_events` joins `tool_requested` → `tool_finished` / `tool_failed` on `call_id`.
`ToolCall.ok` is **tri-state**: `None` is a call that was requested and never terminated,
which is neither a success nor a failure and must not be counted as either. `path_args`
comes from `Tool.path_args` — the same metadata the policy engine's root check reads — so
this module keeps no second list of argument names.

`Flag(code, detail, hard)` lives in `results.py`, so `verification.py` imports one way only.
Every `detail` is written by the runtime, quoting the journal; nothing in a flag comes from
model output, which is the rule that makes flags safe to render for the orchestrator while a
transcript is not.

**`WorkerResult`** gained `validation: str = "valid"` and `flags: tuple[Flag, ...] = ()`.
`validation` is checked in `__post_init__` against `journal/events.VALIDATION_STATUSES`, the
way `status` already was against `WORKER_STATUSES`. `for_orchestrator()` **always** emits
`validation` and emits `validation_flags` (the sentences) when there are any.

**`run_subagent`** keeps the events it already drains. `absorb()` appended to a list; after
the report is validated, the ledger is folded from that list and the flags are attached with
`dataclasses.replace`. Nothing is read back from the store and nothing is read twice.

**Journal vocabulary 24 → 25.** `worker_verified`, emitted before `worker_finished`:

```
worker_id, name, validation (enum), flags[], details[], entry_version,
tool_calls, failures, denials, files_touched, urls_touched, shell_runs,
last_shell_exit (int, nullable), turn_status, report_valid
```

`worker_finished` gained `validation` (enum) and `flags[]`, both **required**, the way 6b
made `report_valid` required and for the same reason. `journal/render.py` renders a
verification only when it found something.

### Session 9b — what an invalidated result costs

Per Dylan's ruling, **tool success and validation status are separate axes.** `delegate`
still returns `ok = (status == "completed")`; an invalidated result does not flip it. What it
does cost:

| | |
|---|---|
| not an answer | `INVALIDATED_BLOCK` is appended to the tool result, naming the flags and the three moves. It carries `observations.RUNTIME_NOTE`, so the one tuple that means "the runtime wrote this" already refuses it into a handoff and into a durable belief. |
| not cacheable | `result_cache.remember` refuses `invalidated`. |
| no proposals | `candidate_memories` from an invalidated worker are dropped in `run_subagent`, and `worker_finished.candidates` counts what was actually proposed. |
| structured | `ToolResult.data` gained `validation` and `flags`, so anything downstream matches a code rather than the wording of a sentence. |

The orchestrator prompt gained one bullet under **Truthfulness**. The runtime does **not**
re-delegate on its own: a runtime that did could loop on a false positive with nobody
watching.

### Session 9c — the escape hatch

`[agent] escape_step = true` (new, shipped on). A turn that spends `max_steps` gets one extra
iteration in which the offered set is `ESCAPE_TOOLS ∩ tool_schemas` — `{"delegate"}` — and
which carries `ESCAPE_NUDGE`, followed by the tool-free `FINAL_NUDGE` step it would have had
anyway. Never granted at `max_steps = 1`, where it would take the model's only working step.

`abandoned` is now `not ended_by_choice` rather than `steps >= max_steps`. `ended_by_choice`
is set only by a step that was *offered* tools and requested none.

`ToolExecutor._count_denial`, keyed `(tool, rule, args-fingerprint)` per turn, fills
`attempt` on a denied result. That makes the existing `STUCK_LIMIT` withdrawal fire on a
verbatim repeat, with `DENIED_NUDGE` instead of `STUCK_NUDGE` — a refusal is the rule's
answer about the action, and telling the model it was an argument problem would send it back
to rephrase a call that has already been ruled on.

---

## what deviated from the plan, and why

1. **The plan said `remember` refuses anything not `valid`. It refuses `invalidated` only.**
   The stricter rule was implemented first and the suite found its cost immediately: eight
   result-cache tests went red because a scripted worker makes no tool calls, draws the soft
   `completed_without_tools` flag, and was therefore never cached. Re-running a worker on
   every hit to re-derive a doubt that is already known costs a worker for nothing. What must
   not happen is the doubt being **lost** in the copy, so `validation`, `flags` and `details`
   now travel in the cache entry exactly as `tainted` does, and `entry_version` went 1 → 2.
   A v1 entry is skipped by the fold rather than read — it was written before anything
   checked it, and serving it as `valid` would assert a check that never ran.

2. **`ENTRY_VERSION` bumped, which the plan did not anticipate.** Consequence of 1.

3. **The escape step is refused at `max_steps = 1`.** Not in the plan; found while reading
   the arithmetic. At a budget of one the escape step *is* the first step, so the model would
   have been offered `delegate` instead of its only working call.

4. **Six checks shipped, as planned, but `no_attempt` and `completed_without_tools` are
   mutually exclusive** rather than both firing on a zero-call worker. Two flags for one
   situation is noise, and the details differ only in which claim they are about.

5. **`verify` refuses to run at all when `report_valid` is False.** Not in the plan. The
   answer in that case is a sentence `results.py` wrote, and running the text checks over it
   is the verifier flagging its own prose.

6. **The numbering.** Filing this as Pass 9 renumbered Discovery to 10 and Evaluate to 11.
   The eight existing outcome records were annotated, not rewritten — a record describes what
   was true when it was written. The mapping is in `session-ledger.md` under Carried forward
   and at the top of each moved plan file.

7. **`docs/plans/pass-11-evaluate.md` gained the three items Dylan ranked below daily use**
   (a planning layer, parallel subagents, delegation overrides), with his reasoning, so they
   are declined rather than forgotten. Parallel subagents is explicitly logged as
   **unmeasured**, which is why it is a tuning question and not a decision.

Nothing in the pass file's `Must not` was crossed: no model call in the verification path,
every check names the journal event it reads, the runtime never re-delegates on its own, and
the `blocked`/`uncertain` → `ok=False` mapping is untouched.

---

## what is now true about the code that was not before

- **A worker's report is checked against that worker's own journal, on every delegation.**
  Before this pass the runtime read exactly two things out of the events it was already
  draining — `tool_failed` for the transcript and `agent_finished` for `budget_exhausted` —
  and discarded the rest.
- **A `completed` result can be refused without being called a failure.** `status` and
  `validation` are independent, and `worker_finished` carries both.
- **The result cache cannot launder a doubt.** It could not launder taint before; it can now
  not launder a verification either.
- **An invalidated worker proposes no memories.** That path reached `candidate_memories`,
  which the daemon's consolidator re-derives from.
- **A turn that used its whole budget and then answered is `completed`.** The old test read
  the budget being *used up* as work being unfinished. The daemon heartbeat runs at
  `max_steps = 4` and used all four on both of its successful runs, so it could never report
  anything but `abandoned` — the agenda has been filing its answers as summaries of
  unfinished work by construction.
- **A policy denial can end.** Nothing in the loop used to tell a model that a `deny` is
  final, which is the other half of the same heartbeat finding.

### Measured

| | |
|---|---|
| suite | 1029 → **1067 green**, `uv run ruff check .` clean |
| new tests | 38 (25 in `tests/test_verification.py`, 3 delegation, 3 cache, 7 loop) |
| vocabulary | 24 → 25 types, 23 → 24 emitted; `tool_progress` is still the only one nothing writes |
| escape-step cost | **+1 model call**, and only on a turn that spends its whole tool budget. Zero on a turn that ends early, which is nearly all of them. |
| verification cost | one list of events already in memory, and no store read |
| mutations | 6 applied, **6 caught** (below) |

### Mutations

Each was applied to shipped code, run against a targeted selection, and reverted. A survivor
would have been the finding; there were none.

```
every flag soft                      → test_a_green_test_run_claimed_with_no_test_run_is_invalidated
a ledger that sees nothing           → test_the_ledger_joins_a_call_to_how_it_ended
cache keeps an invalidated result    → test_an_invalidated_result_is_never_cached
denials keyed without the arguments  → test_a_denial_with_different_arguments_does_not_withdraw_the_tool
a worker reads the whole run         → test_one_workers_events_are_read_out_of_a_run_that_holds_two
the escape step never ends by choice → test_a_turn_that_answers_on_its_escape_step_is_completed_not_abandoned
```

### Live data

Nothing in this session wrote to `~/.local/share/agent/journal.db` (its `-wal` was last
written 90 minutes before the first test run and did not move) or reached the model endpoint:
every worker in the tests is a `FakeProvider`, and the `cfg` fixture's monkeypatched
`get_config` covers `verification.py` transitively — it calls `get_registry()`, not
`get_config()`.

---

## schemas as actually implemented

```python
# agent/results.py
@dataclass(frozen=True)
class Flag:
    code: str
    detail: str          # runtime-written, quotes the journal
    hard: bool = False

# WorkerResult, new fields
validation: str = "valid"          # valid | uncertain | invalidated
flags: tuple[Flag, ...] = ()

# for_orchestrator() adds
"validation": str                   # always
"validation_flags": list[str]       # the details, only when non-empty
```

```python
# agent/verification.py
@dataclass(frozen=True)
class ToolCall:
    name: str; args: dict; ok: bool | None; error: str; exit_code: int | None

@dataclass(frozen=True)
class WorkerLedger:
    calls: tuple[ToolCall, ...]; turn_status: str; steps: int
    files_touched: frozenset[str]; urls_touched: frozenset[str]
    # properties: failures, denials, shell_runs, last_shell_exit, succeeded(*names)
```

```
# journal/events.py — worker_verified
worker_id       str            name          str
validation      str (enum)     flags         list        details      list
entry_version   int            tool_calls    int         failures     int
denials         int            files_touched int         urls_touched int
shell_runs      int            last_shell_exit int|None  turn_status  str
report_valid    bool

# worker_finished, added
validation      str (enum)     flags         list        (both required)

# worker_result_cached, added at entry_version 2
validation      str (enum)     flags         list        details      list
```

The six checks, as implemented:

| flag | class | fires when | row |
|---|---|---|---|
| `tests_not_run` | hard | a passing test run is claimed and no `shell_exec` matching a test **runner** finished ok | B12 |
| `file_not_read` | hard | an evidence line names a path not touched by any `fs_*` call, not a substring of any shell command, and not in the brief | B10 |
| `status_conflict` | hard | `completed` while the worker's own turn ended `failed` or `cancelled` | B11 |
| `no_attempt` | soft | the report says something could not be accessed and the worker made zero tool calls | B16 |
| `completed_without_tools` | soft | `completed` with zero tool calls, and `no_attempt` did not already fire | 8d ×4 |
| `write_not_performed` | soft | `actions_taken` claims a change with no successful `fs_write` and no successful `shell_exec` | — |

`abandoned` is deliberately **not** a `status_conflict`: `results.validate_report` already
forces a budget-exhausted worker to `uncertain`, and flagging it here would report one fact
as two contradictions.

---

## deferred items, and where they went

- **9d, the measured run.** Filed in `docs/plans/pass-09-verification.md` and pending in the
  ledger. The number it exists for is the **false-positive rate per flag**, and the pass
  file's `Must not` already says a hard flag does not stay hard without one.
- **The final report call itself.** This pass verifies the report; it does not question
  whether a second inference over `text[:20000]` should be producing it. That remains the
  Pass 11c tuning question, annotated in place in `pass-08-outcome.md`.
- **`daemon + delegate(researcher)` is still `allow`.** Untouched here — it is a policy hole
  and not a verification question. Still filed in `pass-11-evaluate.md` with its marker.
- **Eval runs still land in `facts`.** Untouched, and still blocking for the retrieval pass
  (now Pass 10).

---

## open questions for later passes

1. **What is each check's false-positive rate?** Unknown, and three of the six are hard on
   the strength of a measured shape rather than a measured rate. The known false-positive
   shapes, written down before 9d can measure them:
   - `tests_not_run` — a report that describes an existing green suite without claiming to
     have run it ("the tests pass on main, so the bug is in my change") reads as a claim.
   - `file_not_read` — a worker that finds a path through a tool whose argument is not
     declared in `path_args`, or that cites a path it derived rather than opened.
   - `completed_without_tools` — a brief genuinely answerable from its own text.
   - `write_not_performed` — a change made through a tool family that is not `fs_write` or
     the shell.
2. **Does the escape step ever get taken?** The mechanism is tested; whether a 27B reaches
   for `delegate` when it is the only tool on the table is a measurement, and its answer
   decides whether the extra model call is worth what it buys.
3. **Does `ended_by_choice` change the heartbeat's recorded status in practice?** The
   arithmetic says yes. The daemon has not been restarted against this build.
4. **Should an `uncertain` validation be visible to the user, or only to the orchestrator?**
   Today it reaches the model and the journal, and the CLI renders a flag line. Nothing
   surfaces it in the final answer, so a relayed-but-doubted claim looks exactly like a
   corroborated one to Dylan.
5. **Is `worker_verified` the right place for the ledger's counts, or should the ledger
   itself be foldable?** It carries counts, not calls — the calls are already a few hundred
   events back in the same journal. A replay harness (11a) may want the fold rather than the
   summary.
