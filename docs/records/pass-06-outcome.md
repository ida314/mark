# Pass 6 — Delegation Interface & Standardized Results — outcome

Sessions completed: **6a**, **6b**. 6c (result cache) is untouched.

---

## Session 6a — The delegation signature

### what shipped

| file | what it is | lines |
|---|---|---|
| `src/agentd/agent/delegation.py` | `TaskSpec`, the normalization, the role lookup, `delegate()` | 289 |
| `src/agentd/agent/subagents.py` | `run_subagent` takes a `TaskSpec`; `SubagentSpec.expected_output`; the brief | +45 −7 |
| `src/agentd/tools/builtin_delegate.py` | the tool is a door onto `delegate()`; two new arguments | +50 −23 |
| `src/agentd/journal/events.py` | `worker_created.task_digest` | +5 |
| `tests/test_delegation.py` | 21 tests | 324 |
| `tests/conftest.py` | `agentd.agent.subagents` in the `get_config` monkeypatch list | +6 −1 |
| six existing test modules | call sites now build a `TaskSpec`; three hand-built payloads gained a digest | +21 −11 |

Suite **861 → 882 passing**. `.venv/bin/ruff check src tests scripts` clean. No tool was
added, removed or re-classified; `delegate` is still `unsafe_write`, still `always_on`, and
still the only delegation tool. No migration, no table, no config flag, no prompt file
changed. Nothing here writes memory, and no worker result is cached yet.

### what deviated from the plan, and why

**1. The spec is normalized in `TaskSpec.__post_init__`, not by a builder.** The pass asks
for a spec "stable enough to serve as a cache key". A builder function that normalizes leaves
a second way to make the object — the constructor — and the two would be free to disagree
about the key a result is stored under, which is the one disagreement 6c cannot detect from
either side. Normalizing in `__post_init__` means there is no such thing as an un-normalized
spec: a caller who constructs one by hand gets what `delegate()` would have built.

**2. Normalization is deliberately shallow, and the asymmetry is the reason.** Whitespace,
Unicode composition (NFC), ordering and duplicates are erased; wording, case, digits and
punctuation are not. The two failure directions are not equally expensive — a missed cache
hit costs a worker; a false cache hit hands back some other piece of work's answer as this
one's. So every rule that erases something had to be defensible on its own, and "these two
sentences mean the same thing" is not a judgement this module makes.

**3. `relevant_context` and `constraints` are sets: deduplicated and sorted.** Two
delegations that list the same constraints in a different order are the same delegation. The
cost is stated at the definition: a caller who means "first A, then B" must say so in `task`,
because these fields are things that are *true of* the delegation, not steps in it.

The sorting is not cosmetic and the first version of its test did not bite. Deduplicating
through a `set` and returning it in iteration order passes every in-process assertion and
produces **a different digest in the process that reads the cache back**, because
`PYTHONHASHSEED` randomises `str` hashing. `test_the_same_delegation_hashes_the_same_in_a_new_process`
runs three interpreters with three seeds; it is the only test here that catches it.

**4. An omitted `expected_output` is filled from the role's own reporting contract, and the
filled value is written into the spec.** `SubagentSpec` gained `expected_output` for
researcher and coder, taken from the architecture's own examples. The fallback happens at
construction rather than at render time on purpose: a default applied later would let two
specs that hash identically hand two different briefs to two workers. It is a declared
per-role constant that is visible in the canonical form, in the digest and in the archive —
not the house bug, which is a *failed* extraction quietly becoming a plausible value.

**5. `spec_version` is inside the canonical form.** The normalization rules are the key. When
they change, keys computed under the old rules must not collide with keys computed under the
new, and a stored spec from another version is refused by `from_dict` rather than read as if
the rules had not moved.

**6. The tool's wire argument stays `agent`, not `durable_role`.**
`config/policy.default.yaml` matches `private-data-no-outward-delegation` on
`args.agent.in [researcher, coder]` — the rule that stops a turn holding the user's mail from
delegating its way around the interlock with a fresh session and its own web tools. A rename
here that did not land in the policy file in the same edit would open that door and leave the
suite green. The mismatch between the wire name and the spec field is recorded in the tool's
docstring instead.

**7. `agent="memory"` is left exactly as it was, concatenation included.** It builds no
worker, journals no `worker_created` and is not cached under a task spec; it packs retrieval
in this process. Its query is still `task` plus the `context` argument appended, because that
string is what gets embedded and rewriting it would change what comes back. It ignores
`constraints` and `expected_output`.

**8. `worker_created` gained a required `task_digest`.** Not asked for by 6a, and it is the
precedent the session ledger flagged as unresolved for this pass (5a added required fields to
an existing type). Taken because the journal is the only durable record of a delegation when
checkpoints are off — which is the shipped default — and `task_preview` is 200 characters and
must never be re-delegated from. Checked rather than assumed: the live journal holds four
pre-6a `worker_created` rows with no such key and all 50 runs in it still fold (below).

**9. The pass's "subagents complete an entire coherent workflow" is a property of the
executor, so no prompt was changed.** `run_subagent` runs the worker's whole loop and returns
a report; there is no mechanism by which a worker hands control back after a tool call. The
sentence is recorded at `delegate()` rather than added to a system prompt.

### what is now true about the code that was not before

- **A worker cannot be started from an ad-hoc string.** `run_subagent` raises `TypeError` on
  anything that is not a `TaskSpec`, rather than coercing one — a coercion would be a second
  normalization path.
