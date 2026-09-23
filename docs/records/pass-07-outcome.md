# Pass 7 — Memory Scopes & Transactional Promotion — outcome

Sessions completed: **7a**, **7b**. 7c is untouched and appends to this file.

7a is an inspection session. **No code, test, migration or config changed**, and nothing was
written to the live stores — see the live-data section at the end for the counts that prove
it. `.venv/bin/ruff check src tests scripts` is clean and `.venv/bin/python -m pytest -q` is
**920 passed** as of this session's start, which is the baseline 7b inherits.

---

## Session 7a — Harness inspection

### what shipped

| file | what it is |
|---|---|
| `docs/records/pass-07-outcome.md` | this section: what the harness does with memory today, with file references |

Nothing else. The pass file's 7a exit is "a written description of current memory behavior,
with file references", and the rest of the pass depends on that description being of what is
there rather than of what the code reads as if it does.

### what the harness did before this pass

**One physical store, four record types, no scope dimension at all.** Everything durable
lives in the Postgres schema created by `migrations/0003_memory.sql`, plus a markdown mirror
on disk. Nothing in the codebase is scoped to a task, a run or a worker; nothing is discarded
when a run completes; and the word "working" does not appear.

```
migrations/0003_memory.sql:18   facts            semantic. bitemporal, append-only by trigger
migrations/0003_memory.sql:97   episodes         episodic. one row per consolidated session
migrations/0003_memory.sql:118  candidate_memories  the proposal queue everything writes into
migrations/0003_memory.sql:141  procedures       "how you have helped before"
migrations/0003_memory.sql:3    entities         + fact_entities, fact_evidence
migrations/0002_archive.sql:16  raw_events       the append-only archive; the real episodic record
~/.local/share/agent/memory/    the markdown mirror, a git repo (memory/mdrepo.py)
```

So episodic and semantic are **separate tables in one system**, retrieved by one function
and rendered into one block. They are not separate systems, and they have no separate
lifecycle: `facts` and `episodes` are both user-scoped, both durable, both written only by
the consolidation path.

**Retrieval treats them as record types of one ranked list.** `memory/retrieval.py:544`
`pack()` queries facts, episodes, procedures and pending claims in parallel, fuses them with
RRF, scores them with one formula (`retrieval.py:710`: `0.60·rrf + 0.15·recency +
0.10·importance + 0.10·confidence + 0.05·prior`), and renders them into four fixed sections
(`SECTION_SHARE` at `retrieval.py:33`, `_render` at `retrieval.py:762`). The only thing that
distinguishes episodic from semantic downstream is `Item.kind`, a string, and three dict
literals keyed by it.

**The episodic half has never had a row.** Live Postgres: `episodes` = **0** after **111**
`post_session` runs recorded `status='ok'` in `consolidation_runs`. The writer is
`memory/consolidate.py:189`, guarded by `if extraction.episode.title:` at line 179. The
extraction schema explains it, and it is this codebase's signature bug in a fourth place:

```
Extraction.model_json_schema()["required"]                -> None      (no required keys)
$defs.ExtractedEpisode.required                           -> None      (title/summary = "")
$defs.ExtractedFact.required   -> statement, subject, predicate, object, category
```

`ExtractedFact` was repaired in an earlier pass (the comments at `consolidate.py:65-78` say
why) and `ExtractedEpisode` was not, so a model that omits `episode` — which a strict guided
decode is free to do for a non-required object — produces a valid `Extraction` whose episode
has an empty title, and the writer silently skips. Nothing counts it: the stats dict at
`consolidate.py:268` records `facts_proposed` and the review counters and **has no field for
the episode**, so 111 healthy-looking runs report nothing wrong. The one test in the tree
(`tests/test_subagents_and_consolidation.py:137`) scripts an episode *with* a title, so the
empty-title path is never exercised.

Consequence for 7b: the "episodic" bucket the pass wants to separate is, in the live store,
an empty table and 35% of every retrieval budget (`SECTION_SHARE["episodes"] = 0.35`) that
has never been spent. The episodic record that actually exists is `raw_events` (1474 rows),
and `pack()` never queries it.

**Promotion today has two batching points and two synchronous bypasses.**

```
batched, off the conversation's critical path
  daemon/main.py:91   maybe_consolidate_idle  -> post_session, per session gone idle
  daemon/main.py:98   nightly                 -> post_session for each pending session,
                                                 then conflict resolution, stale checks,
                                                 regenerate_markdown
synchronous, inside a live turn
  tools/builtin_memory.py:167  memory_remember + a correction cue reaches
                               review.propose_and_review -> a *fact*, mid-turn
  agent/loop.py:744 / :973     _store_candidate, per tool result carrying candidates
  agent/subagents.py:309       a worker's candidate_memories, at worker finish
```

