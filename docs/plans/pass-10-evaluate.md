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
