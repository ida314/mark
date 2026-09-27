# Tool-Call Architecture Plan

## Goal

Build an agent architecture that reduces load on the main LLM while preserving access to a large and growing capability surface.

The runtime should own execution mechanics such as context-window management, tool-call lifecycle, durable run state, and event visibility. The main LLM should primarily act as an orchestrator: interpret the user's request, determine whether it should answer directly or delegate, select the appropriate subagent strategy, and initiate tool discovery when needed.

Most complex requests should be handled through subagents, but delegation should not be mandatory for every request. The orchestrator must first understand the request and decide whether delegation is actually necessary.

The system should optimize for:

* A small permanent tool surface for the orchestrator
* Reliable delegation to specialized workers
* Dynamic discovery of tools rather than exposing the complete tool catalog
* Strong separation between orchestration, task execution, tool routing, and runtime responsibilities
* Explicit memory scopes
* Controlled context-window handoffs
* Durable run state that survives process failure, with side-effect safety
* Runtime-owned tool-call events that the frontend can observe

The goal is not to minimize the number of tools in the system. The goal is to keep the active context small and routing reliable while preserving access to a much larger capability surface.

---

## 1. Runtime Responsibilities

The runtime should own concerns that should not depend on the main LLM remembering to perform them.

These include:

* Context-window limits
* Context-handoff triggering
* Tool-call execution state
* Tool-call lifecycle events
* Agent invocation
* Agent-local working memory
* Event propagation to the frontend
* Temporary tool injection and removal
* Run journaling
* Checkpoint writes at defined boundaries
* Resume and reconciliation after failure
* Effect-ledger management and idempotency enforcement

### Context Limit and Handoff

The runtime, rather than the orchestrator, should monitor remaining context.

When the active context reaches approximately **8,000 tokens remaining**, the runtime should inject a handoff instruction requiring the current orchestrator to construct a structured handoff for a fresh orchestrator instance.

The threshold should be configurable rather than hard-coded. An initial threshold of roughly 8,000 remaining tokens provides enough space for the current agent to summarize the task, preserve unresolved state, and complete the handoff without operating at the edge of the model's context window.

The handoff should contain structured state rather than a transcript dump.

For example:

```text
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

The runtime should then start a new orchestrator with:

```text
system instructions
relevant persistent memory
structured handoff
necessary recent conversation context
currently relevant tools
```

The old orchestrator's complete context should not automatically be copied into the new context.

Handoff generation should be treated as a reusable runtime component rather than a single context-limit mechanism. It has three triggers:

```text
context exhaustion        # ~8k tokens remaining
cold resume               # crash, restart, or long idle gap
stale thread reentry      # user returns after an extended period
```

See Section 16 for how handoff interacts with checkpoint restore.

### Durability and Resume

The runtime should assume that any process can die at any point without warning.

Execution state is therefore persisted outside the process. The runtime owns:

* An append-only journal of all lifecycle events per run
* Periodic checkpoints derived from that journal
* An effect ledger recording every side-effecting tool call
* Reconciliation of uncertain effects on resume

The controlling distinction:

```text
execution state
  "where in the work are we"
  run-scoped
  disposable once the run completes

memory
  "what does the agent know"
  user-scoped
  durable across sessions
```

Checkpointing covers execution state. Sections 11 and 12 cover memory. These are separate systems that interact only at the promotion boundary.

---

## 2. Main Orchestrator

The main agent should primarily coordinate work.

Its responsibilities are:

* Interpret user intent
* Maintain the conversational thread
* Decide whether the request can be handled directly
* Decide whether a subagent is necessary
* Select an appropriate durable subagent role
* Create or request task-specific ephemeral workers
* Describe required capabilities for tool discovery
* Integrate subagent results
* Present the final response to the user

The default decision process should be:

```text
User request
    ↓
Interpret request
    ↓
Can the orchestrator handle this cheaply and reliably?
    ├── Yes → handle directly
    └── No
         ↓
Determine required worker / capability
         ↓
Subagent selection
         ↓
Tool discovery and routing
         ↓
Execute through subagent
         ↓
Return compressed result to orchestrator
```

Most multi-step tasks should move through the subagent path, but simple requests should not incur delegation overhead unnecessarily.

---

## 3. Agent Hierarchy

Use a relatively small set of durable subagent roles, approximately **5–15**, rather than creating a permanent specialist for every narrow capability.

Examples might include:

```text
coder
researcher
memory
communications
planner
data
automation
```

These are durable roles with stable instructions and domain-specific behavior.

Within those roles, the system should be capable of creating **task-specific ephemeral workers**.

For example:

```text
Durable role:
coder