`post_session` (`consolidate.py:137`) is the whole promotion pipeline: read the session's new
archive rows (`repo_archive.events_for_session`, `db/repo_archive.py:140`), render them into
12k-token chunks with `[E:<uuid>]` markers (`consolidate.py:109`), extract, insert the
episode directly, insert one candidate per fact/goal/loop/procedure, then run the review gate
(`review.process_pending`, `consolidate.py:265`) and advance `sessions.consolidated_upto`.

`memory/review.py:184 process_candidate` is the only writer of `facts` — except that
`nightly` calls `repo_memory.supersede_fact` and `repo_memory.retract_fact` directly at
`consolidate.py:311-315`, using `review._judge` but not `process_candidate`. That second loop
is a real exception to "review.py is the only writer" and 7c should not assume otherwise.

The gate's order, with the checks 7c must not weaken (`review.py`): empty statement →
`contains_secret` (`:224`) → confidence floor `MIN_CONFIDENCE=0.50` → **evidence required
unless `proposed_by in ("user","md_watcher")`** (`:230`) → untrusted content may not touch
`IDENTITY_CATEGORIES` (`:234`, `:34`) → neighbour search / judge / merge / supersede →
accept above `USER_ACCEPT_CONFIDENCE=0.60` for `user` and `ACCEPT_CONFIDENCE=0.70` for
everyone else (`:316`). `memory_remember` keeps `proposed_by = ctx.actor` on both paths and
says at `builtin_memory.py:165-168` why promoting it to `"user"` would launder a model
inference through both of those exemptions.

**The markdown mirror is a third representation of the same facts**, regenerated
deterministically with no LLM (`consolidate.py:355 regenerate_markdown`,
`memory/mdrepo.py:153 write_generated` replacing only the generated block). It promotes a
fact only when `evidence_count >= 2` or `proposed_by == "user"` (`consolidate.py:366`), on
top of `repo_memory.promotable_facts`'s own filter (`db/repo_memory.py:293`: active,
confidence ≥ 0.85, `sensitivity='normal'`, five categories, older than 24h). Live: 28 active
facts, 7 ever promoted, and `profile/preferences.md` still reads `_Nothing established yet._`
although five active `preference` facts exist — none has two pieces of evidence.

**Is there task-local scratch state today? No — there are five things that are nearly it,
and none of them is memory.**

| what | where | scope | survives? |
|---|---|---|---|
| `ToolContext.extra` | `tools/base.py:43`, built once per turn at `agent/loop.py:474` | one turn, shared by every tool call in it | no; four keys only: `handoff`, `correction_cue`, `added_tools`, `approver` |
| `Session` in-process fields | `agent/loop.py:77-96` (`tools_used`, `tainted`, `private`, `summary`, `handoff`) | one conversation | partly — `private` is re-derived from the archive, `handoff` from a checkpoint |
| `AgentLoop.last_pack` | set at `agent/loop.py:421` | one turn, one process | no |
| the worker result cache | `agent/result_cache.py`, `journal/worker_results.py:43` | **run**, enforced in the fold | yes — it is journal events, `worker_result_cached` / `worker_result_reused` |
| `pending_promotions[]`, `memory_watermark` | `journal/checkpoints.py:222-223`, columns at `journal/store.py:132-133` | run | stored, read back, **written by nobody** (`checkpoints.py:388-389`, `:403`) |

The run-scoped result cache is the only existing thing shaped like "execution state that is
captured by checkpoints and discarded with the run", and it is not memory. So 7b is building
the working bucket on bare ground, and the closest precedent to copy is
`journal/worker_results.py`: state derived from a fold of the run's own journal, with the
checkpoint holding a copy that may lag and may not disagree.

**Worker isolation, as it stands.** A worker gets a fresh `AgentLoop`, a restricted registry
and its own `ToolContext` (`agent/subagents.py:177-189`), so two workers cannot see each
other's `extra` — by construction, not by a rule anything states or tests. What they *do*
share is the caller's session: `Session(id=parent_session_id, ...)` at `subagents.py:189`.
A worker's prose is archived under actor `subagent:<role>` in the caller's session
(`subagents.py:211`, `:287`, plus its own `assistant_step`/`assistant_message` rows), and
`repo_archive.recent_messages` (`db/repo_archive.py:178`) excludes `subagent:%` from the
orchestrator's *prompt history* — but `events_for_session` (`:140`) does **not**, so every
worker's transcript is consolidation input for the caller's session. That is deliberate
(`subagents.py:305-308` says a third copy would be the same text again in every consolidation
prompt), and 7b should know that "worker transcripts never enter orchestrator context" holds
for prompts and not for promotion.

### the requirement carried in from the Pass 4/5 boundary

> Messages with `synthetic=True` are never summarized or promoted as fact.

**Nothing on today's promotion path reads a message list, so the requirement is currently
satisfied structurally rather than by a check.** The facts, either way:

- The extractor's input is the **archive**, not an in-process message list:
  `post_session` → `repo_archive.events_for_session` → `render_transcript`
  (`consolidate.py:109`), which iterates `kind`, `actor`, `content`, `trust` and filters only
  on empty content and a 2000-character clip for tool results. There is no `synthetic`
  column on `raw_events` (12 columns; `trust` is the only provenance flag) and no
  kind/actor filter of any sort.
- Nothing archives a synthetic message. The six writers are
  `agent/loop.py:384` (`user_message`), `:620` (`assistant_step`), `:728` (`tool_result`),
  `:781` (`assistant_message`), `agent/subagents.py:211`/`:287` (`subagent_message`,
  `subagent_result`) and `consolidate.py:438` (`md_edit`). `observations.ClosingMessage` is
  produced only by `agent/observations.py:732` and reaches only `rehydrate.Restart.closing`
  (`agent/rehydrate.py:224`); `AgentLoop.run_turn` has **no parameter** that accepts it
  (`loop.py:315-327`), and `agent journal restart` only prints a count of them
  (`cli/app.py:1567`).
- Runtime-authored *system* text in the message list is likewise unarchived: `FINAL_NUDGE`
  and `STUCK_NUDGE` are appended to `messages` and journaled as `message_appended`
  (`loop.py:759-768`) and never written to `raw_events`. The system block is rebuilt every
  turn and is journaled as a character count only (`loop.py:459-472`).
- The one filter that exists is on the *handoff* side, not the promotion side:
  `agent/handoff.py:544-575 source()` excludes anything with `synthetic=True` or whose text
  starts with a `RUNTIME_MARKERS` string (`observations.py:617`).

What this means for 7c, stated as a risk rather than a finding: the protection is an
accident of the archive being the input. The moment a resumed turn archives its own message
list — which is the obvious way to give `Restart.closing` a runtime producer — a synthetic
tool message becomes a `tool_result` row indistinguishable from a real one, because
`raw_events` cannot express `synthetic` and `render_transcript` does not look at the text.
The cheap check available today is the `RUNTIME_PREFIX` string; the honest one is a column.

### the precedent question: may a pass add a *required* field to an existing event type?

**Verified, not decided.** `validate_payload` (`journal/events.py:499`) has exactly one
caller in `src/`: `journal/runtime.py:123`, inside `RunJournal.emit`. And `RunJournal.emit`
is the only caller of `JournalWriter.append` in the whole tree (`runtime.py:124`; grep for
`writer.append` returns that line alone). No reader validates: `JournalStore.read` /
`read_all` (`journal/store.py:441`, `:450`) construct `Event`s straight from the rows, and
`resume._rehydrate`, the ledger fold and `worker_results.cached_results_at` read payload keys
with `.get`. So the write path is the only gate, and every row already on disk folds
regardless of what the vocabulary says today.

Live evidence from `~/.local/share/agent/journal.db` (read from a copy taken with its `-wal`;
1140 events, 52 runs):

| event type | rows that would fail `validate_payload` today | why |
|---|---|---|
| `worker_created` | **4 of 5** | no `task_digest` (6a made it required) |
| `agent_finished` | **5 of 52** | no `context_tokens` and the other 5a context fields |
| `worker_finished` | **5 of 5** | `status="ok"`, outside `WORKER_STATUSES = ("completed","blocked","uncertain")` (`events.py:110`) |

That last row is the strongest version of the fact: the enum a later pass narrowed is
violated by *every* worker row in the live journal, and the fold reads them anyway. The
precedent is therefore cheap in the same way it has been twice before, and its cost is
entirely in what a *reader* assumes — a 7c reader that does `payload["memory_watermark"]`
rather than `.get` is where this stops being free. Recorded; not ruled on.

### live-data check, and the proof that nothing was written

House rule: read the real rows.

- **Journal.** `~/.local/share/agent/journal.db` was copied **with its 4.1 MB `-wal`** into
  the scratch dir and queried there; the live file was never opened by this session. At the
  start: **1140 events, 52 run ids, 24 effect rows, 0 checkpoints, `user_version=3`**. At the
  end, re-copied: **1140 events, 52 run ids**, and `diff` of the two run-id lists is empty —
  **no new run_id appeared**. Checkpoints are still off in the shipped config, which is why
  the `checkpoint` table is empty and why `pending_promotions` has no live rows to inspect.
- **Postgres.** Read-only `SELECT`s only, through `docker exec agentd-postgres-1 psql`:
  facts 47 (28 active, 7 promoted), candidate_memories 116 (0 pending; 70 accepted, 22
  merged, 21 needs_review, 3 superseding), episodes **0**, procedures 0, entities 10,
  raw_events 1474, sessions 111. Same three counts at the end of the session: 47/116/0.
- **Markdown repo.** `~/.local/share/agent/memory` read only; `git status --porcelain` is
  empty at the end and the head commit is still `039033f memory: promote 6 facts, 1 goals,
  0 procedures` from 2026-09-23 03:33.