- **An unknown durable role is refused at spec construction**, by the one lookup that knows
  what roles exist, and the exception names the ones that do. There is no fallback worker,
  because a spec naming a role that does not exist would still hash and 6c would cache a
  result under a key claiming a role the run never had.
- **The same delegation has the same identity in the two places it can be made.**
  `test_the_delegate_tool_and_a_direct_delegation_produce_the_same_spec` drives the model's
  door (`builtin_delegate`) and the runtime's door (`delegation.delegate`) with the same
  words spelled differently and asserts one digest in the journal for both.
- **What a worker was asked to do is identifiable in the journal**, not merely previewable:
  `worker_created.task_digest` is the whole spec as a key, next to a preview that truncates
  every real delegation this machine has ever made (295, 513, 644 and 1768 characters).
- **The whole brief is in the archive.** `subagent_message` carries the rendered brief —
  task, context, constraints, what to return — so a re-delegation can be built from what was
  really sent. `actions.input` carries `{"task": brief, "task_spec": {...}}`.
- **A delegation is the same delegation whichever step of whichever turn made it.** Nothing
  run-scoped is in the spec: `run_id`, `step_id`, the approver and the parent ids are
  plumbing and are kept out of the key, which is what makes an in-run duplicate a cache hit
  in 6c rather than a near-miss.

**Mutation-checked rather than trusted for being green.** Eleven mutations, ten caught at
once and one survivor, which is the useful one:

- drop NFC → caught (1 failure).
- lowercase in `normalize_text` → 3 failures.
- an unknown role falls back to `researcher` → 2 failures.
- **return the deduplicated items in set order rather than sorted → nothing failed**, because
  set iteration is stable inside one interpreter. Closed by the three-interpreter test above;
  the mutation now fails 2.
- drop `spec_version` from the canonical form → caught.
- allow a blank task → caught.
- apply the role's `expected_output` at render time instead of in the spec → caught.
- coerce a `str` task in `run_subagent` instead of raising → caught.
- write `task_digest: ""` into the journal → caught.

**Live-data check (the house rule: read the real rows).** A full copy of
`~/.local/share/agent/journal.db` **with its `-wal` and `-shm`** — the first check in these
records to take the WAL, which is why it sees rows the earlier ones could not:

1. **50 runs, 50 fold.** Including the four pre-6a `worker_created` rows, which have no
   `task_digest`. `validate_payload` is on the write path only, so a required field added
   today does not break a row written yesterday.
2. **`worker_created.role` is `"subagent"` in every real row and `name` is the durable role.**
   So `checkpoints.WorkerRef.role` — which reads `payload["role"]` — says `"subagent"` for
   every open worker a checkpoint has ever described. Not touched here (see open question 2).
3. **Every real delegation on this machine is longer than the preview.** 295, 513, 644 and
   1768 characters against a 200-character `task_preview`.

### the live journal was written to by accident, and the seven rows are still there

Reported rather than cleaned up. Writing an outcome record is not permission to rewrite the
journal, and these are append-only rows in a run nothing points at.

`run_subagent` does `cfg = cfg or get_config()`, and `agentd.agent.subagents` was **not** in
`tests/conftest.py`'s `get_config` monkeypatch list. Every existing test passes `cfg=`
explicitly, so it had never mattered; the `delegate` *tool* cannot — it has a `ToolContext`
and no `Config` — so the first test to drive the tool path ran a worker against the real
config and wrote **7 events under `run_id = "run-tool"`** into
`~/.local/share/agent/journal.db` (1090 → 1097 rows).

What was and was not affected: no `effect` row, no `checkpoint` row, no other run's events,
and nothing in Postgres (the pool is patched, so the archive and `actions` writes went to
`agent_test`). The run folds as `no_orchestrator` with 0 orchestrator messages and 0 orphans —
all seven events carry a `worker_id` — so it is inert to `resume`, `fork` and every checkpoint
path. `agentd.agent.subagents` is now in the monkeypatch list, with the reason written at the
line, and a re-run added no further rows.

### schemas exactly as implemented

The delegation signature (`agent/delegation.py`):

```python
async def delegate(
    durable_role: str,
    task: str,
    *,
    relevant_context: Sequence[str] | str = (),
    constraints: Sequence[str] | str = (),
    expected_output: str = "",
    # plumbing of the run it is made inside; none of it is part of the spec or the key
    parent_session_id, parent_turn_id, parent_autonomy, approver,
    registry=None, cfg=None, parent_action_id=None, parent_run_id=None,
    parent_step_id=None, journal=None, provider=None,
) -> SubagentResult
```

```
TaskSpec (frozen; normalized in __post_init__)
  durable_role      str              must be a key of subagents.SPECS, else UnknownRole
  task              str              non-empty after normalization, else EmptyTask
  relevant_context  tuple[str, ...]  deduplicated, sorted; a bare str is one item
  constraints       tuple[str, ...]  deduplicated, sorted
  expected_output   str              the caller's, or the role's, written in at construction
  .as_dict() / .from_dict(d)         includes "spec_version"; a foreign version is refused
  .canonical()      str              json, sort_keys, separators=(",",":"), ensure_ascii=False
  .digest           str              sha256 hex of canonical()
  .brief            str              the text the worker is handed, derived from the spec only

SPEC_VERSION = 1
DelegationError <- UnknownRole, EmptyTask
normalize_text(str) -> str           NFC, \r\n -> \n, runs of spaces, per-line strip,
                                     3+ blank lines -> 1, outer strip. Nothing else.
role_names() -> ("coder", "researcher")
role_spec(name) -> SubagentSpec      raises UnknownRole, never a default role
```