Ephemeral workers:
- repository explorer
- implementation worker
- test/debug worker
```

or:

```text
Durable role:
researcher

Ephemeral workers:
- source finder
- document analyst
- synthesis worker
```

Ephemeral workers should exist only for the current task or workflow unless there is a strong reason to preserve them.

This avoids two undesirable extremes:

```text
one general-purpose agent doing everything
```

and:

```text
hundreds of permanently defined micro-agents
```

The durable role provides stable domain behavior. Ephemeral workers provide task-specific specialization.

---

## 4. Subagent Selection Pipeline

Before selecting tools, the orchestrator should determine what kind of worker is needed.

Conceptually:

```text
request
  ↓
orchestrator interpretation
  ↓
direct execution?
  ├── yes → execute directly
  └── no
       ↓
select durable role
       ↓
determine whether task-specific workers are useful
       ↓
instantiate ephemeral worker if necessary
```

The orchestrator should pass a task specification containing enough information for the worker to operate independently.

Example:

```text
delegate(
  role="coder",
  task="""
  Locate the authentication token expiry configuration.
  Change the expiry to 24 hours.
  Run the relevant tests.
  Return the files changed, implementation summary, and test results.
  """
)
```

Subagents should generally complete an entire coherent workflow rather than repeatedly returning control to the orchestrator after every tool call.

The task specification is also the cache key for worker results. See Sections 14 and 16.

---

## 5. Tool Discovery Pipeline

Tool selection should become a separate pipeline rather than requiring every agent to carry the complete tool catalog in its prompt.

All tools should have an English-language description and an embedding representation.

When an orchestrator or subagent needs capabilities, the requesting agent should first produce a natural-language capability description.

For example:

```text
"I need tools that can search a source-code repository,
read matching files, edit files, and execute tests."
```

That description is then used for tool retrieval.

### Stage 1 — Semantic Tool Retrieval

Embed:

```text
required capability description
```

and compare it against embeddings for:

```text
tool descriptions
```

Return the top-K candidate tools.

Conceptually:

```text
capability_description
    ↓ embedding
tool-description vector search
    ↓
top-K candidate tools
```

While the total number of tools remains small, this embedding stage can be bypassed or implemented as a lightweight filter.

However, the architecture should support semantic retrieval from the beginning because it becomes necessary as the tool catalog grows.

Do not design the system around the assumption that every tool will permanently fit in context.

---

## 6. Specialized Tool-Call Router

Semantic similarity alone should not determine which tools are exposed to a worker.

After retrieving the top-K semantically relevant tools, pass those candidates to a specialized **tool-call router**.

The router should inspect:

```text
task
agent role
candidate tools
tool descriptions
tool schemas
effect_class
```

and return a much smaller final set, usually around **3–6 tools**.

Pipeline:

```text
Agent describes required capability
        ↓
Embedding retrieval
        ↓
Top-K candidate tools
        ↓
Tool-call router
        ↓
3–6 selected tools
        ↓
Inject tools into worker context
```

This creates two filtering stages:

```text
semantic retrieval
→ reasoning-based tool routing
```

The first stage provides scalable recall.

The second stage provides precision.

This distinction becomes increasingly important as the tool catalog grows.

---

## 7. Temporary Tool Exposure

Tools selected through the discovery pipeline should normally be scoped to the current worker or task.

The runtime should:

1. Retrieve candidate tools.
2. Route them through the tool-call router.
3. Inject the selected tools into the worker context.
4. Allow the worker to use them.
5. Remove them when the task is complete unless they remain relevant.

A worker should not accumulate every tool it has ever used.

Tool availability should be treated as temporary execution context.

Tool routing decisions are not persisted in checkpoints. They are cheap to recompute on resume, and recomputing them avoids restoring a stale tool set.

---

## 8. Main Agent Tool Surface

The orchestrator should keep only a small permanent tool surface.

A reasonable initial target is approximately **8–12 always-visible capabilities**, but the exact number should be determined empirically.

Likely permanent capabilities include orchestration-level operations such as:

```text
delegate
tool_search

time_now
calendar_upcoming
coursework_due

reminder_set
watcher_add
notify_user

