# Pass 10 — Evaluate & Tune

**Architecture reference:** Phases 10 and 11
**Depends on:** every prior outcome record
**Load into session:** `CLAUDE.md`, this file, and the specific records relevant to the
question being asked. Not all of them at once.

## Goal

Close the loop. This is recurring, not a one-time pass. Sessions 10a and 10b run once;
10c repeats indefinitely.

---

## Session 10a — Replay harness

**Scope.** Replay a recorded run from the journal with cached tool results and no live
model calls. Because every model and tool call was already recorded, replay exercises the
full orchestration path at zero LLM and tool cost.

This turns every recorded run into a regression test, and it is the cheapest debugging
tool the system will have.

**Exit.** A recorded run replays deterministically and any divergence from the recorded
sequence is reported rather than swallowed.

---

## Session 10b — Full metric run

**Scope.** Pass 1 suite against the Pass 1 baseline.

```
wrong-tool rate
tool-router precision
tool-retrieval recall
main-agent token usage
worker token usage
main-agent calls per complex task
completion rate
delegation latency
handoff success rate
memory retrieval quality

resume success rate
orphaned-effect rate
duplicate-side-effect incidents
worker-result reuse rate
checkpoint overhead (latency and storage)
```

**Exit.** Full comparison table in `docs/records/eval-<date>.md`.

---

## Session 10c — Tuning cycle (recurring)

Work one question per session. Each produces a small change and a dated record entry.

```
which tools deserve permanent orchestrator visibility
which workflows deserve durable specialist roles
which durable roles should create ephemeral workers
which APIs should be combined into higher-level tools
is delegation too aggressive or too conservative
is retrieval top-K too large or too small
should the routed tool set be larger or smaller
should the handoff threshold change
is memory promotion too aggressive
are checkpoint boundaries too frequent or too coarse
does any long-running role justify mid-worker checkpointing
does journal retention need a pruning policy yet
should coder gain content-addressed file pre-images for real undo
```

The last three are the deferred items from the durability spine. Revisit them only when a
trace shows they are costing something.

### Logged from the Pass 8 boundary, with the evidence already in hand

*Added 2026-09-24, on Dylan's instruction: "not blocking, log it for after Pass 8."*

- **The heartbeat spends its step budget retrying a call that is denied every time.** After
  the 2026-09-23 daemon restart the 400 is gone, but a heartbeat turn will still retry a
  denied `open_loop_close` up to its budget. The daemon runs at `autonomy="observe"`
  (`daemon/heartbeat.py:118,130`) with `max_steps = 4`, so a denial the model cannot appeal
  can consume the whole turn. Nothing in the loop tells a model that a `deny` is final rather
  than an argument problem worth rephrasing — which is the same shape as the
  `_rejected` counter `tools/executor.py` already keeps for invalid arguments, applied to
  policy denials instead.
- **A working heartbeat can never report `completed`.** `loop.py:829` sets `abandoned`
  whenever `steps >= max_steps`, and the heartbeat's budget is 4; it used all four on both of
  its successful runs. Its answer is therefore by construction "a summary of unfinished work,
  not an answer" (`loop.py:825`), and that is the text `repo_agenda.notify` files as a
  Suggestion. Tuning item, not a bug.
- **A worker's final report is a second inference over a truncated transcript, and it is wrong
  in both directions.** Named in the 8a record and never filed here until now. Four measured
  instances across three sessions: a researcher claimed "no web access from that context" having
  made zero calls in a run where the same role made ten successful web calls minutes earlier
  (8b); a coder reported "all 19 tests pass" when 2 of 19 failed, disproved by re-running the
  same fixture on the same commit (8d B12); a coder emitted a citation naming three files that
  do not exist after eleven correct `shell_exec` calls had found the real ones (8d B10); and in
  the one case the orchestrator caught it, the worker's own `status` was `uncertain` while its
  report claimed success (8d B11). After 8a this is on the critical path for every coding row,
  because the report is all the orchestrator ever sees. **On the 8d evidence this outranks the
  rest of this list.**

- **`daemon + delegate(researcher)` is `allow`, so a heartbeat can reach the open web
  unattended.** Carried out of 8c, filed for 8d, not fixed there. `delegate` is `risk="read"`,
  so `daemon-never-external` (which matches `risk: [external, destructive]`) cannot see it; and
  a worker's `ctx.origin` is `subagent:researcher`, so nothing keyed on `origin: [daemon]`
  applies one layer down. `web_fetch` is also `risk="read"` — its `effect_class` is
  `unsafe_write`, a different axis the engine does not consult — so it is `allow` at `observe`.
  8c closed the same hole for `mail` by name with `mail-delegation-never-unattended`; the
  general fix is one rule at the `delegate` layer naming every role that holds an outward tool,
  or reclassifying `web_fetch`'s risk. `coder` is covered only incidentally, by the risk matrix
  denying `shell_exec`/`fs_write` at `observe`. **This is a policy hole, not a tuning question —
  it should not wait for a recurring session.**

- **An ephemeral `coder/explore` worker, and whether to expose it to the model.** 8a's criterion
  is in the `subagents.py` comment — "a `coder` run that spends its budget re-reading the repo
  before it can run a failing test" — and six of fourteen 8a workers did exactly that. Deferred
  to 8d "with the unattended measurement that would settle it"; 8d ran attended only, so the
  measurement still does not exist.

- **Eval runs still end up as durable beliefs, and the recorded fix does not work.** Promotion
  patched off in the harness covers one writer; the daemon's consolidation loop is the other, and
  it **re-derives** candidates from a run's `episodes` and `actions`, so clearing
  `candidate_memories` before restarting it removes the backlog and not the source — measured
  8d, clone path back in `facts` 90 seconds after the restart. Cheapest real fix is a
  session-level `synthetic` flag the consolidator's episode scan skips; next is pointing the
  suite at a throwaway database via `AGENT_DB__*`. **Blocking for Pass 9**, which is a retrieval
  pass and will be measured against a store these rows pollute.

- **Daemon health is invisible where it looks like it should be.** A provider 400 does not
  raise out of `run_turn` — it is journaled as `agent_finished status=failed` — so
  `repo_agenda.notify(title="Heartbeat failed")` has never once fired. 64 notifications
  exist and exactly one is a `heartbeat` row. The `except Exception` around the heartbeat
  catches everything except the failure mode that actually happens, so anyone checking the
  agenda for daemon health concludes it is fine. This one is a trace question with its answer
  already known.

---

## Exit criteria

None. This is the steady state.

## Must not

- Tune more than one variable per session. Two simultaneous changes produce an
  uninterpretable result.
- Change a default without a dated record entry explaining what the trace showed.