The brief, rendered (blocks joined by a blank line, absent blocks omitted):

```
<task>

Context you have been given (you cannot see the conversation this came from):
- <ref>

Constraints:
- <constraint>

Report back:
<expected_output>
```

`SubagentSpec` gained one field, and the two roles' contracts are:

```
researcher  "What you found, the source url or file path for every claim, the caveats that
             matter, and what you looked for and could not find."
coder       "The files you changed, a summary of the change, and what you ran to check it
             with its real result."
```

The journal, one field added to an existing type (no migration; the file stays at schema v3):

```
worker_created  ... task_chars: int; task_preview: str
                task_digest: str    sha256 of the canonical task spec   (6a, required)
                parent_step_id: str|null
```

The tool, as the model now sees it (`agent` and `task` required, as before):

```
delegate(agent ∈ {researcher, coder, memory}, task,
         context?: string, constraints?: string[], expected_output?: string)
```

### deferred items, and where they went

- **The result schema — 6b.** `SubagentResult` is untouched: `status ∈ {ok, partial, failed,
  budget_exhausted}`, `summary`, `artifacts`, `citations`, `candidate_memories`, `tainted`.
  The pass's `{completed, blocked, uncertain} / answer / evidence[] / actions_taken[] /
  followups[]` is 6b's, and 6b will have to reconcile its status words with 4c's
  `blocked`/`uncertain` vocabulary, which means something else.
- **`result_key`, persistence and `worker_results[]` — 6c.** Nothing caches anything.
  `TaskSpec.digest` is the intended input: `result_key = hash(durable_role, task_spec,
  relevant_context_refs)` hashes three things that are all inside the canonical form, so it
  should be that digest or a hash of it with the run id, and **not** a second independent
  derivation.
- **Parallel workers — not this session's.** Delegation is still awaited inside the step that
  makes it, so 4a's open question 1 (`open_workers[]` has never been non-empty) stands.
- **`delegate(memory)` — left alone.** It is the one branch of the tool that is not a
  delegation, and Pass 8 owns the tool surface.

### open questions for later passes

**1. Prose context is a weak cache key, and the tool's `context` argument is prose.** The
normalization removes incidental whitespace, and nothing can remove the model rephrasing its
own background paragraph between two delegations that mean the same thing. The architecture
says `relevant_context_refs`; this session accepted strings because the existing wire
argument is a string and the local 27B is already unreliable with schemas. If 6c measures a
poor hit rate, the first thing to try is refs (urls, paths, fact ids) rather than more
normalization.

**2. `WorkerRef.role` is `"subagent"` for every worker ever checkpointed.**
`checkpoints._open_workers` reads `payload["role"]`, which is the *LLM* role; the durable role
is `payload["name"]`. Confirmed on all four real rows in the live journal. Nothing was changed
here because reading a key that pre-6a rows do not have would crash a fold, so **6c must read
`name` (or `payload.get("durable_role", payload["role"])` if it adds one)**, and a
re-delegation built from `WorkerRef` as it stands today would ask "subagent" to do the work.

**3. A required field was added to an existing event type, which is the ledger's open
question from 5a and is now answered twice the same way.** Old rows do not have it, nothing
re-validates on read, all 50 live runs still fold. Whether that stays acceptable when Pass 7
does it to `memory_watermark` is Dylan's, not this session's.

**4. `run_subagent`'s `cfg` fallback reaches the real config, and only a conftest entry stops
a test from using it.** The accident above is fixed for the suite; the underlying shape —
a module that resolves config at call time, reachable from a tool that has none — is
unchanged, and it is the same shape as every other name on that monkeypatch list.

**5. Nothing yet counts a delegation that was made twice.** The digest makes it observable
for the first time (two `worker_created` rows with one digest in one run), and 6c is what
turns that from an observation into a saved worker.

**6. Still open, untouched by 6a:** effect events carry no `worker_id` (3b #4), so a
reconciliation still cannot say whether the orchestrator or a worker made a call (4b #6);
`open_workers[]` has never been non-empty (4a #1); checkpoints are off in the shipped config;
token accounting is still broken upstream.


---

## Session 6b — The result schema, validated on return

### what shipped

| file | what it is | lines |
|---|---|---|
| `src/agentd/agent/results.py` | `WorkerReport`, `WorkerResult`, `validate_report`, `unreadable_report` | 237 |
| `src/agentd/agent/subagents.py` | the final report is decoded, validated and reconciled; new `worker_finished` payload | +60 −56 |
| `src/agentd/tools/builtin_delegate.py` | the payload is `for_orchestrator(debug=...)`; `ok` is `completed` only | +15 −9 |
| `src/agentd/journal/events.py` | `WORKER_STATUSES`; `worker_finished` re-keyed | +23 −6 |
| `src/agentd/db/repo_archive.py` | `recent_messages` / `recent_message_sizes` exclude `subagent:*` actors | +15 |
| `src/agentd/config.py`, `config/default.toml` | `[delegation] debug_transcripts` | +21 |
| `src/agentd/agent/delegation.py`, `agent/loop.py` | return type and one stale comment | +11 −2 |
| `tests/test_worker_results.py` | 17 tests (19 cases) | 315 |
| six existing test modules | `WorkerReport` in place of `SubagentResult`; re-keyed journal payloads | +64 −56 |

Suite **882 → 900 passing** (one test moved into the new module and was rewritten there).
`.venv/bin/ruff check src tests scripts` clean. No tool added, removed or re-classified;
`delegate` is still `unsafe_write`, `always_on`, and the only delegation tool. No migration,
no table, no new archive kind. Nothing here writes memory, and no result is cached yet.

**The live journal was not written to.** 1097 rows before and after, 50 runs, and the newest
row is still 6a's `run-tool` accident from 02:59 UTC. The `agentd.agent.subagents` conftest
entry 6a added is what makes that true for the tool-driven tests in this session.

### what deviated from the plan, and why

**1. Two objects, not one: `WorkerReport` (pydantic) and `WorkerResult` (frozen dataclass).**
The pass names one schema. The split is the difference between what a worker *claims* and
what the runtime *concluded*, and it is what makes "validate on return" mean something: the
only way to get a `WorkerResult` is through `validate_report` or `unreadable_report`, and
nothing decodes into one. `WorkerResult.__post_init__` refuses a status outside the three.

**2. Reconciliation is after the decode, and there is no cross-field validator.** A
`model_validator` on `WorkerReport` would raise inside `complete_json`, which gets exactly one
repair attempt and then raises `LLMError` — so an honest "I ran out of steps" report would be
destroyed by the schema rather than recorded as the unfinished work it is. The two rules that
can move a status live in `validate_report`, and both of them only ever move it *towards*
`uncertain`. Nothing there can turn a worker's bad news into good news
(`test_the_runtime_never_upgrades_a_worker_that_reported_honestly`).

**3. An unreadable report is `uncertain`, never `blocked` — this is the reconciliation with
4c the 6a record asked for.** 4c fixed the words: `blocked` means the work did not happen and
`paths_for(BLOCKED)` puts `retry` first; `uncertain` means nobody can say what happened and
never offers retry for an `unsafe_write`. A worker whose report came back unreadable may
already have run `fs_write` and the shell, so `blocked` would authorise re-running whatever it
did. Same argument forces `budget_exhausted → uncertain` *regardless of what the worker said*,
including when the worker said `blocked`. The old vocabulary (`ok`, `partial`, `failed`,
`budget_exhausted`) is gone rather than mapped: `partial` had no reading in the new three that
was not already `completed`-with-followups or `uncertain`.

**4. The failure path no longer borrows the transcript for an answer.** The shipped code was
`summary=f"Sub-agent could not produce a structured report: {exc}. Raw output: {text[:1000]}"`
— the house bug and a transcript leak in one line: it reads to the orchestrator as a report,
in the one case where nothing was reported. It is now a sentence this runtime wrote, with
`report_valid=False` beside it. The decode error is kept (on the result and in the archive)
and is **not** in what a model is shown, because a `ValidationError` string quotes the
model's own malformed output back.

**5. `worker_finished` was re-keyed, not extended.** `artifacts` and `citations` are not
fields of a result any more, so counting them was not an option; the payload now carries
`evidence`, `actions_taken`, `followups`, `answer_chars`, `answer_preview` and a required
`report_valid`. The status enum changed with it. Pre-6b rows carry the old four words and the
old keys; `validate_payload` is write-path only, nothing in `src` folds either field
(`journal/render.py` prints `status` and `name`), and all 50 live runs still fold.

**6. A leak this session found and closed, which the pass did not ask for.** A worker runs
inside its caller's *session*, so `agent/loop.py` archives its final prose as
`kind='assistant_message', actor='subagent:<role>'` on that session — and
`repo_archive.recent_messages` selected by `session_id` and `kind` only. The orchestrator's
**next turn rebuilt its history out of rows a worker wrote**: "worker transcripts never enter
orchestrator context" broken in the place nobody would look, with no tool call involved.
`AND actor NOT LIKE 'subagent:%'` now excludes them, and `recent_message_sizes` (the handoff
watermark's size query) gets the same clause so the two queries measure the same set of rows.
The rows stay in the archive; what changed is that nothing replays them into a prompt.

**7. No `subagent_transcript` archive kind, after writing one and taking it out again.** The
transcript is already retained by the runtime — the worker's own `assistant_step` rows (one
per step) and its joined `assistant_message`, both under `subagent:<role>`. A third copy would
have entered every `post_session` consolidation prompt, which reads *all* kinds through
`events_for_session`, and paid tokens to extract facts from the same prose twice.

**8. One new config section rather than a flag on `[agent]`.** `[delegation]
debug_transcripts = false`. The architecture's rule names an "explicit debug mode", so the
mode has to exist and has to do something; `test_debug_mode_is_the_one_way_a_transcript_reaches_the_delegating_model`
fails if the flag is ignored. It switches who is *shown* the transcript, never whether it is
kept.

**9. The `delegate` tool's description changed, and `FINAL_INSTRUCTION` was rewritten field
by field.** The tool now advertises what comes back ("a status (completed, blocked or
uncertain), an answer, its evidence and what was done"); the final prompt names each field and
says an empty answer is not allowed. This is the one prompt whose output is rejected rather
than repaired, so telling the model the shape is the cheapest thing available. Pass 5's eval
baselines were taken against the old wording.

### what is now true about the code that was not before

- **A worker that returns prose is a failure with a name.** `status="uncertain"`,
  `report_valid=False`, no evidence, no actions — and the caller can tell without reading the
  answer. Both exception shapes land there: `LLMError` after the provider's repair attempt,
  and a `ValidationError` from a provider that validates locally.
- **A report that decoded but says nothing is also a failure.** `answer` is required by the
  schema *and* checked after the decode, because a model can satisfy a required string with
  `"   "`. That is the only substantive check; nothing here fails a `completed` for having no
  evidence (see open question 2).
- **The journal says whether the worker reported at all.** `status` cannot carry it: an
  unreadable report is `uncertain` and so is a worker that honestly could not vouch for its
  own work. `report_valid` is the field that separates them, and it is required.
- **`blocked` and `uncertain` are both `ok=False` at the tool boundary.** The executor writes
  that as `effect.failed(...)`, and `agent/observations.py` reads a failed effect as
  *uncertain* rather than as blocked (3b's ruling, restated at the 4/5 boundary) — so a
  delegation that may have changed files is never offered for a silent retry.
- **One door renders a result for a model.** `WorkerResult.for_orchestrator(debug=False)`.
  The transcript and the decode error ride on the result for the archive and the debug path
  without being one `json.dumps` away from a prompt.
- **The orchestrator's history contains no worker prose**, by the query rather than by
  convention.

**Mutation-checked rather than trusted for being green.** Ten mutations, ten caught:

- drop the `subagent:` clause from `recent_messages` → 1 failure.
- the unreadable answer borrows the transcript again → 2.
- accept an empty answer → 1.
- ignore `budget_exhausted` → 2.
- `for_orchestrator` always includes the transcript → 2.
- `debug=False` hard-coded in the tool → 1.
- `blocked` counts as a successful tool call → 1.
- an unreadable report reads as `blocked` → 5.
- `report_valid=True` on an unreadable report → 4.
- (and the suite was run clean between each.)

**Live-data check (the house rule: read the real rows).** This machine has **six** real
delegations, in Postgres rather than in the journal: `raw_events` holds 6 `subagent_message`
and 6 `subagent_result`, every one of them `payload->>'status' = 'ok'`, and `actions` holds 6
rows with `kind='subagent'`, all `ok`, all with a non-null `output->'summary'`. So the old
vocabulary is in the archive on six rows, nothing re-reads them, and no row anywhere has ever
carried a null where a result field belonged.

### schemas exactly as implemented

```
WorkerReport (pydantic; what the model is asked for, sent to complete_json)
  status            Literal["completed","blocked","uncertain"]   required, no default
  answer            str                                          required, no default
  evidence          list[str] = []
  actions_taken     list[str] = []
  followups         list[str] = []
  candidate_memories list[CandidateIn] = []        (moved here from subagents.py)