goals_list
open_loops_list
```

Potentially retain frequently used operations such as:

```text
goal_upsert
open_loop_add
open_loop_close
profile_read
```

However, low-level capabilities should generally not remain permanently visible simply because they exist.

Examples that should normally be delegated or dynamically loaded include:

```text
filesystem operations
shell execution
web search
web fetching
email APIs
deep-memory APIs
document APIs
specialized integrations
```

Whether a tool remains permanent should be determined by observed usage and routing performance.

---

## 9. Coding and Filesystem Work

Coding and repository manipulation should normally be handled behind the durable `coder` role.

Move low-level tools such as:

```text
fs_list
fs_read
fs_search
fs_write
shell_exec
```

out of the orchestrator's permanent tool surface.

The coder should own:

* Repository exploration
* Code search
* File inspection
* Implementation
* Editing
* Test execution
* Debugging
* Build commands
* Relevant shell operations

Instead of requiring the orchestrator to perform:

```text
fs_search
→ fs_read
→ fs_read
→ fs_write
→ shell_exec
```

the orchestrator should issue a higher-level task:

```text
delegate(
  role="coder",
  task="""
  Locate the authentication token expiry configuration,
  change it to 24 hours,
  run the relevant tests,
  and report the files changed and test results.
  """
)
```

Use `coder` when:

* Repository exploration is required
* Multiple files may need inspection
* Code must be modified
* Tests or shell commands are required
* The number of low-level operations is uncertain

Very small coding questions that require no repository access may still be answered directly by the orchestrator.

Note that `fs_write` and `shell_exec` are `unsafe_write` effects. Their results are journaled and their retry behavior is governed by Section 16.

---

## 10. Web Research

Open-ended web work should normally be handled behind the `researcher` role.

Capabilities such as:

```text
web_search
web_fetch
```

should not need to remain permanently visible to the orchestrator.

The researcher should handle:

* Open-ended research
* Multiple searches
* Source comparison
* Source retrieval
* Document reading
* Evidence extraction
* Significant synthesis

The orchestrator should provide the research objective rather than micromanaging individual searches.

For example:

```text
delegate(
  role="researcher",
  task="""
  Determine the current state of support for feature X
  across projects A, B, and C.

  Prefer primary documentation.
  Return the relevant differences, source links,
  important caveats, and publication dates.
  """
)
```

The researcher should return a compressed, structured result rather than its full search transcript.

Research is expensive and read-only, which makes it the clearest case for result reuse on resume rather than re-execution.

---

## 11. Memory Architecture

Memory should be explicitly divided into three conceptual buckets.

Memory is distinct from execution state. Execution state answers "where in the work are we" and is scoped to a single run. Memory answers "what does the agent know" and is scoped to the user across every run and session. Checkpoints capture the former. The three buckets below describe the latter.

### Working Memory

Working memory exists to complete the current task.

Properties:

* Task-local
* Agent-local
* Short-lived
* Not globally readable by other subagents by default

For example:

```text
coder worker A working memory
```

should not automatically be visible to:

```text
researcher worker B
```

If information must cross agent boundaries, it should be passed explicitly through structured messages, results, or runtime-managed shared task state.

Working memory should disappear when its task scope ends unless selected information is promoted into longer-lived memory.

Working memory is the one memory type that sits inside execution state. It is captured by checkpoints and discarded when the run completes.

### Episodic Memory

Episodic memory represents past events or experiences.

Examples:

```text
The user previously changed project X to use Postgres.
A deployment failed because environment variable Y was missing.
The user asked the agent to investigate issue Z last week.
```

This is memory of **what happened**.

### Semantic Memory

Semantic memory represents durable facts or knowledge.

Examples:

```text
The user's primary project repository is X.
Service Y depends on Redis.
The user prefers Python for data-processing utilities.
```

This is memory of **what is known**.

The existing harness should be inspected to determine whether episodic and semantic information are currently stored separately or merely represented as different records within the same retrieval system.

Even if they share the same physical database, they should remain distinct logical memory types because they have different retrieval and update semantics.

---

## 12. Memory Promotion

Information should not automatically move from working memory into persistent memory.

Use an explicit promotion process:

```text
working memory
    ↓
memory classification
    ├── discard
    ├── episodic memory
    └── semantic memory
```

Examples:

```text
temporary compiler error
→ discard

completed migration and resulting outcome
→ episodic

