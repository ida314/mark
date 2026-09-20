# Pass 9 — Tool Discovery & Router

**Architecture reference:** §5, §6, §7, §18, §19, Phases 7 and 8
**Depends on:** `docs/records/pass-01-outcome.md`, `docs/records/pass-03-outcome.md`, `docs/records/pass-08-outcome.md`
**Load into session:** `CLAUDE.md`, this file, those three outcomes

## Goal

Absorb catalog growth without growing any prompt.

Two filtering stages with different jobs. Retrieval provides recall; the router provides
precision. Evaluate them separately or you will tune the wrong one.

```
capability description
      ↓
embedding retrieval
      ↓
top-K candidates
      ↓
tool-call router
      ↓
3–6 tools
      ↓
worker
```

---

## Session 9a — Registry and retrieval

**Scope.** Every tool carries:

```
name
English description
schema
embedding
effect_class
metadata
```

Embed the description. Implement top-K vector search against a natural-language capability
description produced by the requesting agent, for example: "I need tools that can search a
source-code repository, read matching files, edit files, and execute tests."

While the catalog is small this stage may be near pass-through. Build it anyway. The
architecture must not assume every tool permanently fits in context.

**Exit.** Retrieval returns ranked candidates for a capability description.

---

## Session 9b — Recall evaluation

**Scope.** Build a labeled set: for each task in the Pass 1 suite, the tools a correct
execution actually needs. Measure whether top-K contains them.

Recall is the only thing measured here. A top-K full of junk still scores well, and that
is correct — precision is the router's job.

**Exit.** Recall number recorded, with K.

---

## Session 9c — Router

**Scope.** A reasoning-based router receiving:

```
task
agent role
candidate tools
tool descriptions
tool schemas
effect_class
```

returning 3–6 tools, injected into the worker context for the task and removed when it
ends. A worker does not accumulate every tool it has ever used.

Routing decisions are not persisted in checkpoints. They are cheap to recompute on resume,
and recomputing avoids restoring a stale tool set.

**Exit.** Router returns a bounded set for every task in the suite.

---

## Session 9d — Precision evaluation

**Scope.** Against the same labeled set: did the routed set contain what was needed, and
how much did it contain that was not? Report alongside wrong-tool rate versus the Pass 1
baseline.

**Exit.** Both metrics recorded. Wrong-tool rate at or below baseline with a materially
smaller injected tool set.

---

## Exit criteria (pass)

Two stages, independently measurable. Retrieval recall and router precision both recorded.

## Must not

- Collapse the two stages into one "tool selection" metric.
- Persist routing decisions into checkpoints.
- Let the routed set grow past 6 to make a hard task pass. Record the failure instead.

## Outcome record must capture

- Registry schema and embedding model
- K, and how it was chosen
- The labeled set, and where it lives
- Recall and precision numbers
- Tasks where routing failed, with the capability description that produced the failure
