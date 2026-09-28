# Session prompts

38 implementation sessions plus a bootstrap. Each block is paste-ready.

Every session follows the same shape:

```
/clear
→ opening prompt          (enter plan mode first: shift+tab)
→ review the plan, accept
→ execute
→ closing prompt          (identical every time)
→ commit
```

---

## The two constants

**Closing prompt.** Use verbatim at the end of every session.

```
Write or update docs/records/pass-NN-outcome.md for the session we just finished.

Sections: what shipped; what deviated from the plan and why; what is now true about the
code that was not before; schemas exactly as implemented; deferred items and where they
went; open questions for later passes.

Record what exists, not what was intended. If something in the plan turned out wrong, say
so in the deviation section rather than quietly matching the plan.
```

**Mid-session reset.** When the session has picked up a wrong assumption it keeps
returning to:

```
/clear
```

then re-run the opening prompt with `Resume mid-session. Read
docs/records/pass-NN-outcome.md for what is already done.` appended.

---

## Bootstrap

```
Read CLAUDE.md and docs/plans/README.md.

Fill in the four command placeholders in CLAUDE.md from this repo's actual build, test,
typecheck, and lint commands. Then give me a one-paragraph description of how the current
agent loop is structured: where the orchestrator lives, where tools are registered, and
where the frontend gets its state from.

Do not change any code.
```

---

## Pass 1 — Baseline & Instrumentation

**1a**
```
Read docs/plans/pass-01-baseline.md.

Implement session 1a only: telemetry emission. Flat JSONL is fine; Pass 2 replaces the
storage. Observe the Must not section.
```

**1b**
```
Read docs/plans/pass-01-baseline.md and docs/records/pass-01-outcome.md.

Session 1b only: write evals/baseline-tasks.md. Propose the task set first and let me edit
it before you commit it. This suite gets frozen and re-run in passes 8, 9, and 10, so the
coverage matters more than the count.
```

**1c**
```
Read docs/plans/pass-01-baseline.md and docs/records/pass-01-outcome.md.

Session 1c only: run the frozen suite and write docs/records/baseline.md as a table.

Then help me with the failure survey. Ask me what I remember about lost runs and duplicate
side effects over the last month and write it up. Do not guess at numbers I have not given
you.
```

---

## Pass 2 — Durable Run Journal

**2a**
```
Read docs/plans/pass-02-journal.md and docs/records/pass-01-outcome.md.

Session 2a only: the SQLite journal store and writer. Monotonic seq enforcement, sync
append for effect and checkpoint events, retention hook. Observe the Must not section.
```

**2b**
```
Read docs/plans/pass-02-journal.md and docs/records/pass-02-outcome.md.

Session 2b only: wire the full event vocabulary through the runtime. Define payload shapes
for all of them including the six that later passes write, so those passes fill a slot
rather than migrate a schema.
```

**2c**
```
Read docs/plans/pass-02-journal.md and docs/records/pass-02-outcome.md.

Session 2c only: frontend subscribes to the journal-backed stream with last-seen-id
replay. Remove the existing in-memory event bus rather than leaving it alongside.

Then run the kill test: kill the process mid-run at three different points and confirm the
journal is complete up to each kill.
```

---

## Pass 3 — Effect Class & Effect Ledger

**3a**
```
Read docs/plans/pass-03-effects.md and docs/records/pass-02-outcome.md.

Session 3a only: add effect_class to the tool registry. Mandatory, no default,
registration fails loudly without it. Do not classify any tools yet.
```

**3b**
```
Read docs/plans/pass-03-effects.md and docs/records/pass-03-outcome.md.

Session 3b only: the effect ledger and idempotency key derivation.

Spend the time on argument canonicalization. List every field excluded from the hash and
write a test proving two attempts at the same logical call produce the same key. This is
the part that fails silently.
```

**3c**
```
Read docs/plans/pass-03-effects.md and docs/records/pass-03-outcome.md.

Session 3c only: classify every read-only and local-state tool. Maintain
docs/records/effect-classification.md as a table with a one-line justification per tool.

Flag anything you expected to be read and is not. Ask me about the ambiguous ones rather
than deciding.
```

**3d**
```
Read docs/plans/pass-03-effects.md and docs/records/pass-03-outcome.md.

Session 3d only: classify everything touching filesystem, shell, email, calendar, or an
external API.

Default to unsafe_write under uncertainty. For each integration, check whether the API
accepts a client-supplied id before calling anything idempotent_write, and record what you
checked.
```

---

## Pass 4 — Checkpoints, Resume, Fork

**4a**
```
Read docs/plans/pass-04-checkpoints.md, docs/records/pass-02-outcome.md, and
docs/records/pass-03-outcome.md.

Session 4a only: checkpoint record and the five write boundaries. Put it behind a feature
flag.

Leave handoff_object, worker_results, pending_promotions, and memory_watermark defined and
inert. Passes 5, 6, and 7 fill them.

Measure and record checkpoint write overhead per boundary type.
```