repository uses PostgreSQL as primary database
→ semantic
```

This reduces accumulation of low-value state.

### Promotion and Checkpoint Consistency

Promotion is the one place where disposable execution state writes into durable user-scoped memory, so it must be transactional with respect to checkpointing.

Requirements:

* Memory writes are effects and carry idempotency keys (Section 16).
* Promotions are batched at task or run boundaries rather than performed opportunistically mid-task.
* Each checkpoint records a `memory_watermark` marking the episodic and semantic write positions that had been committed at that point.
* Pending, uncommitted promotions are carried in the checkpoint as `pending_promotions[]`.

Without this, a crash between classification and write produces either lost promotions or, worse, duplicate semantic facts on resume. Duplicate semantic memory is particularly corrosive because it silently degrades retrieval quality rather than failing loudly.

---

## 13. Subagent Isolation

Each worker should receive only what it needs:

```text
task instructions
relevant conversation state
relevant persistent memory
selected tools
worker-local working memory
```

It should not automatically receive:

```text
the entire user conversation
all persistent memories
all tools
other workers' scratch state
full transcripts from previous workers
```

Isolation reduces context usage and lowers the chance that unrelated state affects execution.

When multiple workers need to collaborate, communication should occur through explicit task-state objects or structured results.

Isolation also makes workers independently re-runnable, which is what allows the worker boundary to serve as the unit of atomicity in Section 16.

---

## 14. Standardized Subagent Results

Subagents should return structured, compressed results.

A standard result schema should include:

```text
status
answer
evidence
actions_taken
followups
```

For example:

```json
{
  "status": "completed",
  "answer": "Token expiry was changed from 1 hour to 24 hours.",
  "evidence": [
    "config/auth.ts: TOKEN_EXPIRY = '24h'",
    "auth tests: 18 passed"
  ],
  "actions_taken": [
    "inspected auth configuration",
    "modified token expiry",
    "ran authentication tests"
  ],
  "followups": []
}
```

Raw subagent transcripts should not normally be injected back into the orchestrator's context.

Full transcripts may be retained by the runtime for:

```text
debugging
observability
evaluation
auditing
```

but should not become normal orchestration context.

### Verification

A worker's report is a claim about work, produced by a second inference over a truncated
transcript. It is not the work, and it is not evidence of the work. The runtime holds the
evidence already: the run journal records every tool the worker requested, with its
arguments, and the outcome of every one of them.

So when a worker returns, the runtime reads that worker's own journal back and attaches what
it actually did:

```text
tool calls, with arguments
failures and policy denials
the last shell_exec exit code
files and urls touched
the worker's own turn status
```

It then checks the report against that ledger, deterministically and with no model call. A
check that cannot name the journal event it read is a grader, not a verifier. Known
contradictions:

```text
a test claimed to pass with no passing test run
a file cited as evidence that was never read
"could not access X" with no attempt recorded
a completed report from a turn that failed
a completion with no tool calls at all
a change claimed in actions_taken with nothing written
```

The result therefore carries two independent axes, and collapsing them loses information:

```text
status              what the worker says happened      completed | blocked | uncertain
validation_status   what the runtime can corroborate   valid | uncertain | invalidated
```

`invalidated` means a hard contradiction: the report is not usable as an answer, is not
cached, and its proposed memories are dropped. It is not the same as a failed delegation —
the worker ran, and what it did is on the record. What is refused is the claim.

### Result Persistence

Completed worker results are persisted and content-addressed by a hash of the task specification.

```text
result_key = hash(durable_role, task_spec, relevant_context_refs)
```

This serves two purposes:

1. **Resume.** A run that dies after three completed research workers should not re-run those workers. The checkpoint carries the results, and re-delegation of an identical task spec is a cache hit.
2. **Deduplication.** An orchestrator that redundantly delegates the same task within a run gets the cached result instead of paying for it twice.

Cache entries are run-scoped by default. Cross-run reuse is a later optimization and should not be attempted in the first version, because staleness semantics for research and repository state are not obvious.

---

## 15. Frontend and Runtime Event Ownership

Tool-call state should be owned by the runtime rather than inferred from LLM text.

The runtime should emit structured events such as:

```text
agent_started
agent_finished

tool_requested
tool_started
tool_progress
tool_finished
tool_failed

handoff_started
handoff_finished

worker_created
worker_finished

message_appended
checkpoint_written
effect_intended
effect_committed
run_resumed
run_forked
```

The frontend should subscribe to these runtime events.

Conceptually:

```text
Agent
  ↓
Runtime
  ├── executes tools
  ├── tracks state
  ├── appends to the run journal
  └── emits events
          ↓
      Frontend
```

This allows the frontend to display:

```text
Searching repository…
Reading files…
Running tests…
Research complete.
```

without requiring the language model to manually generate UI-status messages.

**The event stream is durable and append-only, not merely streamed.** It is written to persistent storage before or concurrently with delivery to the frontend. This is the single design decision that makes checkpointing cheap: the journal that drives the UI is the same journal that reconstructs run state. Do not build two separate event paths.

If the current harness already follows the runtime-owned event model, preserve it and add persistence rather than duplicating tool state inside the agent layer.

---

## 16. Checkpointing and Resume

### Purpose

Checkpointing exists to do three things:

```text
resume    process dies, deploy happens, machine sleeps;
          continue without losing accepted work
          or repeating side effects

fork      user rewinds: "no, go back to before that"