- **One Python process was run**, to print `Extraction.model_json_schema()`, under
  `AGENT_PATHS__DATA_DIR=<scratch>` so that an import that resolved config at call time could
  not reach the live journal. It touched no database and no network.

### what deviated from the plan, and why

**1. One thing the pass file assumes turned out not to be true, and it changes 7b's job.**
The scope sentence asks whether episodic and semantic are "stored separately, or as different
record types in one retrieval system". The answer is "separate tables, one retrieval system"
— but the more useful answer is that **the episodic table is empty and always has been**, so
7b is not separating two live buckets, it is adding a third to one live bucket and one dead
one. Filed as an open question rather than fixed here: repairing the extraction schema is a
code change, and this session may not make one.

**2. No `agent memory` command was run and no runtime code was driven.** The brief's
prohibition plus the 6a precedent (seven events into the live journal from an import that
resolved config at call time) meant the SQLite file and the markdown files were read
directly, and Postgres through `psql`. The one exception is the pure-pydantic schema print
above, sandboxed by env var.

### what is now true about the code that was not before

Nothing. No code changed. What is new is written down rather than in the tree:

- `episodes` is empty after 111 successful consolidations, and the reason is a non-required
  field in an extraction schema plus a stats dict that does not count the omission.
- `validate_payload` is provably write-path-only — one caller, and one caller of the write
  it guards — and three event types already have live rows that violate today's spec.
- There is no task-local memory of any kind; the only run-scoped, checkpoint-captured,
  discarded-with-the-run state in the runtime is the worker result cache.
- Nothing on the promotion path reads a message list, so the synthetic-message rule is
  satisfied by the shape of the pipeline rather than by any check.

### schemas exactly as implemented

None written this session. For 7b/7c's convenience, the two slots already cut for this pass,
unchanged since 4a:

```sql
-- journal.db, schema v3, table `checkpoint` (journal/store.py:132)
pending_promotions text NOT NULL CHECK (json_valid(pending_promotions)),   -- always '[]'
memory_watermark   text CHECK (memory_watermark IS NULL OR json_valid(memory_watermark))
                                                                           -- always NULL
```

```
checkpoint_written.memory_watermark : dict | null   (events.py:333, req + nullable)
Checkpoint.pending_promotions       : tuple[dict, ...] = ()   checkpoints.py:222
Checkpoint.memory_watermark         : dict | None = None      checkpoints.py:223
```

`[]` and `null` are different statements and 4a's reasoning for that still binds: an empty
list is "there were none", a null is "this pass recorded none".

### deferred items, and where they went

- **Everything the pass builds — 7b and 7c.** No bucket, no isolation mechanism, no
  promotion batching, no watermark, no test.
- **The empty-episode bug — unassigned, and it is not 7a's to fix.** It is a one-field change
  to `ExtractedEpisode` plus a counter in the stats dict, but it changes extraction behaviour
  on the live consolidator and belongs to whoever owns the episodic bucket in 7b. See open
  question 1.
- **The `nightly` second writer of `facts`** (`consolidate.py:311-315`) was found, not
  touched.

### open questions for later passes

**1. The episodic bucket 7b is asked to separate is empty, and will stay empty until
`ExtractedEpisode` requires a title.** Both halves need doing together: make the field
required (or make an untitled episode an error rather than a skip), and record in
`consolidation_runs.stats` whether an episode was written, because today "the model returned
no episode" and "the code never looked" are the same observation. Until then, any 7b test
that asserts on episodic memory is asserting over a table the live system never fills — the
exact shape of "green tests over a dead path" this repo keeps finding.

**2. A `working` bucket adds a fifth `Item.kind`, and three dict literals must learn it at
once.** `TYPE_PRIOR` and `CHANNEL_WEIGHTS` (`retrieval.py:24-26`) already contain a `"raw"`
kind that nothing produces and that `_render`'s section map (`retrieval.py:767`) has no case
for. A `raw` — or a `working` — item reaching `_render` raises `KeyError`, and `pack()` is
called inside a `try/except Exception` at `agent/loop.py:408-423` that degrades to a
`Notice`, so the failure costs all of memory retrieval and says only "(memory retrieval
unavailable)". If 7b renders working memory through `pack`, `SECTION_SHARE`, the section map
and the title map change in the same commit; if it does not, the cheaper answer is that
working memory never goes through retrieval at all.

**3. There is no write position on any memory table to build `memory_watermark` out of.**
`facts`, `episodes` and `candidate_memories` are keyed by uuid7 (time-ordered, so usable as a
high-water mark) and have no bigint sequence; the only monotonic cursor in the schema is
`raw_events.id`, which `sessions.consolidated_upto` already uses. 7c has to choose between a
uuid7 high-water mark per table, a new sequence column (an additive migration plus
`agent db migrate`), or a watermark expressed as candidate ids. A count is not a position and
must not be used as one.

