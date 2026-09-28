# Agent Runtime

Personal LLM agent. Orchestrator delegates to durable specialist roles; the runtime owns
context boundaries, execution state, durability, and lifecycle events.

## Governing invariant

The run journal is the source of truth.

```
state = fold(reduce, journal, initial)
```

A checkpoint snapshot is an acceleration structure. It may lag the journal. It may never
disagree with it, because it is always recoverable by re-folding.

## Vocabulary

| Term | Meaning |
|---|---|
| `run_id` | One orchestration run. Scope for all execution state. |
| `seq` | Monotonic position in that run's journal. |
| `effect_class` | `read` \| `idempotent_write` \| `unsafe_write`. Mandatory on every tool. |
| `idempotency_key` | `hash(run_id, step_id, tool_name, canonical_args)` |
| `result_key` | `hash(durable_role, task_spec, relevant_context_refs)` |
| `memory_watermark` | Committed episodic/semantic write positions at checkpoint time. |
| checkpoint triggers | `turn_end`, `worker_finished`, `pre_effect`, `handoff`, `manual` |
| execution state | "where in the work are we." Run-scoped. Disposable. |
| memory | "what does the agent know." User-scoped. Durable. |

Handoff and checkpoint are not the same thing. A handoff is a lossy semantic compression
for surviving a context limit. A checkpoint is a lossless mechanical snapshot for
surviving a process failure.

## Commands

```
build:      uv sync                  # pure Python, no compile step
            agent sandbox build      # the shell_exec container image
test:       uv run pytest            # add -m docker / -m live for the marked suites
typecheck:  none                     # no mypy/pyright/ty is configured or installed
lint:       uv run ruff check .      # uv run ruff format . to fix formatting
```

## Working agreement

- Implementation follows `docs/plans/pass-NN-*.md`. One pass file per session.
- Read the pass file from disk. Do not work from a pasted copy.
- Each pass file has a `Must not` section. It is binding.
- Every session ends by writing or updating `docs/records/pass-NN-outcome.md`.
- Load only the outcome records the current pass declares as dependencies.
- `docs/architecture/tool-call-architecture.md` is the canonical reference. Read it on
  demand for a specific question, not at session start.

## Conventions that differ from defaults

- A tool registered without `effect_class` fails registration. There is no default.
- `unsafe_write` effects are never automatically retried after a crash. They surface as
  `status="uncertain"`.
- Worker transcripts never enter orchestrator context outside debug mode.
- A worker's report is a claim, not evidence. The runtime reads that worker's journal back,
  checks the report against it without a model call, and may mark the result `invalidated` —
  which is a separate axis from its `status`.
