# Pass 4 — Checkpoints, Resume, Fork

**Architecture reference:** §16, §21 (`status="uncertain"`)
**Depends on:** `docs/records/pass-02-outcome.md`, `docs/records/pass-03-outcome.md`
**Load into session:** `CLAUDE.md`, this file, pass-02 and pass-03 outcomes

## Goal

Survive process death without losing accepted work or repeating side effects.

This is the first pass that changes recovery behavior. Put it behind a feature flag.

---

## Session 4a — Checkpoint record and boundaries

**Scope.**

```
checkpoint
  run_id
  seq
  created_at
  trigger              # turn_end | worker_finished | pre_effect | handoff | manual
  orchestrator_id
  messages_ref         # pointer into journal, not a copy
  handoff_object       # null until Pass 5
  open_workers[]       # worker_id, role, task_spec, status
  worker_results[]     # empty until Pass 6
  pending_promotions[] # empty until Pass 7
  effects_cursor
  memory_watermark     # null until Pass 7
```

`messages_ref` is a pointer. The checkpoint must not duplicate conversation content that
already exists in the journal.

Write at all five triggers. `pre_effect` fires only for `unsafe_write`.

Three fields are deliberately inert in this pass. Define them now so Passes 5, 6, and 7
fill a slot rather than migrate a schema.

**Exit.** A normal run produces checkpoints at every boundary. Checkpoint write overhead
measured and recorded.

---

## Session 4b — Resume and reconciliation

**Scope.**

```
resume(run_id)
    load latest checkpoint
    reconcile orphaned effects
    rehydrate full message list from journal
    continue
```

Reconciliation, by class:

```
read              re-execute freely
idempotent_write  re-execute
unsafe_write      never auto-retry
                  surface as status="uncertain" with the recorded args
```

Any ledger entry at `started` with no terminal state becomes `orphaned` on resume.

The lossy handoff-based resume path is Pass 5. This session implements the mechanical
full-rehydration path only.

**Exit.** Kill at each of the five boundary types and resume correctly.

---

## Session 4c — Uncertain status

**Scope.** Add `status="uncertain"` as a first-class orchestrator observation, distinct
from `blocked`.

```
blocked      the work did not happen
uncertain    it is not known whether the work happened
```

The orchestrator must have an explicit path for `uncertain`: verify by reading back
(check sent mail, re-query the calendar), ask the user, or proceed without it. It must not
silently retry and must not silently drop.

**Exit.** An orphaned `unsafe_write` produces a user-visible, honest statement of the
ambiguity.

---

## Session 4d — Fork

**Scope.**

```
fork(run_id, seq) →
    new run with parent_run_id, forked_from_seq
    state rehydrated from the checkpoint at seq
    original run untouched; its journal is not rewritten
```

On fork, list every `committed` effect after `seq` and present it:

```
Since that point I:
  - modified 3 files in src/auth/
  - sent 1 email
  - created 1 calendar event

Reverting the conversation. I have not undone any of the above.
```

**Exit.** Fork produces a working new run with the original intact and the effect
disclosure accurate.

---

## Exit criteria (pass)

Kill the process at each boundary type and resume successfully. An orphaned
`unsafe_write` surfaces as `uncertain` and is never silently retried. Fork works and
discloses honestly.

## Must not

- Mid-worker checkpoints.
- Automatic side-effect reversal, including file rollback. The disclosure is the feature.
- Cross-run result caching.
- Populate `worker_results[]`, `handoff_object`, or `memory_watermark`. Later passes own
  those and will need the slots empty.

## Outcome record must capture

- Checkpoint record as implemented, including which fields are still inert
- Checkpoint write overhead: latency and storage per boundary type
- Reconciliation rules as implemented
- How `uncertain` is surfaced to the user
- Fork semantics and the disclosure format
