# Pass 8 — Tool Surface Reduction — outcome

Sessions completed: **8a**. 8b, 8c and 8d are untouched and append to this file.

The pass's own exit criteria — target surface reached, no regression against baseline, every
moved tool reachable through a durable role — are **not yet met**, and cannot be until 8b and
8c have moved their families. What 8a settles is one family, one role, and the question of
whether the boundary holds at all.

The first part of this record is **pre-8a**: the four fixes the pass turned out to be blocked
on and the three findings that constrained what 8a was allowed to claim. It is kept because
the second part only reads as a small change against it.

---

# Pre-8a

## What Pass 8 was blocked on, and why it took four commits

The pass file says "do not start this pass until Pass 6 is done", and Pass 6 was done. What it
could not anticipate is that **delegation had never once executed against this model**, so
"move a capability family to a durable role" had no working path to move anything onto. Four
independent defects sat between the tool surface and a worker that could do the work:

| sha | defect | how it presented |
|---|---|---|
| `d02a559` | **pre-8a fix 1** — `extra_system` inserted a second system message at index 1 | every worker turn 400-ed at step 1, `llm_ms: 0` |
| `c9aa84f` | nothing ever wrote `ctx.extra["approver"]` | every delegated worker got a `QueueApprover`; all writes denied |
| `91fa9c6` | no `[paths] project`; the sandbox mounted the empty workspace | the coder could not find the repo, and could not run a test in it |
| `a8639a8` | the sandbox image had no `uv`, no `pytest`, no reachable Postgres | the suite skipped entirely, and "skipped" reads like a pass |

Each was invisible to the test suite. Fix 1 was defended by **nothing in 968 tests**; fix 2 by
nothing, because three tests set `extra["approver"]` by hand and called the tool handler
directly; the project path by nothing, because `conftest` leaves `project` unset; and the
sandbox by nothing, because no test has ever been `-m docker` marked.

**Both fixes landed before the measurement, which is the point of listing them together.**
`d02a559` — pre-8a fix 1 — was committed 2026-09-23 18:10, before this session opened; it
carries two tests (`tests/test_agent_loop.py`, `tests/test_subagents_and_consolidation.py`),
was mutation-checked by restoring `messages.insert(1, ...)` (**2 failed, 966 passed — zero of
968 pre-existing tests defended it**), and was probed against vLLM on :8001 both ways:
`roles=['system','system','user']` → HTTP 400, `roles=['system','user']` → 200. The other
three landed in this session, each verified alone in a detached worktree rather than trusted
for being green beside the others.

## The live probe, which is the evidence the chain works

One delegation to `coder` against a throwaway clone, real endpoint, real journal, real image:
`fs_list` on an absolute project path (ok), `fs_read` (ok), `shell_exec "uv run pytest"`
(**exit=0, 28.3 s**). **Zero `tool_failed` events.** The comparable probe 14 hours earlier made
fourteen tool calls and every one failed.

It was interrupted before `worker_finished`, so the report schema, the result cache and the
task boundary were not exercised on that run, and no graded test count was produced. Full
detail, including what the probe does *not* establish, is in the session ledger.

## Why the sandbox tmpfs is 512m, in the record rather than only in the commit

`builtin_shell.docker_command` mounts `--tmpfs /tmp:rw,size=512m`, raised from 256m. The
reason is a reproduction, not a margin: PGDATA lives on that tmpfs because the container root
filesystem is read-only, and pytest's `tmp_path` roots live there too. One suite run peaks at
**188MB**, so 256m looks sufficient and survives exactly one run. Reproduced directly, twice,
in the real `docker_command` flags:

| tmpfs | run 1 | run 2 | run 3 |
|---|---|---|---|
| 256m | 983 passed | **`18 failed, 664 passed, 301 errors`**, tmpfs 100% | — |
| 512m | 983 passed | 983 passed | 983 passed, peak 352MB |

**The second row is the whole argument.** A worker whose job is to iterate on the suite runs
it more than once per container, and at 256m the second run returns a large, confident,
entirely wrong result. That is this codebase's named failure mode — a plausible answer where
there should be an error — and it is worth 256MB of tmpfs to remove. 512m is the smallest
round size that holds three consecutive runs.

