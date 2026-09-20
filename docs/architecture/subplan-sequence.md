# Tool-Call Architecture — Subplan Sequence

Ten passes. Each is intended to be a single Claude Code plan-mode session producing one reviewable, independently shippable change set.

## Dependency order

```text
1  Baseline & Instrumentation
        ↓
2  Durable Run Journal
        ↓
3  Effect Class & Effect Ledger
        ↓
4  Checkpoints, Resume, Fork
        ↓
5  Context Handoff
        ↓
6  Delegation Interface & Standardized Results
        ↓
7  Memory Scopes & Transactional Promotion
        ↓
8  Tool Surface Reduction
        ↓
9  Tool Discovery & Router
        ↓
10 Evaluate & Tune        (recurring, not one-shot)
```

Passes 2–5 are the durability spine and must run in order. Pass 7 can move earlier if memory work is blocking something else, as long as it lands after 4. Pass 8 must not precede 6.

---

## Pass 1 — Baseline & Instrumentation

**Covers:** Phase 1, part of §20

**Goal.** Get numbers before changing anything.

**Scope.**
- Emit and store telemetry for the existing system: tool calls per task, tool-selection failures, context usage, latency, completion rate, main-agent token usage.
- Record how often the agent considers irrelevant tools.
- Record how often runs are currently lost to process failure or timeout, and what recovery costs.

**Exit criteria.** A baseline table exists for a fixed set of representative tasks, reproducible on demand.

**Must not.** Touch the tool surface, agent structure, or prompts. This pass is read-only with respect to behavior.

---

## Pass 2 — Durable Run Journal

**Covers:** Phase 2B (journal portion), Phase 9, §15

**Goal.** One append-only event log that is both the durability substrate and the frontend feed.

**Scope.**
- SQLite journal keyed by `run_id` with monotonic `seq`.
- Full event schema from §15, including `message_appended`, `checkpoint_written`, `effect_intended`, `effect_committed`, `run_resumed`, `run_forked` (the last several are written by later passes; define them now).
- Frontend subscribes to a journal-backed stream, with reconnection replaying from a last-seen event id.
- Retention and pruning policy, even if the initial policy is "keep everything."

**Exit criteria.** Kill the process mid-run and the journal contains the complete event sequence up to the kill. Frontend reconnect after a drop replays without visible loss.

**Must not.** Implement checkpoints. Change agent behavior. Build a second event path parallel to the journal — if a legacy in-memory event bus exists, this pass replaces it rather than sitting beside it.

---

## Pass 3 — Effect Class & Effect Ledger

**Covers:** §19 tool metadata, effect ledger portion of §16

**Goal.** Know, for every tool call, whether re-running it is safe.

**Scope.**
- Add `effect_class` (`read` | `idempotent_write` | `unsafe_write`) to the tool registry schema. Registration fails without one; no default.
- Audit and classify every currently registered tool. This is the bulk of the pass and it is mostly judgment, not code.
- Implement the ledger protocol: write `intended` before dispatch, `started` on dispatch, `committed` with `result_ref` or `failed` on completion.
- Idempotency key derivation: `hash(run_id, step_id, tool_name, canonical_args)`. Argument canonicalization needs care — exclude timestamps, request ids, and other incidental fields, or keys will never match across a retry.

**Exit criteria.** Every effecting call produces a ledger entry. Killing the process mid-call leaves an entry stuck at `started`.

**Must not.** Implement reconciliation or resume. Nothing reads the ledger yet; this pass only writes it.

---

## Pass 4 — Checkpoints, Resume, Fork

**Covers:** remainder of §16, `status="uncertain"` in §21

**Goal.** Survive process death without losing accepted work or repeating side effects.

**Scope.**
- Checkpoint record and the four automatic boundaries (`turn_end`, `worker_finished`, `pre_effect`, `handoff`) plus `manual`.
- Resume: load latest checkpoint, reconcile orphaned effects, rehydrate the message list.
- Reconciliation rules by effect class. `unsafe_write` never auto-retries.
- `status="uncertain"` as a first-class orchestrator observation, distinct from `blocked`.
- Fork with `parent_run_id` / `forked_from_seq`, and disclosure of every effect committed after the fork point.

**Exit criteria.** Kill the process at each boundary type and resume successfully. An orphaned `unsafe_write` surfaces as `uncertain` and is never silently retried. Fork produces a working new run with the original intact.

**Must not.** Mid-worker checkpoints. Automatic side-effect reversal. Cross-run caching.

**Note.** Leave `worker_results[]` in the checkpoint record defined but unpopulated — Pass 6 fills it. Until then, resume re-delegates completed workers. That is correct behavior, just wasteful, and it will show up as real cost during testing.

---

## Pass 5 — Context Handoff

**Covers:** Phase 2, §1

**Goal.** Survive the context limit, and reuse the same machinery for cold resume.