replay    deterministic traces for evaluation and debugging
```

These are distinct from the handoff system. A handoff is a **lossy semantic compression** used to survive a context limit. A checkpoint is a **lossless mechanical snapshot** used to survive a process failure. Both are needed, and they share infrastructure, but they should not be conflated.

### Journal

The Section 15 event stream, written append-only, keyed by `run_id` with a monotonic `seq`.

The governing invariant:

```text
state = fold(reduce, journal, initial)
```

The journal is the source of truth. Live in-process state is a cache of folding the journal. A checkpoint snapshot is an acceleration structure that may lag the journal but must never disagree with it, because it is always recoverable by re-folding.

SQLite is sufficient for a single-user personal agent. Do not reach for a distributed log.

### Checkpoint Boundaries

Snapshot at boundaries rather than continuously:

```text
turn_end           end of each orchestrator turn
worker_finished    a durable or ephemeral worker returns a result
pre_effect         immediately before any unsafe_write effect
handoff            after a structured handoff is produced
manual             user-requested marker
```

### Checkpoint Record

```text
checkpoint
  run_id
  seq
  created_at
  trigger              # turn_end | worker_finished | pre_effect | handoff | manual
  orchestrator_id
  messages_ref         # pointer into journal, not a copy
  handoff_object       # latest structured handoff, if one exists
  open_workers[]       # worker_id, role, task_spec, status
  worker_results[]     # standardized Section 14 results, keyed by result_key
  pending_promotions[]
  effects_cursor
  memory_watermark     # episodic / semantic write positions
```

Note that `messages_ref` is a pointer. The checkpoint should not duplicate conversation content already in the journal.

### Workers Are the Unit of Atomicity

Do not checkpoint inside a worker in the first version.

Worker transcripts are already discarded (Section 14), so the natural recovery for a worker that dies mid-flight is re-delegation with the same task specification. Completed worker results are cached by `result_key`, so resume does not re-run finished work.

This gives most of the value of fine-grained checkpointing at a fraction of the complexity:

```text
worker completed      → result restored from cache, not re-run
worker in flight      → re-delegated from its task_spec
worker touched an
unsafe_write effect   → gated by the effect ledger before re-delegation
```

Mid-worker checkpointing can be added later for specific long-running roles if traces show it is worth the complexity.

### Effect Ledger

This is the part that cannot be skipped, because the agent sends email, sets reminders, writes files, and executes shell commands. LLM tool calls are not naturally idempotent, and blind retry after a crash produces duplicate real-world actions.

Every tool declares an effect class:

```text
read              no external state change; free to re-execute
idempotent_write  re-execution converges to the same state
unsafe_write      re-execution may duplicate a real-world action
```

Every effecting call is journaled:

```text
effect
  idempotency_key = hash(run_id, step_id, tool_name, canonical_args)
  state            # intended | started | committed | failed | orphaned
  tool
  effect_class
  args_hash
  result_ref
  created_at
```

The protocol:

1. Write `intended` before execution.
2. Transition to `started` when the call is dispatched.
3. Write `committed` with `result_ref` on success, or `failed` on a clean failure.

An effect left at `started` when the process dies becomes `orphaned` on resume.

### Reconciliation on Resume

```text
for each orphaned effect:
    read              → re-execute freely
    idempotent_write  → re-execute
    unsafe_write      → never auto-retry
                        surface to the orchestrator as
                        status="uncertain" with the recorded args
```

An `uncertain` effect is a first-class observation, not an error. The orchestrator decides whether to verify (read back the calendar event, check the sent-mail folder), ask the user, or proceed without it. Hiding the ambiguity is worse than surfacing it.

### Resume Policy

```text
resume(run_id)
    load latest checkpoint
    reconcile orphaned effects
    restore cached worker results
    if fresh and messages fit within budget:
        rehydrate full message list        # mechanical, lossless
    else:
        rehydrate from handoff_object
                   + recent turns
                   + memory refs           # semantic, lossy
```

If a cold resume requires a handoff object and none exists, the runtime generates one from the journal using a cheap model call before starting the new orchestrator. This is the same generator used at the context-limit threshold in Section 1, invoked under a different trigger.

`WARM_WINDOW` and the message budget should be configurable. Start with something like a few hours and tune from traces.

### Fork and Undo

User-initiated rewind creates a new run:

```text
fork(run_id, seq) →
    new run with
      parent_run_id
      forked_from_seq
      state rehydrated from checkpoint at seq
```

The original run is left intact. Its journal is not rewritten.

The first version does **not** automatically revert side effects. On fork, the runtime lists every `committed` effect after `seq` and presents it to the user:

```text
Since that point I:
  - modified 3 files in src/auth/
  - sent 1 email
  - created 1 calendar event

