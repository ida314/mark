# Pass 6 — Delegation Interface & Standardized Results — outcome

Sessions completed: **6a**. 6b (result schema) and 6c (result cache) are untouched.

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