**4b**
```
Read docs/plans/pass-04-checkpoints.md and docs/records/pass-04-outcome.md.

Session 4b only: resume and reconciliation. Mechanical full-rehydration path only; the
handoff-based path is Pass 5.

Reconciliation by effect class, with unsafe_write never auto-retried.

Then kill and resume at each of the five boundary types.
```

**4c**
```
Read docs/plans/pass-04-checkpoints.md and docs/records/pass-04-outcome.md.

Session 4c only: status="uncertain" as a first-class observation distinct from blocked.

The orchestrator needs an explicit path for it: verify by reading back, ask me, or proceed
without. Show me the user-facing wording for an orphaned email send before you finalize it.
```

**4d**
```
Read docs/plans/pass-04-checkpoints.md and docs/records/pass-04-outcome.md.

Session 4d only: fork with parent_run_id and forked_from_seq, plus the committed-effect
disclosure.

No automatic reversal of anything, including files. The disclosure is the feature.
```

---

## Pass 5 — Context Handoff

**5a**
```
Read docs/plans/pass-05-handoff.md, docs/records/pass-02-outcome.md, and
docs/records/pass-04-outcome.md.

Session 5a only: runtime-side remaining-token accounting and a configurable threshold near
8000. The runtime owns this; the orchestrator is never asked to monitor its own context.
```

**5b**
```
Read docs/plans/pass-05-handoff.md and docs/records/pass-05-outcome.md.

Session 5b only: handoff generation and fresh-orchestrator initialization. Validate the
handoff shape; a handoff missing next_actions or unresolved_questions is a failure, not a
partial.

Store it in handoff_object on the next checkpoint.

Then force a handoff on the two near-context-limit tasks from the baseline suite and show
me the resulting handoff objects.
```

**5c**
```
Read docs/plans/pass-05-handoff.md and docs/records/pass-05-outcome.md.

Session 5c only: extend the Pass 4 resume policy with the warm/cold branch, and implement
journal-to-handoff generation for a cold resume with no stored handoff.

Use the cheapest model that produces a usable handoff. Record the per-invocation cost.
```

**5d**
```
Read docs/plans/pass-05-handoff.md and docs/records/pass-05-outcome.md.

Session 5d only: the lookup. A read-class tool taking one manifest ref and returning a
bounded excerpt. No free-text query, no "everything", never invoked by the runtime at
orchestrator start, journaled like any other tool call, reading the journal/archive rather
than a new store.

Record lookups per successor turn. If successors routinely fetch every manifest item as
their first action, that is the wholesale restore by another route - flag it for Pass 10,
do not tune it.

Then run the confabulation eval and compare against the rates recorded before the manifest
and after the manifest alone. Then re-run all three handoff tasks and judge the pass exit.
```

---

## Pass 6 — Delegation Interface & Standardized Results

**6a**
```
Read docs/plans/pass-06-delegation.md and docs/records/pass-04-outcome.md.

Session 6a only: the delegate signature, and convert existing ad-hoc delegation paths to
it.

The task spec becomes a cache key in 6c, so it has to be stable. Two logically identical
delegations must produce identical specs.
```

**6b**
```
Read docs/plans/pass-06-delegation.md and docs/records/pass-06-outcome.md.

Session 6b only: the result schema with validation on return. A worker returning prose
instead of the schema is a failure, not a degraded success.

Transcripts stay in the runtime and out of orchestrator context except in explicit debug
mode.
```

**6c**
```
Read docs/plans/pass-06-delegation.md and docs/records/pass-06-outcome.md.

Session 6c only: result_key derivation and the run-scoped result cache, populating
worker_results in the checkpoint.

Then the reuse test: kill a run after three completed workers, resume, confirm none of the
three re-run. Report the measured reuse rate.
```

---

## Pass 7 — Memory Scopes & Transactional Promotion

**7a**
```
Read docs/plans/pass-07-memory.md, docs/records/pass-03-outcome.md, and
docs/records/pass-04-outcome.md.

Session 7a only: inspect what the current harness actually does with memory. Are episodic
and semantic stored separately or as record types in one system? Is there any task-local
scratch state today?

Write the finding into the outcome record. Change no code this session.
```

**7b**
```
Read docs/plans/pass-07-memory.md and docs/records/pass-07-outcome.md.

Session 7b only: the three logical buckets and worker working-memory isolation. Keep the
storage backend shared if a logical distinction achieves the same thing.

Then prove it: two concurrent workers cannot read each other's working memory, and a
completed run leaves none behind.
```

**7c**
```
Read docs/plans/pass-07-memory.md and docs/records/pass-07-outcome.md.

Session 7c only: promotion as journaled effects with idempotency keys, batched at task or
run boundaries, with memory_watermark and pending_promotions in the checkpoint.

Then fault-inject: kill between classification and write, test both orderings, and confirm
neither a lost promotion nor a duplicate semantic fact.
```

---

## Pass 8 — Tool Surface Reduction

**8a**
```
Read docs/plans/pass-08-tool-surface.md, docs/records/pass-01-outcome.md, and
docs/records/pass-06-outcome.md.

Session 8a only: establish the coder durable role and move fs_* and shell_exec off the
orchestrator surface. Define the ephemeral workers within the role.

Then re-run only the coding tasks from the baseline suite and compare completion rate.
Move nothing else this session.
```

