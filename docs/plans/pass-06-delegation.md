# Pass 6 — Delegation Interface & Standardized Results

**Architecture reference:** §4, §14, Phases 5 and 6
**Depends on:** `docs/records/pass-04-outcome.md`
**Load into session:** `CLAUDE.md`, this file, pass-04 outcome

## Goal

A stable delegation contract, and worker results that survive a crash instead of being
re-earned.

Until this pass lands, resume re-delegates completed workers. That is correct but
expensive, and the cost shows up in Pass 4 testing.

---

## Session 6a — Delegation signature

**Scope.**

```
delegate(
  durable_role,
  task,
  relevant_context,
  constraints,
  expected_output
)
```

The task specification must contain enough for the worker to operate independently, and
must be stable enough to serve as a cache key. Two logically identical delegations should
produce identical specs; incidental variation in phrasing defeats the cache in Session 6c.

Subagents complete an entire coherent workflow rather than returning control after every
tool call.

**Exit.** Existing ad-hoc delegation paths converted to the single interface.

---

## Session 6b — Result schema

**Scope.**

```
status          completed | blocked | uncertain
answer
evidence[]
actions_taken[]
followups[]
```

Validate on return. A worker that returns prose instead of the schema is a failure, not a
degraded success.

Raw transcripts are retained by the runtime for debugging, observability, evaluation, and
auditing, and never injected into orchestrator context outside an explicit debug mode.

**Exit.** Every durable role returns valid results across the Pass 1 task suite.

---

## Session 6c — Result cache

**Scope.**

```
result_key = hash(durable_role, task_spec, relevant_context_refs)
```

Persist completed results keyed by `result_key`. Populate `worker_results[]` in the
checkpoint, filling the slot Pass 4 left inert.

Two uses: resume reuses completed work rather than re-running it, and a redundant
in-run delegation is a cache hit rather than a second bill.

Run-scoped only.

**Exit.** Kill a run after three completed workers, resume, and none of the three re-run.

---

## Exit criteria (pass)

Single delegation interface. Validated result schema. Resume reuses completed worker
results.

## Must not

- Cross-run result reuse. Staleness semantics for repository and web state are not worked
  out, and this is not the pass to work them out.
- Inject worker transcripts into orchestrator context.
- Remove any tool from the orchestrator surface. That is Pass 8, and it depends on this
  pass being solid first.

## Outcome record must capture

- Delegation signature as implemented
- Result schema and validation behavior on malformed returns
- `result_key` derivation, including what counts as `relevant_context_refs`
- Measured reuse rate on a resume test