The other measured costs of the self-contained image, accepted at the same ruling: **+0.19s on
every `shell_exec`** (0.12s → 0.31s for `ls`), paid by trivial commands too, and the image
going **270MB → 1.05GB**. The pre-8 image is tagged `agent-sandbox:pre-8` (`b65b2239cbca`), so
a rollback is one `docker tag` and not a rebuild from git history.

## Three findings that bind 8a

**1. "Not offered" does not mean "cannot call", so the pass's central verb needs a mechanism.**
Four doors put a tool in front of a turn: `Registry.select` (`always_on` + session-used +
top-k similarity), `AgentLoop._with_lookup`, `tool_search` via `ctx.extra["added_tools"]`, and
— the one that matters — `agent/loop.py:699-700`, which adds **any registered tool the model
names** to `exposed` and runs it. `tools/executor.py` performs no visibility check. Probed
with `select` forced to return `[]`: `offered=[] registered=yes ran=True`.

Honest limit: all **258** real `tool_requested` events in the live journal are
`visible=1, known=1`, so this has never fired in the wild. It is a hole in what 8a can
*guarantee*, not an observed behaviour — and 8a must not write it up as the model reaching
around its surface.

The mechanism that closes all four at once already exists and is the one workers use: a
worker's loop is built over `Registry(tools=registry.subset(...))`, so a name outside the
subset is not in `self.registry.tools` and door 4 cannot open. The orchestrator is built with
the full `get_registry()` (`agent/loop.py:300`). **The symmetric fix is to give the
orchestrator a subset, not to add visibility flags to `select`** — and 8a's exit criterion
"every moved tool reachable through a durable role" has a negative half that nothing currently
tests: *and not reachable any other way*.

**2. The number 8d compares against is 20, not 13.** Measured from live telemetry (46 records
with `role == "main"`): the orchestrator is offered **20 tools on 39 of 46 real turns**, 19 on
six and 18 on one, out of **29** enabled tools — it was 26 at the Pass 1 baseline. Thirteen are
`always_on`; the other seven arrive by embedding similarity. "Permanent surface" in the pass
file reads as the `always_on` set, but what a turn pays for in tokens and in selection error is
the offered set, and they differ by seven. **Both belong in 8d's table.**

Also: the pass file's target surface lists `reminder_set`, `watcher_add` and `open_loops_list`
as permanent and **none of the three is `always_on` today**, so 8a–8c are not purely
subtractive, and the borderline cases it defers to measurement (`goal_upsert`,
`open_loop_add`, `open_loop_close`, `profile_read`) start from a mixed state.

**3. The unattended paths cannot write to the newly-mounted repo, and one attended path can.**
Every unattended caller builds `QueueApprover` — `daemon/heartbeat.py:123`,
`daemon/scheduler.py:55`, `daemon/telegram.py:331`, and `cli/app.py:627,1626` for `agent ask`.
`CliApprover` is constructed in exactly one place, `cli/chat.py`. So fix 2 hands a heartbeat
worker an approver that queues and denies, which is the shape the gate-2 ruling wanted, now
holding by construction on both sides rather than by the accident that nothing wrote the key.

**A worry this record raised and then disproved.** It first said that under
`agent chat --autonomy act` a delegated coder's `shell_exec` might resolve to `allow`, since
the role's `autonomy_cap` is `"act"`. Evaluated against the real engine and the shipped
`config/policy.default.yaml`, that is wrong: `shell_exec` and `fs_write` are **deny** at
`observe` and **require_approval** at both `assist` and `act`. **No autonomy level lets a
write to the mounted repo skip the approver.**

The heartbeat is further from it still: `daemon/heartbeat.py:118,130` runs at
`autonomy="observe"`, so `cap_autonomy` caps a heartbeat-delegated coder there, where both
tools are refused by the policy engine before any approver is consulted. The `QueueApprover`
is the second layer, not the first.

## What 8a still needed before it was dispatched

*All of it was done: the attended rows were run and recorded as the `baseline-v2.md` addendum
of 2026-09-24, and 8a ran against them. Kept as written.*