**8b**
```
Read docs/plans/pass-08-tool-surface.md and docs/records/pass-08-outcome.md.

Session 8b only: establish the researcher role and move web_search and web_fetch. The
researcher returns a compressed structured result, not its search transcript.

Then re-run only the research tasks and compare.
```

**8c**
```
Read docs/plans/pass-08-tool-surface.md and docs/records/pass-08-outcome.md.

Session 8c only: move memory_history, memory_search, and the gmail tools. Establish the
memory durable role over the Pass 7 operations.

Then re-run only the memory and email tasks and compare.
```

**8d**
```
Read docs/plans/pass-08-tool-surface.md, docs/records/pass-08-outcome.md, and
docs/records/baseline.md.

Session 8d only: full baseline suite, full comparison table, and a decision on the four
borderline permanent tools.

If a family regressed, diagnose the role's task specification before proposing that any
tool return to the orchestrator.
```

---

## Pass 9 — Result Verification & Replanning

**9a**
```
Read docs/plans/pass-09-verification.md, docs/records/pass-06-outcome.md, and
docs/records/pass-08-outcome.md.

Session 9a only: the worker ledger read back out of the run journal, the six deterministic
checks over it, the flags and validation status on the result, and the new journal event.

No model call in the verification path. Every check names the journal event it read.
```

**9b**
```
Read docs/plans/pass-09-verification.md and docs/records/pass-09-outcome.md.

Session 9b only: what an invalidated result costs. Not cacheable, no candidate memories,
and runtime-written text naming the flags and the three moves.

The orchestrator decides. The runtime does not re-delegate on its own.
```

**9c**
```
Read docs/plans/pass-09-verification.md and docs/records/pass-09-outcome.md.

Session 9c only: the escape step, ended_by_choice replacing the steps >= max_steps test for
abandoned, and denial finality keyed on (tool, rule, args).

A differently-argued call to the same tool is never withdrawn.
```

**9d**
```
Read docs/plans/pass-09-verification.md and docs/records/pass-09-outcome.md.

Session 9d only: the measured run. The five shapes from 8d against a clone at a recorded
sha, by the 8d method — daemon stopped, promotion off, per-row reset, both sides measured
the same way.

The number this session exists for is the false-positive rate per flag. A hard flag with no
measurement behind it does not stay hard.
```

---

## Pass 10 — Tool Discovery & Router

**10a**
```
Read docs/plans/pass-10-discovery.md, docs/records/pass-03-outcome.md, and
docs/records/pass-08-outcome.md.

Session 10a only: full registry entries with embeddings, and top-K retrieval against a
natural-language capability description.

Near pass-through is fine at this catalog size. Build the real interface anyway.
```

**10b**
```
Read docs/plans/pass-10-discovery.md and docs/records/pass-10-outcome.md.

Session 10b only: build the labeled set — for each baseline task, the tools a correct
execution actually needs — and measure retrieval recall at K.

Recall only. A top-K full of junk that contains the right tools scores well, and that is
correct.
```

**10c**
```
Read docs/plans/pass-10-discovery.md and docs/records/pass-10-outcome.md.

Session 10c only: the tool-call router. 3 to 6 tools out, injected for the task and removed
when it ends.

Routing decisions are not persisted in checkpoints.
```

**10d**
```
Read docs/plans/pass-10-discovery.md, docs/records/pass-10-outcome.md, and
docs/records/baseline.md.

Session 10d only: router precision against the labeled set, plus wrong-tool rate versus
baseline.

If a task only passes with more than 6 routed tools, record the failure. Do not raise the
cap to make it pass.
```

---

## Pass 11 — Evaluate & Tune

**11a**
```
Read docs/plans/pass-11-evaluate.md and docs/records/pass-02-outcome.md.

Session 11a only: the replay harness. Replay a recorded run from the journal with cached
tool results and no live model calls, reporting any divergence from the recorded sequence
rather than swallowing it.
```

**11b**
```
Read docs/plans/pass-11-evaluate.md and docs/records/baseline.md.

Session 11b only: full baseline suite against the full metric set including the durability
metrics. Write docs/records/eval-<today>.md.

Report the numbers. Propose no changes this session.
```

**11c** (recurring — substitute one question per session)
```
Read docs/plans/pass-11-evaluate.md and the most recent docs/records/eval-*.md.

One tuning question this session: <QUESTION>.

Propose a single change, make it, re-run only the affected part of the suite, and append a
dated entry to the eval record with what the trace showed.

One variable. Two simultaneous changes produce an uninterpretable result.
</QUESTION>
```

---

## Notes

- Enter plan mode before every implementation prompt and review the plan against the
  pass file's `Must not` section before accepting.
- Commit between sessions so `/clear` is always safe.
- Do not add standing instructions like "double-check your work" to CLAUDE.md. On the
  current model generation that causes re-verification of work that was already correct.
- Sessions 1b, 3c, 3d, 4c, and 7a deliberately hand judgment back to you. Answer them
  rather than letting the session decide.
