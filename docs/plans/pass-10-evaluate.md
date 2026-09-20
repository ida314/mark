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

---

## Exit criteria

None. This is the steady state.

## Must not

- Tune more than one variable per session. Two simultaneous changes produce an
  uninterpretable result.
- Change a default without a dated record entry explaining what the trace showed.
