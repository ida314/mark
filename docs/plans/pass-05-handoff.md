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

**Also 5c, added by Dylan at the 5b boundary: the manifest.** 5b found that a successor
answers confidently from material the handoff dropped. The manifest is half the fix; 5d is
the other half, and the two are one mechanism.

- A handoff field listing **each dropped item** with a kind, a one-line description, and a
  **ref resolvable against the journal/archive**. Not just kinds - refs, so a lookup can be
  targeted.
- The successor's context states that these exist, and that it must look one up or say it
  does not have it, **never answer from memory of it**.

**Also 5c: requirement B**, filed by Dylan at the Pass 4/5 boundary and inherited from 5b,
which consumes neither `notice()` nor `closing_messages()` and said so. 5c does. A runtime
guard that refuses an `unsafe_write` matching an unresolved uncertain call's tool and
`canonical_args` in the same run, unless the user has said to run it again. The prompt is
currently the only guard, a re-issued call mints a new idempotency key because `step_id` is
part of it, and the model being asked to comply is a local 27B.

**Exit.** A cold resume from a stale run with no stored handoff produces a usable handoff
object and the new orchestrator picks up correctly. Plus: the confabulation eval re-run
after the manifest **alone**, so the manifest's share of the gain is measured before 5d
adds the lookup.

---

## Session 5d — The lookup

**Added by Dylan at the 5b boundary (2026-09-21), after 5b found that a successor does not
reliably refuse to answer from material the handoff dropped.** 5c ships the manifest; this
session ships the only thing that changes the behaviour rather than nudging it.

**His reading of this pass's second Must not, verbatim, which is what makes 5d permissible:**

> it forbids the runtime restoring the old context wholesale as a fallback. It does not
> forbid the successor requesting a specific named item. The line is who chooses what comes
> back and how much.

**Scope.** A `read`-class tool taking one manifest ref and returning a bounded excerpt.

```
takes        exactly one manifest ref
returns      a bounded excerpt
cannot       take a free-text query
cannot       return "everything"
invoked by   the model only - never by the runtime at orchestrator start
journaled    every call, like any other tool call
reads        the journal/archive; not a new store
```

**The guard against this becoming the fallback in disguise**, which is his and is binding:
record lookups per successor turn. If successors routinely fetch every manifest item as
their first action, that is the wholesale restore by another route. **Flag it for Pass 10
rather than tuning it now.**

**Exit.** The confabulation eval is run before and after. Ask a successor about dropped
material: a lookup or "I don't have that" passes, a confident answer fails. Record the
confabulation rate at each point, including after 5c alone, so how much of the gain is the
manifest and how much is the lookup is a measurement rather than an assumption.

---

## Exit criteria (pass)

Forced handoff at the threshold preserves continuity. Cold resume works both with a stored
handoff object and without one.

**Amended by Dylan at the 5b boundary.** The 5b exit is **not met**, and is not to be
recorded as met at two-thirds: the criterion asks for preserved continuity, and the
confabulation finding says the two tasks that ran did not fully preserve it. **The pass exit
is judged after 5d** — re-run all three tasks, same grading, and compare.

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