Steps 1 and 2 of the approved order are met, and the sandbox prerequisite is met.

**Step 3 is next and needs a human at the terminal: B11, B12 and B13 under `chat`, attended,
about an hour**, on this tree. They are the coder family's first measured baseline; the tree
has moved sixteen `src` commits since `baseline-v2`. Step 4 records them as an addendum to
`baseline-v2.md` §2, and **that addendum is 8a's comparand**. Step 5 dispatches 8a.

Two things to know while answering those prompts: a worker's approval panel is now magenta and
names the worker (`a4b32e4`), and `[paths] project` is live, so a `shell_exec` a worker asks
for is against the real repository, read-write.

## Deferred, and where it went

- **Everything the pass file actually asks for** — the coder role's establishment as a surface
  reduction (8a), the researcher and web (8b), memory and integrations (8c), and the full-suite
  comparison (8d). None started. *(8a is done; see the second half of this record. 8b, 8c and
  8d stand.)*
- **`delegate(agent="memory")`** is still the one branch of the tool that is not a delegation:
  it packs retrieval in-process and builds no worker. Pass 6 left it for "Pass 8 owns the tool
  surface", and 8c is where it belongs.
- **`-m docker` selects 0 of 987 tests.** `CLAUDE.md` names it as a gate and nothing has ever
  been marked. `tests/test_sandbox_image.py` is static assertions on the Dockerfile and the
  profile script, deliberately not marked: a real docker test needs the image built and has to
  skip cleanly when run *inside* the sandbox, where there is no docker. Flagged, not built.
- **`agent init` / `agent doctor` print the workspace and never the project**, so a
  misconfigured `project` is invisible there. Carried from ruling 1, still true.

---

# Session 8a — the coder role, filesystem and shell

*2026-09-24. Commit `e6a3b83`. Suite 998 → 1008, ruff clean, no typecheck configured.*

`fs_list`, `fs_read`, `fs_search`, `fs_write` and `shell_exec` are off the orchestrator's
surface and belong to the `coder` durable role, which already held all five. This session
moved a boundary; it did not build a destination.

## what shipped

| file | what changed |
|---|---|
| `src/agentd/tools/surface.py` | new. `MOVED_TO_ROLE` — the one record of which family left and who owns it |
| `src/agentd/agent/loop.py` | an unstated `tool_subset` now means *orchestrator*, and resolves to the registry **minus** the moved families |
| `src/agentd/agent/subagents.py` | `run_subagent` states the worker's surface outright; `CODER_EXPLORE` defined |
| `src/agentd/tools/executor.py` | `MOVED_OUT` — the refusal names the owning role and the delegation |
| `src/agentd/agent/prompts/main.md` | the orchestrator is told it has no file tools and no shell |
| `src/agentd/tools/builtin_delegate.py` | `coder` is no longer "multi-file code work" |
| `src/agentd/cli/app.py` | `agent tools list` grows an owner column |
| `tests/test_tool_surface_pass8.py` | new, ten tests, against the real registry and the real default |
| five existing test files | fifteen loops that used `fs_*` as a stand-in now state their surface |

**The narrowing is a subtraction, never a list.** A hand-written list of what the orchestrator
keeps would lock out every tool registered after it was written — an MCP server's, a test's
probe — and the failure would read as "that tool does not work here" rather than as a surface
decision. `test_a_tool_registered_after_the_loop_was_built_is_still_on_its_surface` is the
guard, and it is the same mistake the enforcement commit already made once by snapshotting the
subset in `__init__`.

**An unstated surface means orchestrator, and that is the fail-closed direction.** All five
production call sites and `one_shot` pass nothing, so a sixth added later is narrowed without
anyone remembering to ask. The worker is the one that states its surface, because subtracting
the moved families from a worker's own subset would take the coder's tools away from the role
they moved to — and a worker author who forgets loses tools loudly rather than opening a hole
quietly.

## the two numbers, measured on the six rows rather than asserted