**4. Promotion is session-scoped and the pass wants it task-scoped.** `post_session` batches
per *session* on idle and nightly; a run is a turn (4a open question 2), a session is many
turns, and a task is smaller than both. "Promotions batch at task or run boundaries" therefore
does not map onto the existing pipeline — 7c is adding a boundary, not moving one. The two
synchronous paths (the correction-cue fast path at `builtin_memory.py:167` and
`_store_candidate` per tool result at `loop.py:744`) are mid-task writes today; both write
*candidates* except the correction-cue path, which writes a **fact** inside a live turn. That
is the one existing violation of "never promote mid-task" and it exists because Dylan wanted
a correction to take effect in the conversation that made it.

**5. Pending candidates have never been screened by `contains_secret`** — it runs at review
time (`review.py:224`), and `retrieval._channel_claims` puts still-pending candidates into the
prompt as the "Unadjudicated claims" section. Not new to this pass, and it becomes 7b's the
moment working memory renders anything unreviewed.

**6. Still open, untouched by 7a:** everything in 4a-4d's lists, plus `short_id` collisions on
uuid7 handles (every `[F:...]` ref rendered from the live table shares a prefix), which
`retrieval._render` and `memory_remember`'s `supersedes_ref` both depend on.

---

## Session 7b — Logical separation and isolation

`.venv/bin/ruff check src tests scripts` clean. `.venv/bin/python -m pytest -q`: **937 passed**
(7a's baseline was 920; +15 new tests in `tests/test_working_memory.py`, +2 from the existing
`test_journal_feed.py` parametrization over `EVENT_TYPES`). Nothing was written to the live
stores — counts at the end of this section.

### what shipped

| file | what it is |
|---|---|
| `src/agentd/memory/scopes.py` | **new.** The three buckets as data: `store`, `scope`, `ends_with`, `retrieval`, `writer`, `live`. Plus `RETRIEVAL_KINDS` and `EXECUTION_STATE`. |
| `src/agentd/journal/working_memory.py` | **new.** The fold: `notes_at` (scoped), `note_at`, `scopes_with_notes` (the unscoped audit view), `ORCHESTRATOR`, `NOTE_MAX_CHARS`, `ENTRY_VERSION`. |
| `src/agentd/agent/working_memory.py` | **new.** `WorkingMemory` (a handle bound to a `RunJournal`), `Note`, `discard_for_turn`. |
| `src/agentd/tools/builtin_working.py` | **new.** `working_memory_note`, `working_memory_list`. The only model-facing door. |
| `src/agentd/journal/events.py` | `working_memory_noted` + `working_memory_discarded` in `EVENTS` and in `EMITTED_TYPES`; the docstring's counts (19→21, 18→20). |
| `src/agentd/tools/registry.py` | registers the two tools. |
| `src/agentd/agent/loop.py` | builds the turn's handle into `tctx.extra["working_memory"]`; discards the orchestrator's scope in `_TurnRecord.__exit__`. |
| `src/agentd/agent/subagents.py` | discards the worker's scope after `worker_finished`, before the checkpoint. |
| `docs/records/effect-classification.md` | one row per new tool, as the audit's test requires. |
| `tests/test_working_memory.py` | **new**, 15 tests. |
| `tests/test_journal_events.py` | `PASS_VOCABULARY` + the two new types. |
| `tests/test_policy.py` | `PRIVATE_SAFE` + the two new tools, with the reason. |

**No migration, in either store.** No Postgres migration, no journal `SCHEMA_V4`, no new column
on `checkpoint`, no new table. `tests/conftest.py` is **unchanged**: the new modules are
additive to no table (nothing to add to `TABLES`) and none of them resolves config at call
time — the working-memory handle is constructed by the turn from the `RunJournal` it already
has and handed to the tool through `ctx.extra`, so no `cfg or get_config()` was added anywhere.

### the three buckets as implemented, and whether storage is shared

```
working    journal    run + agent scope   ends with its task scope    not retrieved
episodic   postgres   user                never                       retrieved by pack()
semantic   postgres   user                never                       retrieved by pack()
```

**Episodic and semantic share the store they already shared** and nothing about them changed:
`episodes` and `facts` in `migrations/0003_memory.sql`, one `pack()`, one rendered block. The
pass file's third *Must not* — do not migrate the physical store if a logical distinction
achieves the same thing — was satisfied by making the distinction the only artifact:
`memory/scopes.py` is importable, its fields are the semantics, and `test_the_three_buckets_
differ_in_store_scope_and_lifetime` fails if two buckets stop differing.