Reverting the conversation. I have not undone any of the above.
```

This keeps the semantics honest. Automatic file rollback is a plausible later addition for the `coder` role specifically, where content-addressed pre-images of edited files are cheap. Automatic reversal of email and calendar effects is not achievable in general and should not be promised.

### Explicitly Out of Scope for the First Version

```text
durable-execution engines (Temporal, Restate, DBOS, Inngest)
deterministic LLM replay guarantees
process or VM-level snapshots
OS-side-effect capture (installed packages, spawned processes)
mid-worker checkpoints
automatic side-effect reversal
cross-run result caching
```

These are real techniques and several of them are where the field is heading. None of them are worth their overhead for a single-user personal agent, and most of them constrain application code in ways that are painful to retrofit out.

---

## 17. Direct Execution vs Delegation

Delegation should be common, but not universal.

The orchestrator should handle a task directly when:

* It can answer from existing context
* No specialized tool access is needed
* The operation requires only one or two simple high-frequency tools
* Delegation overhead would exceed the complexity of the task

Delegation is preferred when:

* Multiple tool calls are likely
* Exploration is required
* The number of steps is uncertain
* Specialized domain reasoning is useful
* Multiple files or documents must be examined
* The task requires iterative execution
* A specialist can compress a large amount of intermediate state

A useful mental model is:

```text
simple + predictable
→ direct

complex + iterative
→ delegate

rare capability
→ discover tools, then delegate or execute
```

A secondary argument for delegation: work performed inside a worker is recoverable by re-running a single task specification, whereas a long chain of direct orchestrator tool calls must be reconstructed from the journal turn by turn.

---

## 18. Dynamic Tool Loading

Capabilities outside the permanent orchestrator set should be discovered dynamically.

The target pipeline is:

```text
task
 ↓
capability description
 ↓
semantic tool retrieval
 ↓
top-K candidates
 ↓
tool-call router
 ↓
3–6 tools
 ↓
worker execution
```

Initially, while the catalog is small, semantic retrieval may simply return most or all tools and allow the router to perform the meaningful filtering.

As the catalog grows, the embedding layer becomes increasingly important.

This permits scaling from:

```text
20 tools
```

to:

```text
hundreds or thousands of tools
```

without expanding every agent's prompt proportionally.

---

## 19. Tool Granularity

Not every API endpoint should necessarily become a standalone permanent tool.

A tool should remain low-level when:

* Fine-grained control is important
* Different workflows combine it in many ways
* The schema is compact
* It is frequently reused

A higher-level capability may be preferable when:

* The same low-level sequence appears repeatedly
* Correct execution requires domain-specific sequencing
* The workflow requires exploration
* Results require significant synthesis
* Its low-level schemas consume substantial context despite infrequent use

The tool surface should evolve from observed usage rather than remain fixed.

### Required Tool Metadata

Every tool in the registry carries:

```text
name
English description
schema
embedding
effect_class          # read | idempotent_write | unsafe_write
metadata
```

`effect_class` is mandatory and has no default. A tool registered without one should fail registration rather than silently fall back to `read`, because a mislabeled effect class is exactly the failure that produces a duplicate email on resume.

Granularity interacts with effect classification: consolidating a low-level sequence into a higher-level tool also consolidates its effect boundary, which is usually an improvement for idempotency but makes partial failure coarser.

---

## 20. Observability

Because more work will occur outside the main orchestrator, observability becomes important.

Track at minimum:

```text
request_id
run_id
orchestrator_id
worker_id
durable_role
ephemeral_role
tools_considered
tools_selected
tools_called
tool failures
worker duration
worker token usage
handoffs
memory reads
memory writes
final status

