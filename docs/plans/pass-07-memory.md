# Pass 7 — Memory Scopes & Transactional Promotion

**Architecture reference:** §11, §12, §13, Phase 3
**Depends on:** `docs/records/pass-03-outcome.md`, `docs/records/pass-04-outcome.md`
**Load into session:** `CLAUDE.md`, this file, pass-03 and pass-04 outcomes

## Goal

Separate what the agent knows from where the run is, and stop promotion from corrupting
memory on a crash.

Duplicate semantic memory is the failure to design against here. It does not throw; it
silently degrades retrieval quality for months.

---

## Session 7a — Harness inspection

**Scope.** Before writing anything, determine what the current harness actually does.
Does it store episodic and semantic information separately, or as different record types
in one retrieval system? Is there any notion of task-local scratch state today?

Write the finding into the outcome record first. The rest of this pass depends on it, and
guessing here produces a migration that fights the existing store.

**Exit.** A written description of current memory behavior, with file references.

---

## Session 7b — Logical separation and isolation

**Scope.** Three buckets, logically distinct even if the storage backend is shared,
because they have different retrieval and update semantics.

```
working    task-local, agent-local, short-lived, disappears with its task scope
episodic   what happened
semantic   what is known
```

Worker working memory is isolated by default. Worker A's scratch state is not visible to
worker B. Crossing that boundary requires an explicit structured message, a result, or
runtime-managed shared task state.

Working memory is the one memory type that sits inside execution state: it is captured by
checkpoints and discarded when the run completes.

**Exit.** Two concurrent workers cannot read each other's working memory. A completed run
leaves no working memory behind.

---

## Session 7c — Transactional promotion

**Scope.** Promotion is the only place disposable execution state writes into durable
user-scoped memory, so it must be transactional against the checkpoint.

```
working memory
    ↓
classification
    ├── discard
    ├── episodic
    └── semantic
```

Requirements:

- Memory writes are effects with idempotency keys (Pass 3 machinery, reused).
- Promotions batch at task or run boundaries, never opportunistically mid-task.
- `memory_watermark` in the checkpoint records committed episodic and semantic write
  positions. Fills the slot Pass 4 left inert.
- Uncommitted promotions ride in `pending_promotions[]`.

**Exit.** Kill the process between classification and write. Resume produces neither a
lost promotion nor a duplicate semantic fact. Test both orderings.

---

## Exit criteria (pass)

Three logical memory types. Isolated worker working memory. Crash-safe promotion verified
by fault injection, not by inspection.

## Must not

- Promote automatically from working memory without classification.
- Promote mid-task.
- Migrate the physical store if a logical distinction achieves the same thing. Shared
  backend is fine.

## Outcome record must capture

- What the harness did before this pass
- The three buckets as implemented, and whether storage is shared or separate
- Isolation mechanism
- Promotion batching points and idempotency key derivation for memory writes
- Results of the crash-between-classify-and-write test