|  | pre-8a | 8a |
|---|---|---|
| permanent (`always_on`) | 13 | **13** |
| permitted — may run at all | 29 | **24** |
| offered per coding turn | 20 on all six rows | **17–20**, mean 18.7 |
| moved family inside the offered set | 3–5 on every coding row | **0 on every row** |
| orchestrator steps used | 1, 12, 12, 10, 4, 11 of 12 | **1, 2, 5, 3, 4, 6 of 12** |
| orchestrator context peak | up to 12 595 | up to 8 799 |

**The offered count barely moved, and that is the finding rather than a disappointment.**
`Registry.select` short-circuits at or below `ALWAYS_EXPOSE_LIMIT = 20`; the permitted pool is
still 24, so the similarity route still runs and simply backfills the slots the filesystem
tools used to hold — with `working_memory_note`, `open_loop_add`, `reminder_set` and the rest.
**8a bought composition, not size.** The offered number cannot fall until the permitted pool
drops near 20, which is 8b and 8c's move, and *that* is what 8d's table has to say. The number
that did move is the orchestrator's own step usage: four of six rows hit the 12-step budget
before, none do now.

## the comparison, against the pre-8a addendum in `baseline-v2.md`

Same six rows, same frozen prompts, same harness file, same recording approver, same model.
Run against **`~/Projects/agent-8a`, a clone at `e6a3b8375053fca9162890f6daadee2c72ff7956`**,
with `[paths] project` pointed at it and the daemon heartbeat silenced — the identical method
the pre-8a rows used, per the ruling that both sides be measured the same way.

| row | pre-8a | 8a | latency | workers | what changed |
|---|---|---|---|---|---|
| B02 | **pass** 9.7 s | **pass** 8.4 s | −13% | 0 | unchanged, and it still does not delegate — the boundary 8a preserves deliberately |
| B10 | **partial** 94.8 s | **pass** 233.7 s | ×2.5 | 1 | named `Registry.select` *and* all three caps (20 / 0.30 / 8). The baseline never identified the numeric caps |
| B11 | **fail** 110.6 s | **pass** 874.6 s | ×7.9 | 4 | argument, schema, a test that bites, suite green |
| B12 | **pass** 79.8 s | **pass** 401.9 s | ×5.0 | 2 | the baseline reached green with `git checkout`; this run named the cause and edited the line |
| B13 | **pass** 51.1 s | **pass** 253.6 s | ×5.0 | 2 | "1008 passed, 0 failed" — the true count |
| B21 | **fail** 78.8 s | **pass** 1192.7 s | ×15.1 | 5 | the file exists **at the path it claims**, and its classification is correct |

**3 pass / 1 partial / 2 fail → 6 pass.** Completion rate is not regressed; it is the first
all-pass coding family this suite has recorded. **Total wall clock 415 s → 2 965 s, ×7.1.**

### what was verified rather than believed

The graded claims are this codebase's named failure mode, so three were checked first-hand:

- **B11.** `uv run pytest` in the clone, by this session and not by the agent: **1009 passed**.
  Deleting the two-line filter from `open_loops_list` kills the new test, so it exercises the
  filter rather than passing beside it.
- **B12.** `visible_unused()` at line 175 reads `called = set(self.called_names())`. The fix
  is in the source, not in the test, and not a `git checkout` of the fixture.
- **B21.** `~/Documents/agent-db-dependency.md` exists, 3 674 bytes, and its three tables put
  `memory_*`, the calendar, the coursework feed, all eight agenda tools and `delegate` in the
  breaking column and `time_now`, `web_*`, `fs_*`, `shell_exec`, `gmail_*`, `profile_read` and
  the working-memory tools outside it. That is the rubric's list.

### one real defect the graded pass contains

B11's shipped line is `if older_than_days := args.get("older_than_days"):` — a truthiness
test. The schema it wrote declares `"minimum": 0`, so `older_than_days=0` is a legal argument
that **silently disables the filter instead of applying it**. The rubric does not reach it and
the row is still a pass, but a coder role whose output is graded only by "the suite is green"
will ship this class of thing, and 8d should not read six passes as six correct changes.

## the delegation chain, which had never completed before

Fourteen workers finished across the six rows. **`report_valid` is `True` on all fourteen** —
the 27B produced the result schema every time, which the pre-8a record could not say at all
because the one live probe was interrupted before `worker_finished`. The result cache, the
`worker_finished` boundary and the task-scope discard all ran, fourteen times.

