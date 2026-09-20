# Pass 1 — Baseline & Instrumentation

**Architecture reference:** Phase 1, §20
**Depends on:** nothing
**Load into session:** `CLAUDE.md`, this file

## Goal

Produce numbers for the current system before anything changes. Passes 8 and 9 are
justified by comparison against these numbers; without them, a regression in completion
rate is indistinguishable from noise.

This pass is read-only with respect to behavior.

---

## Session 1a — Telemetry emission

**Scope.** Add measurement to the existing agent loop. Emit and persist, per task:

```
request_id
tool calls made
tools visible in context but unused
tool-selection failures      # wrong tool chosen, or retry with a different tool
context tokens at start / peak / end
wall-clock latency
main-agent token usage (input / output)
final status                 # completed | failed | abandoned
```

Storage can be a flat JSONL file. Do not build a schema you intend to keep; Pass 2
replaces this with the journal.

**Exit.** Running any task produces a complete record.

---

## Session 1b — Task suite

**Scope.** Write `evals/baseline-tasks.md`: a fixed set of 15–25 representative tasks
spanning the work this agent actually does. Include:

- simple direct-answer requests
- single-tool lookups
- multi-file coding tasks
- open-ended research
- tasks that touch email or calendar
- at least two tasks known to be near the context limit

The suite is frozen after this session. Passes 8, 9, and 10 re-run this exact set.

**Exit.** Suite committed, with a one-line description of what each task exercises.

---

## Session 1c — Measurement run and failure survey

**Scope.** Run the suite. Record results in `docs/records/baseline.md` as a table.

Separately, survey the last 30 days of real usage (logs, shell history, memory of it) and
record:

- how often a run was lost to process failure, timeout, or restart
- what recovery cost when that happened
- any known duplicate side effects (double email, double calendar entry)

That second part motivates the entire durability spine. Write it down even if the honest
answer is "twice, and I re-ran it by hand."

**Exit.** `docs/records/baseline.md` exists and is reproducible on demand.

---

## Exit criteria (pass)

A baseline table for the frozen task suite, plus a written failure survey.

## Must not

- Change the tool surface, agent structure, prompts, or delegation behavior.
- Build durable storage. That is Pass 2.
- "Fix" anything the measurement reveals. Record it and move on.

## Outcome record must capture

- Where telemetry is emitted from and in what format
- Path to the frozen task suite
- The baseline table itself
- Anything the measurement revealed that changes the priority of later passes
