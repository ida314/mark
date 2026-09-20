# Pass 5 — Context Handoff

**Architecture reference:** §1 (Context Limit and Handoff), Phase 2
**Depends on:** `docs/records/pass-02-outcome.md`, `docs/records/pass-04-outcome.md`
**Load into session:** `CLAUDE.md`, this file, pass-02 and pass-04 outcomes

## Goal

Survive the context limit, and reuse the same machinery for cold resume. One generator,
three triggers.

```
context exhaustion     ~8k tokens remaining
cold resume            crash, restart, or long idle gap
stale thread reentry   user returns after an extended period
```

---

## Session 5a — Threshold monitoring

**Scope.** Runtime-side remaining-token accounting for the orchestrator context.
Configurable threshold, default near 8,000 remaining.

The runtime owns this. The orchestrator is not asked to monitor its own context.

**Exit.** Threshold crossing fires reliably in a forced-long run, with enough room left to
produce a handoff without operating at the edge of the window.

---

## Session 5b — Handoff generation

**Scope.** On threshold crossing, inject the handoff instruction and collect:

```
task
user_intent
current_state
decisions_made
constraints
completed_actions
active_subagents
relevant_evidence
unresolved_questions
next_actions
important_memory_refs
```

Structured state, not a transcript dump. Validate the shape; a handoff missing
`next_actions` or `unresolved_questions` is a failed handoff, not a partial one.

Start the fresh orchestrator with system instructions, relevant persistent memory, the
handoff, necessary recent conversation context, and currently relevant tools. The old
context is not copied.

Store the handoff in `handoff_object` on the next checkpoint, filling the slot Pass 4 left
inert.

**Exit.** A forced handoff preserves task continuity across the boundary on at least three
tasks from the Pass 1 suite, including the two near-context-limit tasks.

---

## Session 5c — Cold resume integration

**Scope.** Extend the Pass 4 resume policy:

```
resume(run_id)
    load latest checkpoint
    reconcile orphaned effects
    if fresh and messages fit within budget:
        rehydrate full message list          # mechanical, lossless
    else:
        rehydrate from handoff_object + recent turns + memory refs
```

If a cold resume needs a handoff object and none exists, generate one from the journal
with a cheap model call before starting the new orchestrator.

`WARM_WINDOW` and the message budget are configurable. Start with a few hours and tune
from traces.

**Exit.** A cold resume from a stale run with no stored handoff produces a usable handoff
object and the new orchestrator picks up correctly.

---

## Exit criteria (pass)

Forced handoff at the threshold preserves continuity. Cold resume works both with a stored
handoff object and without one.

## Must not

- Let the orchestrator decide when to hand off.
- Copy the old context into the new orchestrator as a fallback when handoff generation
  looks weak. Fix the generator instead.
- Use the lossy path when the lossless one fits.

## Outcome record must capture

- Threshold and how remaining tokens are accounted
- Handoff schema and validation rules
- `WARM_WINDOW` and message budget defaults
- The journal-to-handoff generator: model used, prompt, cost per invocation
- Observed handoff quality on the test tasks