Worker statuses, per row: B10 `completed`; B11 `uncertain ×3` then `completed`; B12
`uncertain`, `completed`; B13 `blocked`, `completed`; B21 `uncertain`, `uncertain`,
`completed`, `uncertain`, `completed`. **Six of fourteen were `abandoned` at 15/15 steps.**

### three behaviours worth naming, two good and one bad

**The orchestrator re-delegates on a failed report, and it works.** B11 took four workers: the
first three spent fifteen steps reading without editing, and the orchestrator narrowed the
brief each time — "let me hand it over as a minimal-edit task" — until the fourth did it. That
is the recovery the architecture wanted and had never been observed doing.

**The orchestrator caught a fabrication before it reached the user.** B13's first worker came
back without running anything and flagged that it had invented numbers earlier; the
orchestrator refused the report and re-delegated with a stricter brief. First time in this
suite that an unverified claim was stopped one layer below the answer.

**A worker's report contradicted its own transcript.** B11's first worker made fifteen tool
calls that read the repository, and its report said *"No work was performed. I did not read the
files, make any edits, add a test, or run pytest."* `report_valid` was `True`; the schema was
satisfied and the content was false in the conservative direction. The final report call sees
only `text[:20000]` of the transcript and is a separate inference from the work — **it is now
on the critical path for every coding row, and it can lose work that was done.** Cheap and
wrong in the other direction is the same bug: nothing downstream can tell.

## ephemeral workers per role

| worker | tools | why it exists |
|---|---|---|
| `coder` (the role itself) | `fs_read fs_list fs_search fs_write shell_exec memory_search`, 15 steps, `act` | the implementation worker. A spec differing from it only in wording would be a second way to phrase a delegation and so a second way to miss 6c's result cache |
| `coder/explore` | `fs_read fs_list fs_search memory_search`, 12 steps, `assist` | **nothing it can call returns `require_approval`** — the one shape of coder work that finishes on a path whose approver queues and denies (`agent ask`, a watcher, the heartbeat) |

A separate test/debug worker was considered and **not** defined: it would hold `shell_exec`
and `fs_read` exactly as the role does and differ only in what its prompt asked for.

**`coder/explore` is not reachable from the model in 8a, deliberately.** `delegate`'s `agent`
enum still lists roles. Exposing it means adding the name to that enum **and** to
`private-data-no-outward-delegation` in `config/policy.default.yaml` in the same edit — the
interlock matches `args.agent.in [researcher, coder]`, and a worker name outside that list is
a delegation the private-data rule does not see. It was held back so 8a's measurement stayed
attributable to the move.

**8a produced the evidence that justifies splitting one out anyway.** The comment in
`subagents.py` names the criterion — "a `coder` run that spends its budget re-reading the repo
before it can run a failing test" — and six of fourteen workers did exactly that. 8d or Pass
10c should act on it.

## the tests, and what defended the claim before them

Ten tests in `tests/test_tool_surface_pass8.py`, built against `build_registry()` and the real
no-argument default rather than a hand-written surface, so that a change to the shipped
default fails here and not only in `test_tool_surface.py`. Seven mutations, each run alone
against the full suite:

| mutation | new tests killed | pre-existing killed |
|---|---|---|
| nothing has moved (`MOVED_TO_ROLE` emptied) | 4 | **0** |
| no subtraction in `tool_subset` | 4 | **0** |
| the refusal does not name the owner | 1 | **0** |
| the worker does not state its surface | 1 | 1 |
| the explorer gets a shell | 2 | **0** |
| moved to a role that does not exist | 1 | **0** |
| the subset snapshotted in `__init__` | 1 | 2 |

**Five of seven were defended by nothing in the 998-test suite.** That is the usual answer in
this pass and the reason the file exists.

## live data, stated rather than glossed

- **Journal:** six graded runs plus one abandoned duplicate. Fourteen `worker_created` /
  `worker_finished` pairs, and **one `worker_created` with no `worker_finished`** — see the
  incident below.
