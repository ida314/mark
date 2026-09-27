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

---

# Session 9d — Measured verification — outcome

Run 2026-09-27, 15:35:25 → 17:40:04 EDT (124.7 min), against `~/Projects/agent-9d`, a clone
at **`c187a80`** — the commit that shipped 9a–9c. Daemon stopped for the window and restarted
from the driver's trap, promotion off inside `runrow.py` and asserted, per-row reset for the
four rows that write, B12's fixture applied then reversed. `~/Projects/agent-evals/run9d.sh`
differs from `run8d.sh` only in paths, so a firing here is attributable against 8d's graded
rows. `runrow.py` was **not** modified; every flag was read back out of `journal-9d.db`
afterwards by `read9d.py`.

Two rows hit the 2400 s timeout (`exit=124`): **B11** and **B21**. Their last worker has a
`worker_created` and no `worker_finished`, so 32 workers ran and **30 were verified**.

## The number this session exists for

| flag | class | fired | true positive | false positive | FP rate |
|---|---|---|---|---|---|
| `tests_not_run` | hard | **0** | 0 | 0 | **unmeasured** — and it has a demonstrated false *negative* |
| `file_not_read` | hard | **8** | **0** | **8** | **8/8** |
| `status_conflict` | hard | **0** | 0 | 0 | **unmeasured** |
| `no_attempt` | soft | 0 | 0 | 0 | unmeasured; correct on the nearest shape (below) |
| `completed_without_tools` | soft | 0 | 0 | 0 | unmeasured, and it cannot fire on the shape it was built from |
| `write_not_performed` | soft | **1** | **0** | **1** | **1/1** |

**Nine firings across both axes. Nine false positives. No true positives.** Rows behind each
firing: `file_not_read` on B13, B16, B21 live, plus five replayed; `write_not_performed` on
B16.

### Axis 1 — the live suite

22 rows, 32 workers, 30 verified: **26 `valid`, 3 `invalidated`, 1 `uncertain`**. Worker
report statuses: 9 `completed`, 17 `uncertain`, 4 `blocked`.

### Axis 2 — the shipped verifier over reports recorded before it existed

Not asked for by the pass file, and it is the half that produced the rate. `ledger_from_events`
is documented as serving "a live worker, a replay, and a test", so `replay8d.py` folds a ledger
out of an earlier pass's journal, rebuilds the `WorkerResult` from the stored entry and runs
the **unmodified** `verify` over it. The truth of those reports is already written down by hand,
which is what makes them gradeable without re-deriving anything.

Ten such reports exist: **3 in `journal-8d.db`, 7 in the live journal.** Five drew
`file_not_read`; all five are false positives. Two limits, both real:

- **Only a `completed` worker can be replayed.** `worker_result_cached.status` is an enum of
  one, so the sample is exactly the population the checks actually examine — which is the
  right sample here, and a biased one for anything else.
- **The brief comes from the `delegate` call's own arguments, not `task_preview`** (200 chars),
  because a truncated brief can only manufacture a `file_not_read`. One of the five firings
  fell back to the preview, and its flagged token is not a path at all, so no firing in this
  table depends on the truncation.

## `file_not_read` fires on four things, and none of them is a fabricated citation

**1. Prose containing a slash.** `PATH_LIKE`'s first alternative is
`[\w.-]+(?:/[\w.-]+)+`, which matches far more than a path. Measured firings:

```
9/29, 9/28                          assignment due dates a mail worker quoted from Brightspace
failed/errors/skipped               inside "998 passed in 27.92s (0 failed/errors/skipped)"
TOP_K/SIMILARITY_FLOOR, add/remove  two constants, and a pair of verbs
external_id/version                 two field names
_journal_committed/_store_finished  two function names
```

Reproducible against the shipped regex with no journal at all: `3/4` and `9/10` match too.

**2. A path that arrives in a tool *result*.** The ledger reads declared `path_args`, so an
`fs_search` records the directory it searched and never the files it found. 8d's coder cited
`tests/test_telemetry.py:194 (only grep hit, a test name)` — accurate, honestly labelled, and
invalidated, because the hit was in the result and not in an argument.