**Working memory is in the run journal, and that is not a second store for memory — it is the
store execution state was already in.** It could not go in Postgres: everything in that schema
is user-scoped, durable and append-only by trigger, and this bucket has to be captured by a
checkpoint and discarded when the run completes. Putting it there would have been the
migration the *Must not* forbids. Putting it in the journal cost no schema change at all: two
event types in a vocabulary that is deliberately extended by slot, folded by a module that
mirrors `journal/worker_results.py`. Three logical buckets, two physical stores, zero DDL.

### isolation mechanism

The key is **`(run_id, scope)`**, and both are equalities in the WHERE clause of
`journal/working_memory.notes_at` — the same shape as 6c's run scoping, and for the same
reason: a scope hashed into a key is a rule nothing can check and nothing can count.

`scope` is a non-empty string, always. A worker's is its `worker_id`; a turn that is not a
worker's writes under the literal `"orchestrator"`. Two things it deliberately is **not**:

* **Not an absent `worker_id`.** `worker_id IS NULL` as the orchestrator's bucket is this
  codebase's recurring bug in its other shape — an absent value becoming a shared grouping key
  that unrelated writers collide in. The literal costs nothing and cannot collide.
* **Not the session id, and this is the load-bearing one.** `run_subagent` builds
  `Session(id=parent_session_id, ...)`: a worker and its caller share one session id, carried
  forward from Pass 6 as a known privacy defect that nobody should fix inside a session doing
  something else. It is not fixed here. If a later pass "simplifies" the scope to the session
  id, **worker A, worker B and the orchestrator become one bucket** — they would read and
  overwrite each other's scratch state, a worker sent to read a stranger's web page would be
  writing into the same scratchpad as the worker reading the user's mail, and the bucket would
  look full and healthy from every angle. Mutation-checked below: it is the change that breaks
  the most tests, which is the point of spending four assertions on it.

A second, weaker guarantee sits on top: the handle is constructed by the turn
(`WorkingMemory.for_turn(rj)`) and carries its scope, so a worker's `AgentLoop` has no argument
with which to name its caller's. That is isolation by construction and is deliberately not the
only thing holding the boundary up — a construction-only guarantee is one refactor away from
being nothing, and nothing in the source would show it had gone. The unscoped view
(`scopes_with_notes`) lives in the journal module, is reachable from no tool, and is what the
tests use to ask "did that run leave anything behind".

Crossing the boundary is unchanged from what the pass file allows: a `WorkerResult`. No fourth
door was added.

### the decision 7a's handoff asked for: working memory does not go through `pack()`

Decided explicitly, recorded here, and pinned by
`test_working_memory_is_not_one_of_the_kinds_retrieval_renders` so that changing it takes
changing the decision rather than adding one dict entry. Three reasons, in order of weight:

1. **The boundary would end up inside a scoring formula.** `pack()` has no run and no worker in
   its signature. Isolation expressed as a filter inside RRF fusion is isolation no test can
   read, and the natural fallback for an unknown scope is "show it".
2. **`_render`'s `KeyError` is silent and total.** A fifth `Item.kind` has to be in `TYPE_PRIOR`,
   `CHANNEL_WEIGHTS`, `SECTION_SHARE` and `_render`'s section map at once; the existing `"raw"`
   kind is in two of the four and in neither of the others, which is the standing proof that
   nothing enforces it. An item that reaches `_render` without all four raises inside
   `agent/loop.py`'s broad `except`, which degrades to "(memory retrieval unavailable)" and
   costs the prompt *every* piece of memory.
3. **Different semantics, one budget.** Scratch state would compete with facts for shares tuned
   for durable knowledge, and would inherit the one failure mode that makes a memory failure
   invisible — for state the agent is actively using.

So the read path is `WorkingMemory.notes()`, by the agent that wrote it, and `pack()` never sees
a working item. Consequence worth stating for 7c: the `contains_secret` gap in 7a's open
question 5 is **not** inherited — nothing unreviewed is rendered into anyone else's prompt,
because a note is rendered only back to the scope that wrote it.

### "captured by checkpoints", on a machine that has never written one

The pass file says working memory is captured by checkpoints. As implemented it is captured **by
position, not by copy**: every function in `journal/working_memory.py` takes a `covers_seq`, so
`notes_at(store, run, scope, checkpoint.covers_seq)` *is* what that scope held at that boundary.
That is exactly the relationship `messages_ref` already has with the messages — a pointer, with
the content left in the journal where re-folding finds it — and it is why no column was added.

Said plainly, as asked: **`[checkpoints] enabled` is false in the shipped config and the live
journal holds 0 checkpoint rows across 52 runs.** A bucket stored *in* the checkpoint would
therefore be a bucket that has never once existed on this machine. Stored as journal events it
works today, with checkpoints off, and a checkpoint taken tomorrow accounts for it without any
further code. `test_a_checkpoint_accounts_for_the_working_memory_of_its_own_position` turns
checkpoints on in a copy of the config and shows the fold at `covers_seq` seeing the note
written before the boundary and not the one written after.