**Scope.**
- Runtime-side remaining-token monitoring with a configurable threshold, starting near 8,000.
- Structured handoff generation and fresh-orchestrator initialization.
- Wire handoff into the Pass 4 resume policy: warm resume rehydrates the full message list, cold or stale resume rehydrates from the handoff object.
- Cold-resume path where no handoff object exists: generate one from the journal with a cheap model call.
- Configurable `WARM_WINDOW` and message budget.

**Exit criteria.** A forced handoff at the threshold preserves task continuity across the boundary. A cold resume from a stale run produces a usable handoff object and the new orchestrator picks up correctly.

---

## Pass 6 — Delegation Interface & Standardized Results

**Covers:** Phases 5 and 6, §14

**Goal.** A stable delegation contract and reusable worker results.

**Scope.**
- Delegation signature: `durable_role`, `task`, `relevant_context`, `constraints`, `expected_output`.
- Standard result schema: `status`, `answer`, `evidence`, `actions_taken`, `followups`.
- Ensure durable roles complete coherent workflows without bouncing control back per tool call.
- Result persistence content-addressed by `result_key`, populating `worker_results[]` in the checkpoint from Pass 4.
- Run-scoped cache only.

**Exit criteria.** Resume reuses completed worker results instead of re-running them. Raw transcripts never enter orchestrator context outside debug mode.

**Must not.** Cross-run result reuse. Staleness semantics for repository and web state are not worked out and this is not the pass to work them out.

---

## Pass 7 — Memory Scopes & Transactional Promotion

**Covers:** Phase 3, §11, §12

**Goal.** Separate what the agent knows from where the run is, and stop promotion from corrupting memory on crash.

**Scope.**
- Explicit working / episodic / semantic separation, logical even if the storage backend is shared.
- Worker working memory isolated by default.
- Promotion batched at task or run boundaries.
- Memory writes as journaled effects with idempotency keys.
- `memory_watermark` and `pending_promotions[]` in the checkpoint.
- Inspect the existing harness first to determine what distinction, if any, already exists.

**Exit criteria.** Crash between classification and write produces neither a lost promotion nor a duplicate semantic fact on resume.

---

## Pass 8 — Tool Surface Reduction

**Covers:** Phase 4, §8, §9, §10

**Goal.** Get the orchestrator to 8–12 permanent tools.

**Scope.**
- Move `fs_*`, `shell_exec`, `web_search`, `web_fetch`, `memory_history`, `memory_search`, `gmail_*` behind durable roles.
- Establish the `coder` and `researcher` roles as the owners of those capabilities.
- Define ephemeral workers within those roles where they help.

**Exit criteria.** Orchestrator permanent surface is in the target range, and completion rate has not regressed against the Pass 1 baseline.

**Must not.** Run before Pass 6. Removing a tool from the orchestrator before delegation is reliable converts a working path into a broken one.

---

## Pass 9 — Tool Discovery & Router

**Covers:** Phases 7 and 8, §5, §6, §18

**Goal.** Absorb catalog growth without growing any prompt.

**Scope.**
- Tool registry entries carry name, description, schema, embedding, `effect_class`, metadata.
- Stage 1: semantic top-K retrieval, permitted to be near pass-through while the catalog is small.
- Stage 2: reasoning-based router returning 3–6 tools, receiving `effect_class` among its inputs.
- Evaluate retrieval recall and router precision as separate metrics.

**Exit criteria.** Both stages measurable independently. Wrong-tool rate at or below the Pass 1 baseline with a materially smaller injected tool set.

---

## Pass 10 — Evaluate & Tune

**Covers:** Phases 10 and 11

**Goal.** Close the loop. This is recurring rather than a one-time pass.

**Scope.**
- Full metric set against the Pass 1 baseline, including the durability metrics: resume success rate, orphaned-effect rate, duplicate-side-effect incidents, worker-result reuse rate, checkpoint overhead.
- Build the replay harness: recorded runs replayed with cached tool results, exercising orchestration without LLM or tool cost.
- Work the Phase 11 tuning list.

**Exit criteria.** None. This is the steady state.

---

## Sequencing notes

**Why the durability spine goes first.** Pass 3 requires auditing and classifying every registered tool. Pass 8 rewrites which tools exist where. Doing 8 before 3 means doing that audit twice, and the second audit is the one where a misclassification ships.

**Why Pass 1 is not optional.** Passes 8 and 9 are justified by numbers. Without a baseline, a regression in completion rate is indistinguishable from noise, and the tuning in Pass 10 has nothing to tune against.

**Parallelism.** Pass 7 is the only pass with real freedom in ordering, provided it lands after 4. Everything else has a hard upstream dependency.

**Rollback.** Passes 2 and 3 are additive and safe to ship dark — they write data nothing reads. Pass 4 is the first one that changes recovery behavior, and it is the one that deserves a feature flag.