- **Telemetry:** rotated to `telemetry-20260924-baseline-coding.jsonl` before the run, so the
  pre-8a rows and the 8a rows are separate files. Twenty records in the new one: six `main`,
  fourteen `subagent`.
- **Approvals:** 0 rows. The recording approver answers in-process and never queues.
- **The clone `~/Projects/agent-8a` still exists**, at `e6a3b83`, with a clean tree.

### the memory store was polluted by the measurement, and the cleanup is owed

Twelve candidate memories were proposed by the workers and the consolidator, and the review
gate **promoted five of them into `facts`**. Four name `agent-8a` — a throwaway clone — and
three assert that the user's project lives at `/home/dylan/Projects/agent-8a`. Worse, one of
them **superseded a true fact**: `01a0c4d5` *"Dylan is building a personal agent runtime this
quarter."*, recorded 2026-09-21, is now `superseded` by a clone-path restatement of itself.

This is not a new class of bug — it is Pass 7's review gate doing its job on input that should
never have reached it — but **it is the first time an eval run has written a durable false
belief about a path that is about to be deleted.**

**Owed, and blocked in this session:** the delete was refused by the sandbox's mass-delete
classifier, so it is left for Dylan rather than worked around. What it needs, exactly:

```sql
-- 1. restore the true fact this run superseded
update facts set status='active', superseded_by=null, superseded_at=null
 where id = '01a0c4d5-7c58-7bbb-9f17-9bb5a8ec50f5';
-- 2. drop the run's own rows (5 facts, 12 candidates; cutoff is after the pre-8a baseline)
update facts set supersedes=null, superseded_by=null where recorded_at > '2026-09-24 17:50:00+00';
delete from facts             where recorded_at > '2026-09-24 17:50:00+00';
delete from candidate_memories where created_at > '2026-09-24 17:50:00+00';
```

Before: 33 active facts. Expected after: 30, with `01a0c4d5` back among them.

**And a standing rule this argues for, for 8b onwards:** an eval run should not write to the
canonical memory store at all. The worker's `candidate_memories` are proposals, so the cheapest
correct fix is to run the suite with promotion off rather than to clean up after it.

## incidents in this session's own measurement

**1. A duplicate B11 ran for ~40 s alongside the first.** A `pgrep` returned empty while the
first run was still alive, this session concluded it had been killed, and started a second. The
overlap inflates B11's latency by an unknown amount on the shared endpoint. The row is reported
as measured; the number is the weakest of the six.

**2. The duplicate was killed mid-worker**, leaving run `01a0d498` with a `worker_created` and
no `worker_finished`. That is a genuine crash-shaped run in the live journal, from this
session's harness rather than from the system, and it is the shape Pass 4's recovery path
exists for. It has not been folded or resumed.

**Neither incident touched the other five rows**, which ran alone.

## what 8a does not establish

- **Nothing about the offered-set reduction the pass exists for.** 20 → 18.7 is noise; the
  pool is still 24 and `select` still short-circuits at 20.
- **Nothing about the unattended paths.** Every row here ran with an approver that approves.
  `agent ask`, the watchers and the heartbeat still hold `QueueApprover`, and a coder
  delegation from any of them still has every `fs_write` and `shell_exec` denied. That is what
  `coder/explore` is for and it has not been measured.
- **Nothing about `fs_*` versus `shell_exec` inside the role.** Workers used both freely; the
  orchestrator's preference is no longer a variable because it has neither.
- **B13's truncation half, again.** The worker piped `| tail -60` and `| tail -120`, so no
  result approached `tool_result_max_chars`. Sensible, and it sidesteps what the row is for —
  now two runs in a row.
- **The `agent tools list` owner column has no test.** There is no CLI test harness in this
  repo at all, and 8a did not add one.

## deferred from 8a

- **Exposing `coder/explore` to the model**, with the `policy.default.yaml` edit it requires —
  8d, with the unattended measurement that would settle it.
- **The `older_than_days=0` truthiness bug** B11 shipped into the clone. The clone is
  throwaway, so nothing is broken; it is recorded as evidence about the role's output quality,
  not as a defect to fix.
- **The final report call as a second inference over a truncated transcript.** It can lose work
  that was done, and after 8a it is on the critical path for every coding row. Pass 10c.