WorkerResult (frozen dataclass; what the runtime concluded)
  status            str        one of STATUSES, else ValueError at construction
  answer            str
  evidence          tuple[str, ...] = ()
  actions_taken     tuple[str, ...] = ()
  followups         tuple[str, ...] = ()
  candidate_memories tuple[CandidateIn, ...] = ()
  tainted           bool = False
  report_valid      bool = True
  notes             tuple[str, ...] = ()    runtime-authored sentences only
  report_error      str = ""                runtime-only: quotes the model's bad output
  transcript        str = ""                runtime-only
  .for_orchestrator(debug=False) -> dict
      always: status, answer, evidence, actions_taken, followups, + notes if any
      debug:  + report_valid, report_error, transcript

STATUSES = journal.events.WORKER_STATUSES = ("completed", "blocked", "uncertain")

validate_report(report, *, budget_exhausted=False, tainted=False, transcript="")
  - answer stripped; list entries stripped and blanks dropped
  - empty answer      -> uncertain, report_valid=False, NOTE_NO_ANSWER
  - budget_exhausted  -> uncertain (from any other status), NOTE_BUDGET
  - nothing else. No rule promotes a status.

unreadable_report(error, *, tainted=False, transcript="", budget_exhausted=False)
  -> uncertain, report_valid=False, a runtime-written answer, NOTE_UNREADABLE
