# Pass 7 — Memory Scopes & Transactional Promotion — outcome

Sessions completed: **7a**. 7b and 7c are untouched and append to this file.

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