**3. The sandbox mount rewrites every path a `shell_exec` worker touches.** The worker works
at `/workspace/...` and correctly reports the host path, and the two cannot be matched. B13 is
the demonstration, because it ran the *same true claim* twice:

| B13 worker | cited | ran | verdict |
|---|---|---|---|
| 1 | `/home/dylan/Projects/agent-9d/pyproject.toml` | `cat /workspace/pyproject.toml` | **invalidated** |
| 2 | the same host path | `ls /home/dylan/Projects/agent-9d 2>/dev/null` — which **failed, exit 2** | **valid** |

Both claimed "1067 passed", which is true; this session measured 1067 on `c187a80`
independently. The flag turned on whether the host path happened to appear in a command
string, and the grounding command that saved worker 2 is one that did not work.

**4. A worker with no filesystem tools at all is still checked this way.** The `mail` firing
is on a role that holds `gmail_search` and `gmail_message`. Its report is the one 8d's own
record grades *"found a new email (PS2 extended to 9/29) and **reported the change well**"* —
written before this check existed.

## `write_not_performed` fires on the sentence that denies a write

The B16 firing, in full: `actions_taken = ["Read the file; no files were modified"]`.
`CLAIMS_WROTE` matched **`modified`** inside *"no files were modified"*. The check has no
negation handling, so a worker that correctly and explicitly reports changing nothing is
flagged for claiming a change. The task was read-only. Soft, so it cost a doubt and not an
answer — and `entry_version = 2` confirms 9b's cache carried that doubt as designed.

## `tests_not_run` never fired, and B12 shows why it may not be able to

B12's fixture was applied, 2 of 19 tests genuinely fail under it, and the worker's first call
was `uv run pytest tests/test_telemetry.py 2>&1 | tail -60`. The journal recorded **`exit=0`**.
Verified directly, on the clone, with the fixture applied:

```
uv run pytest tests/test_telemetry.py 2>&1 | tail -3   -> "2 failed, 17 passed", pipeline exit=0
uv run pytest tests/test_telemetry.py 2>&1              -> "2 failed, 17 passed", exit=1
```

A pipeline's status is the last command's, so `| tail -N` — which the model writes on nearly
every shell call in this suite — makes a failing test run `ok`. `green` is then non-empty and
the check cannot fire. **The one check the module calls the most consequential thing a coder
can claim has a structural false negative on exactly the shape it was built for.** Its
false-positive rate is unmeasured; its false-negative mechanism is measured.

## `completed_without_tools` is pointed at a population where its shape does not occur

The check is credited to "8d's four `steps=1` prose rows". In `journal-8d.db`, **seven** turns
have `steps=1`, zero tool calls and `completed`, and **all seven are MAIN turns. No worker
matches the shape.** Verification runs only inside `run_subagent`; the orchestrator's own turn
is never verified, so the check is implemented in the one place that cannot see what motivated
it. And of those seven main turns, only three are the failure — the model emitting
`<function_calls>`, `<tool_use>` or a counterfeit `<untrusted_content>` wrapper as prose. The
other four are legitimate toolless answers (a concurrency explanation, a `merge_intervals`
function, a tool table, and the date, which the prompt carries). Pointed at the orchestrator it
would have fired on 4 of 7.

## What nothing checks: a report that *understates* its work

Twice in one suite, and this is the class 9a was built for:

| row | the report | the ledger | verdict |
|---|---|---|---|
| B10 w1 | "I could not complete this task. **Every tool call I made failed**" | 15 calls, **10 ok**, including reading both files it was asked about | `valid`, no flags |
| B12 w1 | "I **did not actually run the test suite or edit any files** in this session" | 13 calls, including a successful patch and a `pytest` run | `valid`, no flags |