```

The journal, one existing type re-keyed (no migration; the file stays at schema v3):

```
worker_finished  worker_id, name
                 status: enum WORKER_STATUSES          (was ok|partial|failed|budget_exhausted)
                 answer_chars: int                     (was summary_chars)
                 answer_preview: str|absent            (was summary_preview)
                 report_valid: bool                    (6b, required)
                 tainted: bool
                 evidence: int, actions_taken: int, followups: int   (were artifacts, citations)
                 candidates: int, tokens: int, duration_ms: int
```

Config, and the archive query:

```
[delegation]
debug_transcripts = false     # on: the transcript rides back with the result

repo_archive.recent_messages / recent_message_sizes
  ... AND actor NOT LIKE 'subagent:%'
```

The tool, as the model now sees it (arguments unchanged from 6a):

```
delegate(agent ∈ {researcher, coder, memory}, task, context?, constraints?, expected_output?)
  -> {"status", "answer", "evidence", "actions_taken", "followups"[, "notes"]}
     ok = (status == "completed")
```

### deferred items, and where they went

- **`result_key`, persistence and `worker_results[]` — 6c**, unchanged from 6a's note. What
  6c caches is now a `WorkerResult`; it should cache `completed` only, because an
  `uncertain` result is precisely the case where re-running may be the right answer and
  serving it from a cache decides that question silently.
- **`agent="memory"` — still untouched.** It returns retrieval text and not a result schema;
  it builds no worker. Pass 8 owns the tool surface.
- **Whether a `completed` must cite anything — not enforced.** See open question 2.
- **Parallel workers — still not this session's.** `open_workers[]` has never been non-empty.

### open questions for later passes

**1. A worker is shown the caller's conversation, and its brief says it cannot see it.**
Probed, not inferred: `run_subagent` builds `Session(id=parent_session_id, ...)` and
`loop.run_turn` calls `history_messages(session.id)`, so a worker's prompt contains the
user's earlier messages in that session. A scratch probe (a `user_message` containing "MY
SECRET PLAN", then a delegation) found it in the worker's first prompt — while `TaskSpec.brief`
tells the worker "you cannot see the conversation this came from". 6b closed the leak in the
other direction (worker prose → orchestrator history) because that is the rule the pass
states. This one is the private-data interlock's business as much as delegation's, and it is
bigger than a query clause: giving a worker its own session id changes what
`repo_archive.events_for_session` groups and what consolidation reads.

**2. Nothing requires a `completed` result to have evidence.** The researcher's contract says
"the source url or file path for every claim", and the schema now has a field for it, but a
`completed` with `evidence=[]` is accepted. Mechanically enforcing it would convert the local
27B's most common sloppiness into `uncertain` on most real runs, which is how a rule gets
ignored. The counts are in `worker_finished.evidence` now, so the first thing to do is
measure — how many real `completed` results cite nothing — rather than legislate.

**3. `partial` had no successor and one test changed meaning.**
`test_a_workers_tool_failure_lands_in_its_transcript_where_it_happened` scripted
`status="partial"` and now scripts `blocked`. Nothing in the runtime produced `partial` on
its own; it was only ever a word the model could choose. If a real worker wants to say "I did
some of it", the shape available is `completed` with `followups`.

**4. `FINAL_INSTRUCTION` and the tool description changed, so the Pass 5 eval baselines are
not comparable across this session** for anything that measures delegation. The v2 baseline
was taken against the old wording.

**5. The transcript is a field on a dataclass that is returned to callers.** `for_orchestrator`
is the only renderer today and the tests pin it, but nothing *structurally* stops a future
caller from formatting `result.transcript` into a prompt — the guard is a convention plus two
tests, not a type. If 6c starts persisting results, the question of whether the transcript is
persisted with them has to be answered before it is answered by accident.

**6. Still open, untouched by 6b:** `WorkerRef.role` is `"subagent"` for every checkpointed
worker (6a #2); effect events carry no `worker_id` (3b #4); `open_workers[]` has never been
non-empty (4a #1); checkpoints are off in the shipped config; token accounting is still
broken upstream.


---

## Session 6c — `result_key`, the run-scoped cache, and `worker_results[]`

### what shipped

| file | what it is | lines |
|---|---|---|
| `src/agentd/agent/result_cache.py` | `result_key`, `entry_for`, `result_from_entry`, `remember`, `lookup`, `serve` | 181 |
| `src/agentd/journal/worker_results.py` | the fold: `cached_results_at`, `cached_result`, `ENTRY_VERSION` | 90 |
| `src/agentd/journal/events.py` | `worker_result_cached`, `worker_result_reused` | +60 −2 |
| `src/agentd/journal/checkpoints.py` | `worker_results[]` filled from the fold at `covers_seq` | +30 −6 |
| `src/agentd/journal/writer.py` | `worker_result_cached` is synchronous | +9 −1 |
| `src/agentd/journal/render.py`, `journal/__init__.py` | one feed line; the new names exported | +20 |
| `src/agentd/agent/subagents.py` | the cache is consulted before a worker starts, written after it finishes | +18 |
| `src/agentd/agent/results.py` | `WorkerResult.reused_from` | +20 −6 |
| `tests/test_result_cache.py` | 18 tests | 460 |
| `tests/test_checkpoints.py`, `tests/test_journal_events.py` | the new event in three expected sequences; the vocabulary | +30 −10 |

Suite **900 → 920 passing** (18 new, and the feed's per-type parametrization picks up the
two new types). `.venv/bin/ruff check src tests scripts` clean. No tool added, removed or
re-classified. No migration, no table, no Postgres change, no config flag: the cache is not
behind one, because a result kept only when `[checkpoints] enabled` was on would be a result
this machine has never once kept (0 checkpoint rows, below).

**The live journal was not written to.** 1097 rows before and after, 50 runs, and the newest
row is still 6a's `run-tool` accident.

### what deviated from the plan, and why

**1. The cache lives in the journal, not in the checkpoint.** The pass says "persist
completed results keyed by `result_key`. Populate `worker_results[]` in the checkpoint" and
does not say where the persistence is. Putting it in the checkpoint is the reading that
fails on the shipped config: `[checkpoints] enabled` is false, and the live journal holds
**zero checkpoint rows across all 50 runs**, so a checkpoint-only cache would have been
inert on the one machine this runtime runs on. So `worker_result_cached` is a journal event
and `state = fold(reduce, journal, initial)` gives the cache for free; the checkpoint's
`worker_results[]` is a copy of that fold at `covers_seq` - an acceleration structure that
may lag it and may never disagree with it, which is the same relationship `messages_ref` has
with the messages.

**2. Two event types were added to a vocabulary pass 2 fixed**, which
`test_the_vocabulary_is_exactly_the_one_the_pass_specified` exists to make somebody decide
rather than drift into. Both are named in `PASS_VOCABULARY` with the session that added them.
`worker_result_reused` is a second type rather than a flag because a cache hit must leave no
`worker_created` behind it - the exit criterion is read off the *absence* of that event - and
an absence is not a record: without this event the journal would say the second delegation
was never made, and no reuse rate could be counted from it at all.

**3. `result_key(spec)` is `spec.digest` itself, not a hash of it with anything.**
`hash(durable_role, task_spec, relevant_context_refs)` hashes three things that are all
inside `TaskSpec.canonical()`, so any further hashing would be a second derivation over the
same inputs, free to drift from the `task_digest` 6a already writes into `worker_created`.
As it stands the worker that earned a cached result is one join away, on equal strings.

**4. Run scope is in the query, not in the key.** Hashing `run_id` into the key was the
other option 6a left open. It was refused: it would make cross-run reuse impossible *and*
unobservable - the pass's *Must not* buried inside a digest where no test can check that it
still holds, and where nothing could ever count how often two runs did identical work.
`WHERE run_id = ?` in `worker_results.py` is a line a test reads
(`test_a_result_cached_in_another_run_is_never_reused`), and a fork inherits nothing for the
same reason its parent's effect ledger does not travel.

**5. `worker_result_cached` is synchronous, and it is the only event type carrying a body
rather than a preview.** Synchronous because a cache entry still in a buffer is exactly the
entry a crash takes, and the crash is the case the cache exists for; the flush carries the
worker's whole `worker_created ... worker_finished` bracket with it, which is one fsync for
something that has just cost minutes. A body rather than a preview because a 200-character
preview cannot be *served back* as a result - this is the durable artifact, not a
description of one - and `FINAL_INSTRUCTION` already bounds an answer at 200 words.

**6. The transcript is not persisted. 6b's open question 5, answered before it was answered
by accident.** The worker's prose is already in the archive under `subagent:<role>`; a copy
in the journal would be a second one, and every copy is another place a transcript can reach
a prompt from. A reused result therefore has `transcript=""`, which on its own reads as a
worker with nothing to say - so `WorkerResult.reused_from` was added, carrying the worker id
that earned it, rendered only under `for_orchestrator(debug=True)`.

**7. Candidate memories are not persisted either, and a hit proposes nothing.** The worker
that earned the result already proposed them and the review gate already holds them.
Re-inserting per hit is the codebase's other recurring bug - the duplicate - and it would
let one delegation, repeated, manufacture the appearance of independent corroboration.

**8. A cache hit writes no `actions` row of its own.** `repo_ops.write_action(kind="subagent")`
describes a worker that ran, and none did. The tool call itself is still recorded by the
executor, so the ops record of the delegation exists; what does not exist is a second
`subagent` action claiming a worker.

**9. The entry is versioned separately from the key, and a foreign version is a miss.**
`ENTRY_VERSION` sits in the payload, not in `result_key`: the key is the identity of the
*work* and must not move because the record of the work changed shape. An entry the fold
cannot read is skipped - the work is done again, which is what this runtime did before the
cache existed - rather than read as fields that may not mean what they say.

### what is now true about the code that was not before

- **A resumed run does not pay twice for a finished worker.** `run_subagent` consults the
  cache before it journals anything, so a hit leaves no half-started worker to confuse the
  measurement, and returns the same `WorkerResult` the first delegation got.
- **Only `completed` is ever served.** `remember` refuses `blocked` (nothing happened, so
  nothing was earned) and `uncertain` (re-running may be exactly right, and a cache would
  decide that silently and for ever). Both refusals are tests, not comments.
- **A reused result is still untrusted if the work that earned it was.** `tainted` is stored
  and restored; a result earned while the turn held the user's private data does not come
  back trusted the second time.
- **`worker_results[]` is no longer inert.** It is the fold at `covers_seq`, and
  `test_the_checkpoint_records_what_the_journal_has_cached` asserts the checkpoint equals
  the fold rather than merely having something in it.
- **The cache and the checkpoint cannot disagree**, because the checkpointer does not count
  anything: it calls the same `cached_results_at` a delegation is served from.
- **A delegation that was made twice is now visible and countable**, which 6a listed as the
  thing only the digest had made observable.

**Mutation-checked rather than trusted for being green.** Eleven mutations, eleven caught:

- cache every status, not just `completed` → 11 failures.
- ignore `entry_version` on read → 1.
- cache the transcript in the entry → 15 (the journal's unknown-key rule fires first).
- drop `run_id` from the fold's query → 2, both of them the *Must not* tests.
- `worker_results` back to `[]` in the snapshot → 2.
- `worker_result_cached` out of `SYNC_TYPES` → 1.
- drop `tainted` on the way out of an entry → 2.
- never call `remember` → 9.
- `remember` *after* `checkpoint_at` instead of before → 2 (the checkpoint lags by a worker).
- never emit `worker_result_reused` → 3.
- last entry wins instead of first → 1.

### the measured reuse rate

`test_a_resumed_run_does_not_re_run_the_workers_it_finished` is the pass's exit criterion as
a measurement. Three workers complete in one run; the process dies with no `agent_finished`
and no checkpoint of its own; a second `JournalWriter` opens the same file, `resume.resume()`
folds the run back (`state == INTERRUPTED`, `applied=True`), and the same three delegations
are made again against a provider that raises `AssertionError` if a worker so much as starts.

    delegations re-issued after the crash   3
    workers re-run                          0
    worker_created events, before / after   3 / 3
    worker_result_reused events             3
    reuse rate                              3/3 = 100%

The in-run half is the same number by a different route: a redundant delegation inside one
run is one `worker_created` and one `worker_result_reused`.

**What that number is not.** It is a reuse rate on identical task specs, which is the case
the cache is for and the only one it can serve. The live-data check says how often that case
has occurred here so far: the journal holds **5 real `worker_created` events in 4 runs**, and
Postgres holds **6 `kind='subagent'` actions with 6 distinct task strings** - so no run on
this machine has ever made the same delegation twice, and no run with a completed worker has
ever been resumed. The historical hit rate is therefore 0 of 6, and it is 0 because nothing
has yet been repeated, not because the key missed. The first real measurement worth having is
`worker_result_reused` counted against `worker_created` over the next weeks of use.

### schemas exactly as implemented

```
result_key(spec) -> str            == TaskSpec.digest
                                   == worker_created.task_digest, on purpose
  = sha256 of {"spec_version", "durable_role", "task", "relevant_context",
               "constraints", "expected_output"}, json, sort_keys, separators=(",",":")