checkpoint_seq
checkpoints_written
resume_count
orphaned_effects
uncertain_effects_surfaced
worker_results_reused
fork_lineage            # parent_run_id, forked_from_seq
journal_size
```

This information should be runtime telemetry, not something placed into the LLM context by default.

The journal makes most of this free rather than a separate instrumentation effort.

---

## 21. Failure Handling

The runtime and orchestrator should distinguish between:

```text
tool failure
worker failure
routing failure
insufficient capability
context exhaustion
process failure
orphaned effect
user-facing task failure
```

A worker should be able to report:

```text
status="blocked"
```

with a structured reason rather than attempting to hide a failure.

Example:

```json
{
  "status": "blocked",
  "answer": null,
  "evidence": [],
  "actions_taken": [
    "searched repository",
    "located deployment configuration"
  ],
  "followups": [
    "requires access to deployment credential store"
  ]
}
```

The orchestrator can then decide whether to:

```text
discover another capability
create another worker
ask the user
return the partial result
```

A second non-success status exists for reconciliation:

```text
status="uncertain"
```

This indicates that an action may or may not have taken effect, typically an orphaned `unsafe_write` recovered on resume.

```json
{
  "status": "uncertain",
  "answer": null,
  "evidence": [],
  "actions_taken": [
    "attempted to send the follow-up email"
  ],
  "followups": [
    "process failed after dispatch; delivery unconfirmed",
    "verify sent items before retrying"
  ]
}
```

`blocked` means the work did not happen. `uncertain` means it is not known whether the work happened. These call for different orchestrator behavior, which is why they are separate statuses.

### Limits Should Offer a Choice, Not Only an Ending

A step budget, a final-nudge and a stuck-tool withdrawal all end a turn today. Ending is the
right floor, but it is the wrong ceiling: the same four options the orchestrator has when a
worker reports `blocked` are available to a turn that has run out of room, and a runtime that
only ends the turn throws them away.

A turn that reaches its step budget should get one bounded escape: a single step in which the
only thing it can do is delegate a narrower brief, followed by the summary step it would have
had anyway. A turn that declines the escape and answers has ended by choice, and should not be
recorded as abandoned — "abandoned" should mean the budget ran out with work still pending,
not that the budget was fully used.

A policy denial is the other case. A denial is final: it is a rule's answer about the action,
not a complaint about the arguments, and a model that rephrases a denied call is spending its
budget on an appeal that cannot succeed. A denial repeated with identical arguments should
withdraw the tool for the rest of the turn and say why — while a differently-argued call to
the same tool stays available, because a rule may match on an argument.

---

## 22. Rollout Plan

### Phase 1 — Baseline

Measure the current architecture on representative tasks.

Track:

```text
average tool calls per task
tool-selection failures
context usage
latency
completion rate
main-agent token usage
```

Also record how often the current agent unnecessarily sees or considers irrelevant tools.

Record how often runs are lost or restarted today, and what the recovery currently costs.

### Phase 2 — Runtime Context Management

Move context-limit handling into the runtime.

Implement:

```text
remaining-token monitoring
configurable handoff threshold
structured handoff generation
fresh-orchestrator initialization
```

Begin with approximately:

```text
handoff threshold = 8,000 tokens remaining
```

and tune it based on real traces.

### Phase 2B — Run Journal and Checkpointing

Implement durable execution state.

```text
append-only run journal (SQLite)
checkpoint writes at defined boundaries
effect ledger with idempotency keys
effect_class on every registered tool
resume with reconciliation
fork with committed-effect disclosure
```

This phase belongs here rather than late, for two reasons. The handoff mechanism from Phase 2 becomes more useful once a journal exists to generate handoffs from on cold resume. And retrofitting an effect ledger after the tool surface has been rebuilt means auditing and reclassifying every tool a second time.

Reuse the handoff generator from Phase 2 as the cold-resume rehydration path.

### Phase 3 — Introduce Memory Scopes

Explicitly model:

```text
working
episodic
semantic
```

memory.

Ensure worker working memory is isolated by default.

Determine whether the current harness already differentiates episodic and semantic memory internally. If not, add a logical distinction even if both use the same storage backend.

Make promotion transactional against the checkpoint watermark from Phase 2B.

### Phase 4 — Reduce Main Tool Surface

Remove low-level capabilities from the permanent orchestrator set, including:

```text
fs_*
shell_exec
web_search
web_fetch
memory_history
memory_search
gmail_search
gmail_message
```

Keep them available through specialists or dynamic discovery.

Assign each an `effect_class` as it moves.

### Phase 5 — Standardize Delegation

Define the delegation interface.

Support:

```text
durable_role
task
relevant_context
constraints
expected_output
```

Ensure durable roles such as:

```text
coder
researcher
memory
```

can complete coherent workflows without repeatedly handing control back to the orchestrator.

Add task-specific ephemeral workers where they improve execution.

Make the task specification stable enough to serve as a cache key.

### Phase 6 — Standardize Worker Results

Require workers to return:

```text
status
answer
evidence
actions_taken
followups
```

Do not inject raw worker transcripts into orchestrator context except in debugging modes.

Persist results content-addressed by `result_key` so resume reuses completed work.

### Phase 6B — Verify Worker Results

Read each finished worker's own journal back and attach what it did to what it claims.

Flag the contradictions deterministically, with no model call. Give the result a
`validation_status` independent of its `status`, and make an invalidated result
non-cacheable, memory-proposing-free, and unusable as an answer.

Give a turn that hits a step limit or a repeated denial one bounded choice before it ends.

Measure the false-positive rate per check. A check that fires on true reports costs a worker
every time it does.

### Phase 7 — Add Tool Discovery

Represent every tool with:

```text
name
English description
schema
embedding
effect_class
metadata
```

Implement semantic top-K retrieval.

While the catalog remains small, allow this stage to operate as a lightweight or near-pass-through filter.

### Phase 8 — Add the Tool-Call Router

Insert a specialized router after semantic retrieval.

The router receives:

```text
task
worker role
top-K candidate tools
tool descriptions
tool schemas
effect_class
```

and selects the approximately 3–6 tools that should actually be exposed to the worker.

Evaluate both:

```text
retrieval recall
router precision
```

rather than treating tool selection as a single problem.

### Phase 9 — Runtime Event Stream

Ensure all agent and tool-call lifecycle events originate from the runtime.

Expose an event stream that the frontend can subscribe to.

The frontend should not need to parse assistant prose to determine whether a tool is currently running.

The stream is the persisted journal from Phase 2B surfaced to the frontend, not a parallel event path. Frontend reconnection should replay from a last-seen event id against the journal rather than losing state.

### Phase 10 — Evaluate

Compare against the baseline.

Measure:

```text
wrong-tool rate
tool-router precision
tool-retrieval recall
main-agent token usage
worker token usage
main-agent calls per complex task
completion rate
delegation latency
handoff success rate
memory retrieval quality

