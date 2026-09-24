# Pass 8 — Tool Surface Reduction — outcome

**No session of this pass has run yet.** 8a is not dispatched. This record exists because the
working agreement asks every session to write or update one, and because the prerequisite work
that made 8a dispatchable is substantial enough that the next session should not have to
reconstruct it from the ledger.

Everything below is **pre-8a**: the four fixes the pass turned out to be blocked on, and the
three findings that constrain what 8a is allowed to claim. The pass's own exit criteria —
target surface reached, no regression against baseline, every moved tool reachable through a
durable role — are untouched and unmet.

---

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

## What 8a still needs before it is dispatched

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
  comparison (8d). None started.
- **`delegate(agent="memory")`** is still the one branch of the tool that is not a delegation:
  it packs retrieval in-process and builds no worker. Pass 6 left it for "Pass 8 owns the tool
  surface", and 8c is where it belongs.
- **`-m docker` selects 0 of 987 tests.** `CLAUDE.md` names it as a gate and nothing has ever
  been marked. `tests/test_sandbox_image.py` is static assertions on the Dockerfile and the
  profile script, deliberately not marked: a real docker test needs the image built and has to
  skip cleanly when run *inside* the sandbox, where there is no docker. Flagged, not built.
- **`agent init` / `agent doctor` print the workspace and never the project**, so a
  misconfigured `project` is invisible there. Carried from ruling 1, still true.