relevant_context_refs              = TaskSpec.relevant_context: normalized (NFC, whitespace),
                                     deduplicated, sorted. Prose today, not urls and ids
                                     (6a open question 1), which is why a *missed* hit is the
                                     likely failure here and a false hit is not.
scope                              the run. Enforced by `WHERE run_id = ?`, never by the key.
```

```
agent/result_cache.py
  result_key(spec)                         -> str
  entry_for(spec, result)                  -> dict     the journal payload
  result_from_entry(entry)                 -> WorkerResult   every field read by key
  remember(rj, spec, result, *, worker_id) -> bool     False for blocked/uncertain
  lookup(store, run_id, spec)              -> WorkerResult | None    writes nothing
  serve(rj, spec, *, parent_step_id=None)  -> WorkerResult | None    + journals the reuse
  ResultCacheError

journal/worker_results.py
  CACHED = "worker_result_cached"   REUSED = "worker_result_reused"   ENTRY_VERSION = 1
  cached_results_at(store, run_id, covers_seq=None) -> tuple[dict, ...]   first entry wins
  cached_result(store, run_id, result_key)          -> dict | None
```

The journal, two types added (no migration; the file stays at schema v3):

```
worker_result_cached   worker_id (the worker that earned it), result_key, name,
                       status: enum ("completed",)     only completed is ever cached
                       entry_version: int
                       answer: str                     in full, not a preview
                       evidence, actions_taken, followups, notes: list
                       tainted: bool
                       (no transcript, no report_error, no candidate_memories)
                       synchronous - writer.SYNC_TYPES