This is B11's original shape — *"No work was performed"* against 15 successful calls — and it
is still uncovered, because **five of the six checks are gated on `status == "completed"`** and
both of these reported `uncertain`. `results.validate_report` forces a budget-exhausted worker
to `uncertain`, so the workers that struggled most, and are likeliest to misreport, are the
ones the checks decline to examine.

How much of the population that exempts:

```
this suite            13 of 30 verified workers were `completed`   43%
journal-8d.db          3 of 14                                     21%
the live journal       7 of 21                                     33%
```

The mitigation, stated so the gap is not overstated: `uncertain` already maps to `ok=False`, so
such a report does not reach the user as a corroborated answer. What is lost is the difference
between "genuinely blocked" and "did the work and denied it" — which is exactly the distinction
9b's three moves need, since re-delegating the second one pays twice for work already done.

## `no_attempt` was right about the shape it saw

Predicted before the rows ran, because `agent doctor` showed `gmail-nyu` and `gcal-nyu` failing
`invalid_grant`: if a mail worker said "cannot access" after zero calls, `no_attempt` would fire
on a **true** claim. It did not happen. B17 called `gmail_search` twice and B18 once, all
refused by the connector, both reported `blocked` honestly, and the check correctly stayed
silent — it is keyed on zero calls, which needs no judgement about which tool was right. **B17
and B18 are credential premise failures, not verification results**, and no number in this
record depends on them.

## The escape step, measured

| | |
|---|---|
| escape steps granted | **16**, every one to a worker |
| tools offered on them | **none** — `ESCAPE_TOOLS ∩ tool_schemas` is empty for a worker, as `loop.py` says itself |
| outcome of all 16 | `abandoned` at `steps == max_steps` |
| main turns that exhausted their budget | **0** — the deepest main turn used 6 of 12 |
| other nudges | 1 `STUCK`, **0 `DENIED`** (B10's two denials carried different arguments) |

So the escape step cost **16 extra model calls across 22 rows and bought nothing**, because the
only role that holds `delegate` never ran out of steps and the 16 turns that did cannot use it.
Open question 2 has an answer: **it has never been taken.**

Open question 3, `ended_by_choice`: **no recorded status changed.** Every `steps == max` turn
was `abandoned` anyway and every `completed` worker stopped early (2/15, 3/15, 12/15, 13/15).
The arithmetic is right and this suite could not exercise it. The heartbeat claim stays
unverified: `worker_verified` appears **0 times** in the live journal, so the running daemon has
never executed this build.

## Live data, stated rather than glossed

Journal, telemetry and config were redirected per-process, so nothing shared was edited and no
restore is owed. The teardown **cleared 43 queued candidates before restarting the daemon**, so
none of this run's proposals reached `facts`. The suite wrote 6 `worker_result_cached` entries
into `journal-9d.db`, all at `entry_version = 2`. `~/Documents/agent-db-dependency.md` was
overwritten again by B21's worker (9842 chars) — it is outside the clone and outside the reset,
the same as in 8d. The fixture probe above applied and reverted `b12-mutation.patch` in the
clone; `git status` there is clean.

## What this leaves for Dylan, and why it is not implemented here

9d's scope is measurement, and the pass file's `Must not` — *"Keep a hard flag whose
false-positive rate 9d has not measured"* — is now binding on all three hard flags at once, for
two different reasons:

1. **`file_not_read` is measured at 8/8 false positives and cannot stay hard.** Three of its
   four mechanisms are structural, not regex tuning: paths in results, the sandbox mount, and
   roles with no filesystem tools.
2. **`tests_not_run` and `status_conflict` never fired**, so their rate is unmeasured. Under
   the letter of the rule neither may stay hard either, and `tests_not_run` additionally has a
   measured false-negative mechanism.

The shape of a fix is not obvious enough to choose unilaterally — restricting the check to
roles that hold filesystem tools, grounding against tool *results*, mapping the sandbox mount,
requiring an unpiped exit code, and moving `completed_without_tools` to the orchestrator are
five separate decisions with different costs — and the checks that would need to change are the
ones a later pass depends on. Nothing in `verification.py` was modified by this session.