resume success rate
orphaned-effect rate
duplicate-side-effect incidents
worker-result reuse rate
checkpoint write overhead (latency and storage)
```

The journal also functions as a regression corpus: replaying a recorded run with cached tool results exercises the full orchestration path without LLM or tool cost.

### Phase 11 — Tune

Use observed behavior to decide:

* Which tools deserve permanent orchestrator visibility
* Which workflows deserve durable specialist roles
* Which durable roles should create ephemeral workers
* Which APIs should be combined into higher-level tools
* Whether delegation is too aggressive or too conservative
* Whether the tool retrieval top-K is too large or too small
* Whether the final routed tool set should be larger or smaller
* Whether the handoff threshold should change
* Whether persistent-memory promotion is too aggressive
* Whether checkpoint boundaries are too frequent or too coarse
* Whether any long-running role justifies mid-worker checkpointing
* Whether journal retention and pruning policy needs adjusting
* Whether `coder` should gain content-addressed file pre-images for true undo

---

## End State

The target architecture should resemble:

```text
                               ┌── Coder
                               │    ├── ephemeral repo explorer
                               │    ├── ephemeral implementer
                               │    └── filesystem / shell tools
                               │
                               ├── Researcher
                               │    ├── ephemeral source finder
                               │    ├── ephemeral analyst
                               │    └── web / document tools
                               │
User → Runtime → Orchestrator ──┤
                               ├── Memory
                               │    └── episodic / semantic operations
                               │
                               ├── Other durable roles
                               │    └── task-specific ephemeral workers
                               │
                               └── Dynamic integrations
                                    └── Gmail / Calendar / Drive / etc.
```

Tool assignment follows a separate pipeline:

```text
task requirement
      ↓
natural-language capability description
      ↓
tool-description embedding retrieval
      ↓
top-K candidate tools
      ↓
specialized tool-call router
      ↓
3–6 task-relevant tools
      ↓
worker
```

Memory follows:

```text
worker
  ↓
isolated working memory
  ↓
promotion decision
  ├── discard
  ├── episodic memory
  └── semantic memory
```

Context continuity follows:

```text
orchestrator context
      ↓
runtime detects ~8k tokens remaining
      ↓
structured handoff request
      ↓
handoff object
      ↓
fresh orchestrator
```

Durability follows:

```text
every lifecycle event
      ↓
append-only run journal
      ↓
checkpoints at turn / worker / pre-effect / handoff boundaries
      ↓
process failure
      ↓
resume: reconcile effects, restore worker results,
        rehydrate from messages or handoff object
      ↓
continued run   (or forked run, on user rewind)
```

Frontend state follows:

```text
agents / tools
      ↓
runtime
      ↓
persisted journal
      ↓
structured lifecycle events
      ↓
frontend subscriber
```

The runtime owns context boundaries, execution state, durability, agent lifecycle, tool lifecycle, and frontend-visible events.

The orchestrator owns conversation, intent interpretation, decomposition, routing, and final synthesis.

Durable specialists provide stable domain behavior.

Ephemeral workers provide task-specific specialization, and serve as the unit of atomicity for recovery.

The tool-discovery pipeline absorbs catalog growth.

Working memory remains isolated, while episodic and semantic memory provide longer-lived continuity.

The run journal makes execution state recoverable, side effects non-duplicating, and traces replayable, using the same event stream that drives the interface.

The final system should make complex workflows easier for the LLM by exposing **less context at any one time**, not by reducing the total capabilities available to the system.