worker_result_reused   result_key, name, source_worker_id, answer_chars,
                       parent_step_id: str|null
                       the only record of a delegation that ran no worker
```

`WorkerResult` gained one field:

```
reused_from  str = ""   the worker whose run earned this, when it was served from the cache;
                        "" when a worker produced it just now. In for_orchestrator(debug=True)
                        only, and it is what explains an empty transcript on a reused result.
```

The checkpoint, one slot filled (no schema change - the column has existed since 4a):

```
checkpoint.worker_results   [ <worker_result_cached payload>, ... ]  as of covers_seq,
                            read through worker_results.cached_results_at, never counted here
```

### deferred items, and where they went

- **Cross-run reuse — barred, and not worked around.** The pass's *Must not*. Nothing here
  knows whether the file, the repository or the web page a worker read has changed since,
  and the fork tests pin that a rewind inherits nothing.
- **A cache hit for `agent="memory"` — not applicable.** It builds no worker and is not a
  delegation; Pass 8 owns that branch.
- **Parallel workers — still not this pass's.** `open_workers[]` has never been non-empty.
  The fold's first-entry-wins rule is written for the day it is, and is tested today.
- **`WorkerRef.role` — still `"subagent"`, still untouched** (6a open question 2). 6c does
  not re-delegate from a `WorkerRef`: an open worker is re-delegated by the caller re-issuing
  its own `TaskSpec`, and only a *finished* worker's result is ever restored. So the bug did
  not block this session and is still there.

### open questions for later passes

**1. The cache is keyed on prose, and the local 27B writes the prose.** 6a's open question 1,
now with a consequence: the tool's `context` argument is a paragraph the model composes, so
two delegations that mean the same thing miss unless it re-types them identically. The
measurement to take before any more normalization is `worker_result_reused` against
`worker_created` on real runs; if it is near zero while the same work is visibly being
repeated, refs (urls, paths, fact ids) are the fix, not fuzzier matching.

**2. An in-flight worker is still re-delegated from nothing.** A crash *inside* a worker
leaves `worker_created` with no result, and the run's own record of what was asked is a
200-character preview plus a digest - neither of which can be re-delegated from. The full
brief is in Postgres (`subagent_message`) and in `actions.input.task_spec`, so the
reconstruction exists but crosses a store boundary the resume path deliberately does not
cross. Worth deciding in Pass 7 or 8, not assumed.

**3. Autonomy is not part of the key, and a hit can cross a change in it.** Two delegations
with identical specs made at different autonomy levels share a result. Nothing new *happens*
on a hit - it is a read of this run's own history - so this is recorded rather than fixed,
but the day a role's tool set or its cap becomes dynamic, the spec stops being the whole
identity of the work.

**4. Nothing expires.** A run that lasts hours serves a result earned in its first minute. In
a run-scoped cache that is the definition, and it is also the staleness question the *Must
not* deferred, one scope smaller.

**5. Still open, untouched by 6c:** a worker is shown its caller's conversation while its
brief says otherwise (6b #1); nothing requires a `completed` to cite anything (6b #2); effect
events carry no `worker_id` (3b #4); checkpoints are off in the shipped config and have never
been on; token accounting is still broken upstream.