`Checkpoint.pending_promotions` and `Checkpoint.memory_watermark` are still `[]` and `None`.
7b wrote neither; they are 7c's.

### the dead episodic bucket, and what it does to the three-bucket claim

Inherited, not fixed, per the brief: `episodes` is still **0 rows** after 111 `ok`
`post_session` runs. The claim this session can honestly make is therefore *"three declared
buckets, two of which have ever been written"*, and that distinction is carried as data rather
than prose — `Bucket.live` is `False` for episodic, and a test asserts it. The effect on the
work is smaller than it looks: nothing 7b built reads or writes `episodes`, and the separation
the pass wanted is between *working* and the durable pair, which is real either way. The effect
on 7c is larger: the classifier's `episodic` branch will have no live precedent to match, and a
promotion test that asserts on episodic memory is asserting over a table the live system never
fills. 7a's open question 1 stands unchanged and unassigned.

### what deviated from the plan, and why

**1. A scope that holds nothing gets no tombstone.** The plan reads as though a task scope
always ends with a discard. As implemented, `WorkingMemory.discard` returns 0 and writes nothing
when the fold is empty. This is called at the end of *every* turn and *every* worker, and the
alternative puts one "nothing happened" event per turn into Dylan's live feed forever.
`working_memory_discarded` therefore always records a real discard and its `notes` count is
always positive — which also makes it usable as evidence rather than as noise.

**2. Two tools, not one.** A single `working_memory` tool with an `action` argument would have
had to carry one effect class covering its worst argument, making a pure read (`list`) a
ledgered `idempotent_write`. Split, each says the truth: `working_memory_list` is `read`,
`working_memory_note` is `idempotent_write` with `risk="read"` — the note is journaled and
survives a crash within its run, so `read` would lie to the resume path, but nothing leaves the
process, so prompting the user about a scratch note would be a prompt about a danger that is
not there.

**3. A note carries `private` as well as `tainted`.** Not in the plan. Both flags can only be
observed at the moment the note is written; a promotion pass a week later cannot recover them,
and an absent flag reads as a clean one. Adding the field later would have been a vocabulary
change; adding it now is free. The two tools are on `PRIVATE_SAFE` in `tests/test_policy.py`
because they open no egress door — with the reasoning written at the list.

**4. Neither tool is `always_on`.** The other core memory tools are. Making a scratchpad
always-offered spends a line of every prompt on a tool most turns never call, and would perturb
every existing turn's tool set. It is selected like any other tool, and a worker's spec names it
when that role's work is worth keeping notes on. Flagged as reversible in the open questions.

**5. The shipped `RESEARCHER` and `CODER` specs were not given the tools.** Changing the live
roles' tool surface is a behaviour change to delegation, not to memory scoping. The isolation
tests build their own `SubagentSpec`, the way `test_worker_results.py` already does.

### what is now true about the code that was not before

- There is a task-local memory bucket. Before this session the word "working" did not appear in
  the codebase and the only run-scoped, discarded-with-the-run state was the worker result
  cache, which is not memory.
- Two agents in one run have scratch state the other cannot read, enforced in a query rather
  than by construction alone, and the boundary holds across a shared `run_id`, a shared
  `JournalWriter` and a shared `session_id`.
- A completed run leaves no working memory behind, and "leaves none" is a fold over the journal
  rather than a flag: `scopes_with_notes(store, run_id) == {}`.
- The journal vocabulary is 21 types, 20 of them emitted. `tool_progress` is still the only one
  with no producer.
- `memory/scopes.py` exists, so "which bucket is this" is answerable in code.

### green tests are not the evidence; these mutations are

Each mutation was applied to the shipped source, the suite run, and the source restored.

| mutation | tests that failed |
|---|---|
| drop `json_extract(payload,'$.scope') = ?` from `notes_at` | 3 — including the two-concurrent-workers test |
| drop `run_id = ?` from `notes_at` | 1 — `test_working_memory_never_crosses_a_run` |
| `for_turn` returns a constant scope (the "simplify it to one key" change) | 4 |
| remove the discard in `subagents.py` | 2 |
| remove the discard in `loop.py` | 1 |

The first mutation found a real hole on its first run: the two-worker test passed with the scope
filter gone, because both workers were filing under the key `"finding"` and last-write-wins
collapsed a shared bucket into one note that still read as isolation. The fixture now gives each
worker a distinct key, and the test fails as it should. That is the one thing in this session
that a green suite would not have told anyone.

### schemas exactly as implemented

```python
# journal/events.py
"working_memory_noted": {
    "scope":         req(str),   # worker_id, or the literal "orchestrator". Never absent.
    "key":           req(str),
    "text":          req(str),   # in full; a preview cannot be handed back as scratch state
    "chars":         req(int),
    "entry_version": req(int),   # == 1; an entry under other rules is skipped by the fold
    "tainted":       req(bool),  # untrusted text was in context when this was written
    "private":       req(bool),  # the user's own private data was
},
"working_memory_discarded": {
    "scope":  req(str),
    "reason": req(str, enum=("worker_finished", "run_completed", "manual")),
    "notes":  req(int),          # always > 0; an empty scope gets no tombstone
},
```

```python
# memory/scopes.py
Bucket(name, holds, store, scope, ends_with, retrieval: bool, writer, live: bool)
BUCKETS          = {"working": ..., "episodic": ..., "semantic": ...}
RETRIEVAL_KINDS  = {"fact", "claim", "procedure", "episode"}     # no "working"
EXECUTION_STATE  = {"working"}
```

```python
# agent/working_memory.py
Note(key, text, tainted, private)
WorkingMemory(rj: RunJournal, scope: str)
  .for_turn(rj)         -> scope = rj.worker_id or ORCHESTRATOR
  .note(key, text, *, tainted=False, private=False) -> Note   # refuses empty / >4000 chars
  .notes() / .get(key)  -> flushes the writer, then folds
  .discard(reason)      -> int, the number discarded; no event when 0
discard_for_turn(rj, reason)
```

`NOTE_MAX_CHARS = 4000`, enforced as a refusal with the reason in the message. `ENTRY_VERSION = 1`.

### deferred items, and where they went

- **Promotion, classification, `pending_promotions[]`, `memory_watermark`** — 7c, untouched.
  Both checkpoint slots are still `[]` and `None`.
- **The empty-episode bug** — still unassigned. 7a's open question 1, unchanged.
- **The shared `session_id` between a worker and its caller** — deliberately not fixed, per the
  ledger. Nothing built here depends on it, and `journal/working_memory.py`'s docstring says
  what breaks if somebody keys on it later.
- **The `"raw"` kind in `TYPE_PRIOR`/`CHANNEL_WEIGHTS` with no `_render` case** — found again,
  not fixed; it is retrieval's bug, and 7b's answer was to stay out of retrieval entirely.
- **`RESEARCHER`/`CODER` tool surfaces** — not changed. See deviation 5.

### open questions for later passes

**1. Nothing fills working memory unless the model chooses to.** The only producer is a tool the
model calls, and the tool is not `always_on`. If 7c's classifier finds an empty bucket on most
runs, the question is not the classifier — it is whether the runtime should be writing notes
itself (a worker's findings, a tool result worth keeping) and whether the tools should be
offered every turn. Both are one-line changes and both change what every prompt looks like, so
neither was made here.

**2. A note's `private` flag has no consumer yet.** It is recorded and tested and nothing reads
it. 7c is where it has to matter: a note written while the user's mail was in context must not
become a durable fact without the interlock being consulted, and `tainted` must not reach
`IDENTITY_CATEGORIES` at all. The fields are there; the rule is not.

**3. `working_memory_discarded` is buffered, not synchronous.** A SIGKILL between the discard
and the next flush leaves a journal whose fold still shows the notes. That is the safe
direction — a crashed run is not a completed one — but it means "a completed run leaves none
behind" is a statement about runs that ended, not about processes that died. If 7c's resume path
starts asking "did this run finish cleanly", this is one of the signals it must not trust alone.

**4. Nothing prunes the events.** The notes stay in the journal forever, as tombstoned history.
That is deliberate (append-only, auditable) and it is also unbounded growth of a kind the run
journal has not had before — a chatty agent's scratchpad is bigger than its previews. Retention
is Pass 10's; this is the first event type that makes it a size question rather than a tidiness
one.

**5. Two concurrent workers are now tested, and checkpoints still refuse them.** The isolation
test runs two workers at once in one run; `Checkpointer.write` raises `MidWorkerCheckpoint` for
exactly that state. With checkpoints off by default nothing collides today, but the day parallel
delegation ships with checkpoints on, "workers are the unit of atomicity" and "two workers at
once" have to be reconciled. Not new to this session, but this session is the first to exercise
the combination.

### live-data check

Same protocol as 7a: the journal read from a copy taken **with its 4.1 MB `-wal`**, Postgres
through read-only `SELECT`s, the markdown repo not touched at all. The test suite runs against
the `agent_test` database and a `tmp_path` data dir, and no module added this session resolves
config at call time, so there is no new path by which a test could reach the live stores.

- **Journal** (`~/.local/share/agent/journal.db`, via a copy): **1140 events, 52 run ids**,
  24 effect rows, **0 checkpoints** — identical to 7a's start and end figures.
  `working_memory%` events in the live journal: **0**.
- **Postgres**: facts **47**, candidate_memories **116**, episodes **0**, sessions **111** —
  all identical to 7a. `raw_events` is **1475** against 7a's 1474; the newest rows are
  `brightspace.assignment` and `gmail-nyu.message` written by the daemon's connectors during
  this session, not by anything run here.
- **Markdown repo**: not opened.
