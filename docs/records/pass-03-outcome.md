# Pass 3 — Effect Class & Effect Ledger — outcome

Sessions completed: **3a**, **3b**. The two audit sessions 3c/3d are untouched and are hard
stops a human runs, so the pass's exit criteria are **partly** met: every registered tool has
an `effect_class` (3a), every effecting call now writes a ledger entry under a stable
idempotency key, and two attempts at the same logical call produce the same key (3b). What
is still missing is the part no code can supply — **nobody has judged a single tool's class**.
All 26 builtins still declare the `UNAUDITED` placeholder, which is `unsafe_write`, so the
ledger currently records `fs_read` and `time_now` as effects that might have happened.

That is safe today because nothing reads the ledger. It stops being safe the moment Pass 4
reconciles, which is why **Pass 4 must not ship before 3c/3d** (3a's open question 1, still
open and now load-bearing).

---

## Session 3a — Registry field and validation

### what shipped

| file | what it is | lines |
|---|---|---|
| `src/agentd/tools/effects.py` | the vocabulary, the gate, and the unaudited set | 109 |
| `src/agentd/tools/base.py` | `Tool.effect_class`, `__post_init__`, `tool()` keyword | +13 |
| `src/agentd/tools/registry.py` | `Registry.add` refuses an unclassified tool | +11 |
| `src/agentd/tools/builtin_*.py` | 25 declarations, all `UNAUDITED` | +34 |
| `src/agentd/mcp_client.py` | a tool that arrived over a pipe is `unsafe_write` | +7 |
| `tests/test_tool_effect_class.py` | 8 tests | 125 |
| `tests/test_mcp_client.py` | a read-only hint does not move the effect class | +6 |
| `tests/test_{journal_feed,private_interlock,tools_and_daemon}.py` | four tool doubles declare `read` | +7 −4 |

Suite 614 → 622 passing. `.venv/bin/ruff check src tests scripts` clean. No tool was added,
removed, moved, renamed or re-risked; no behaviour changed, because nothing reads
`effect_class` yet.

### what deviated from the plan, and why

**1. The plan's sequencing does not close, and this is the deviation that matters.** 3a
makes the field mandatory with no default; 3c/3d are the sessions that decide each tool's
class, and they are marked in `docs/records/session-ledger.md` as hard stops a human runs.
Between the two, 26 existing tools have to carry *something* or `build_registry()` raises
and the whole runtime is dead for as many sessions as the audit takes. The three ways out
were: break startup until the audit (a dead repo hand-off), give the field a default
(exactly what the pass exists to forbid), or declare a conservative placeholder.

The placeholder was chosen, with the one property that makes it honest: the state "nobody
has looked at this" is recorded separately from the value, because the value alone cannot
express it.

```python
UNAUDITED: EffectClass = UNSAFE_WRITE          # the value at the declaration site
UNAUDITED_TOOLS: frozenset[str] = {...26...}   # who is in that state, as data
```

`UNAUDITED` **is** `unsafe_write` — not a fourth class, and it passes the same gate —
because the pass file's own rule for uncertainty is that a wrong `unsafe_write` costs one
avoidable prompt and a wrong `read` costs a duplicate action nobody can take back. It is
spelled differently at the call site so `effect_class=UNAUDITED` reads as "nobody looked"
rather than "somebody decided this is unsafe", which are different claims. 3c/3d replace
each use with a real class and drop the name from `UNAUDITED_TOOLS`; the audit is done when
that set is empty. This mirrors `journal.EMITTED_TYPES` from 2b: the gap between what is
specified and what has actually been done is data with a test on it, not a paragraph that
goes stale.

The risk being accepted, stated plainly: an auditor who sees `unsafe_write` on `fs_read` and
rubber-stamps it. `UNAUDITED_TOOLS` is the whole defence against that.

**2. The check is in two places, not one.** The pass says registration fails. It does, in
`Registry.add`. But `Tool.__post_init__` also validates, so the failure normally happens at
import of the module that defines the tool, naming it, long before any registry exists.
Same reasoning 2a gave for enforcing `seq` in the writer *and* in a SQLite trigger: the
constructor is the early loud failure, the registry is the one that binds everything
arriving by another road — an MCP server's tools, a duck-typed stand-in, or a `Tool` whose
mutable `effect_class` field was reassigned after construction. Mutation-checked: removing
either one leaves tests failing.

**3. MCP tools are classified `unsafe_write` at wrap time, and this pre-empts a corner of
3d.** It had to be decided here, because `ServerConnection.wrap` constructs a `Tool` and the
field is mandatory. The MCP protocol has no effect class; `read_only_hint` is the server's
claim about itself, and `_risk_for` already only believes it when the user set
`trust_annotations`. Risk and effect class answer different questions — "ask the user first?"
versus "may a crash re-run this?" — and a stranger's self-description can settle the first
but not the second. `test_a_read_only_hint_counts_only_when_the_server_is_trusted_to_say_so`
now asserts that the hint moves `risk` to `read` and leaves `effect_class` at
`unsafe_write`.

**4. Test doubles declare `read` rather than `UNAUDITED`.** Four `Tool(...)` fixtures in
`test_journal_feed`, `test_private_interlock` and `test_tools_and_daemon` wrap handlers that
return a constant string. `read` is true of them, they are not in the registry, and they are
not the audit's business.

**5. `effect_class` is not persisted to Postgres.** `repo_ops.upsert_tool_row` still writes
`risk` and not the effect class (checked against the live table: `tools` has
`name, source, description, input_schema, tags, risk, always_on, enabled, desc_sha256,
embedding, embedding_model, updated_at`). Adding a column means an additive migration that
must be run by hand with `agent db migrate`, for a value nothing queries; the row exists to
serve similarity search, and the registry is the in-process authority §19 describes. Left
out deliberately rather than by oversight — see deferred items.

### what is now true about the code that was not before

- **A tool that has not said whether re-running it is safe cannot exist and cannot be
  registered.** `Tool(...)` without the keyword is a `TypeError` naming `effect_class`;
  `@tool(...)` without it is the same; a value outside the vocabulary, an empty string, a
  `None` assigned after construction, or an object missing the attribute entirely is a
  `ToolRegistrationError` naming the tool. There is no path that yields a class the caller
  did not write down.
- **The field has no default, and that fact is itself asserted.**
  `test_the_effect_class_field_has_no_default_to_fall_back_to` reads
  `dataclasses.fields(Tool)` and fails if anyone later adds `= "read"` to quiet a
  construction error. This is the one mutation that would reintroduce the exact bug the pass
  exists to prevent, and it would otherwise be invisible: adding a default makes every test
  pass again.
- **Startup failure is exercised through the real assembly**, not a mock:
  `test_startup_fails_loudly_when_one_builtin_tool_is_unclassified` blanks one entry of
  `builtin_fs.TOOLS` and calls `build_registry()`. That is the pass's stated exit for 3a.
- **`risk` and `effect_class` are now two fields that must not be conflated**, and the
  comment on the field says so. `risk` gates approval (`needs_reason`, the policy engine);
  `effect_class` gates replay. `fs_read` is `risk="read"` and its effect class is an open
  question until 3c; `reminder_set` is `risk="draft"` and may well be `unsafe_write`. No
  derivation of one from the other was written, and none should be: a derivation is a
  default wearing a different hat.
- **Nothing consumes the value.** No ledger, no key derivation, no reconciliation, no
  resume — per the *Must not*. The only readers are the tests and a human.

**Mutation-checked rather than trusted for being green.** Four mutations, all caught:
give the field `default="read"` (2 failures, including the no-default guard); delete the
check from `Registry.add` (3); make `__post_init__` a `pass` (1 — the vocabulary test is the
only thing that reaches it, which is expected: the constructor's unique coverage is exactly
"a bad value written at a call site"); let the MCP wrapper derive `read` from a trusted
`read_only_hint` (1, the mcp test).

The registry was also built in a real process — `agent tools list` prints all 26 rows — so
"26 tools registered, every one classified" is an observation and not only a suite result.

### schemas as actually implemented

`src/agentd/tools/effects.py`, the whole public surface:

```python
EffectClass = str                      # read | idempotent_write | unsafe_write

READ             = "read"              # no external state change; free to re-execute
IDEMPOTENT_WRITE = "idempotent_write"  # re-execution converges to the same state
UNSAFE_WRITE     = "unsafe_write"      # re-execution may duplicate a real-world action

EFFECT_CLASSES: tuple[EffectClass, ...] = (READ, IDEMPOTENT_WRITE, UNSAFE_WRITE)
MEANING: dict[EffectClass, str]                 # the three definitions above, as data
UNAUDITED: EffectClass = UNSAFE_WRITE           # the placeholder value; not a fourth class
UNAUDITED_TOOLS: frozenset[str]                 # the 26 names nobody has judged

class ToolRegistrationError(Exception): ...
def check_effect_class(name: str, value: object) -> EffectClass: ...
```

`check_effect_class` takes the name and the value rather than a `Tool`, so `effects` imports
nothing and `base` can import it without a cycle; it takes `object` rather than `str`
because the case worth catching is the one where the attribute is missing or `None`. It
raises on `None`, on a non-string, on `""`, and on any string outside `EFFECT_CLASSES`. It
never returns a fallback.

On `Tool` (`tools/base.py`):

```python
effect_class: EffectClass = field(kw_only=True)   # mandatory, no default
def __post_init__(self) -> None: check_effect_class(self.name, self.effect_class)
```

`kw_only=True` is what lets a field with no default sit among fields that have one; it also
means the class can only ever be passed by name. `tool(...)` gained `effect_class` as a
required keyword-only argument and forwards it.

`Registry.add` calls `check_effect_class(t.name, getattr(t, "effect_class", None))` before
`self.tools[t.name] = t`, so a refused tool is not in the registry and is not callable.

**The classification table does not exist yet.** `docs/records/effect-classification.md` is
3c/3d's file and was not created; writing rows into it now would be the audit, done by the
wrong session. The current state of every registered tool, for the record:

```
all 26 builtin tools        unsafe_write   UNAUDITED - nobody has judged these
mcp:<server>/<tool>         unsafe_write   not knowable at registration (see deviation 3)
```

### deferred items, and where they went

- **The ledger, the key derivation and the canonicalization exclusion list — 3b.** Nothing
  in this session writes a ledger row, computes an idempotency key, or touches
  `tools/executor.py`. `effect_intended` / `effect_committed` are still the 2b event shapes
  that nobody emits.
- **The actual classification — 3c (reads and internal tools) and 3d (writes and
  integrations).** Both are hard stops for a human. The mechanical part of their work is:
  replace `effect_class=UNAUDITED` with a real class at the declaration, remove the name
  from `UNAUDITED_TOOLS`, add the row to `docs/records/effect-classification.md`.
  `test_the_tools_nobody_has_judged_yet_are_listed_rather_than_assumed_safe` keeps the set
  and the declarations agreeing while that happens.
- **`agent tools list` does not show the effect class.** One column in `cli/app.py`, left
  out as outside this session's scope, but it is the obvious quality-of-life change for
  whoever runs 3c/3d — the audit would otherwise be read entirely out of source.
- **Persisting the class to the `tools` table.** Deviation 5. It needs an additive migration
  and `agent db migrate`; nothing queries it.
- **A per-server effect-class declaration in `[mcp]` config.** The only way an MCP tool
  could ever be anything but `unsafe_write`. Not built; nobody has a server that needs it.

### open questions for later passes

**1. `UNAUDITED_TOOLS` is 26 of 26, and until it shrinks the classification is worthless in
both directions.** Everything reads `unsafe_write`, so a Pass 4 resume would surface
`time_now` and `fs_list` as uncertain effects needing a human, which is noise that trains
the user to click through — the failure mode a conservative default has instead of duplicate
mail. 3b can build the ledger against this safely because nothing reconciles yet, but **Pass
4 must not ship before 3c/3d**.

**2. A tool call outside a turn is still journaled by nobody, and now it also has no ledger
entry. → 3b.** Unchanged from 2b's open question 2: `policy/replay.execute_approved` runs a
queued approval through the executor with `ctx.run_id = None`. The idempotency key is
`hash(run_id, step_id, tool_name, canonical_args)` and two of those four are `None` there, so
3b has to decide whether a queued call rejoins its original run or opens one — a key computed
from a missing run collides with every other keyless call.

**3. The effect class is declared per *tool*, and at least one tool's real class depends on
its arguments.** `fs_write` takes `mode ∈ {overwrite, append, create}`: `append` duplicates
content on re-execution, `create` fails the second time, `overwrite` converges. One field
cannot say that. The options are to classify at the tool's worst case (what the field
supports today), to let a tool compute its class from its arguments (a callable, which 3b's
key derivation would have to call before dispatch), or to split the tool (Pass 8). Recorded
here because it will be the first argument 3d has, and the pass file's own list of "calls
worth arguing about" starts with `fs_write`.

**4. `risk` and `effect_class` will drift.** They are set at the same call site, they look
alike, and nothing checks them against each other — deliberately, because any check is a
derivation and a derivation is a default. After 3c/3d there will be a factual relationship
(a `risk="read"` tool that is `unsafe_write` should be surprising and rare), and *that* is
worth an assertion. It cannot be written before the audit exists.

**5. Still open, untouched by 3a:** power-loss durability is reasoned rather than measured
(2a #2); the fsync cost is unmeasured and 3b adds two synchronous appends per effecting call
(2a #3, 2b #4, 2c #4); `_migrate` cannot migrate and 3b's ledger is the likely first trigger
if it lands in the journal file (2a #6); retention's `prunable` hook still cannot refuse to
drop a run holding an uncommitted effect, which is 3b's to give it (2a #7); a degraded turn
is invisible to a fold (2c #1); `run_id != turn_id` for the REPL and Telegram (2c #2). And
from Pass 1, token accounting is still broken upstream.

---

## Session 3b — Ledger and key derivation

### what shipped

| file | what it is | lines |
|---|---|---|
| `src/agentd/tools/idempotency.py` | canonicalization, the exclusion list, key derivation | 248 |
| `src/agentd/journal/ledger.py` | the `effect` table, the state machine, the journal emission | 429 |
| `src/agentd/journal/store.py` | schema v2 (the `effect` table), a real migration ladder, `transaction()` / `query()`, prune takes effects with it | +89 −13 |
| `src/agentd/tools/executor.py` | the protocol around dispatch; `_effect_scope`, `_result_ref` | +114 −8 |
| `src/agentd/journal/events.py` | `effect_*` moved into `EMITTED_TYPES` | +21 −7 |
| `src/agentd/tools/effects.py` | `EFFECTING` — the classes that get a row | +7 |
| `src/agentd/agent/loop.py` | the executor's ledger writes through *this loop's* journal | +6 |
| `tests/test_idempotency_key.py` | 16 tests | 234 |
| `tests/test_effect_ledger.py` | 19 tests | 465 |
| `tests/test_journal_events.py` | the complete-turn sequence now contains the effect pair | +24 −13 |

Suite 622 → 657 passing. `.venv/bin/ruff check src tests scripts` clean. No tool was added,
removed or moved; no tool's class changed.

### what deviated from the plan, and why

**1. `started` is a ledger-only transition. The plan says every transition appends to the
journal; there is no third event type.** Pass 2b was told to "define their payload shapes now
so those passes do not re-litigate the schema" and defined exactly two effect events, and
`tests/test_journal_events.py::test_the_vocabulary_is_exactly_the_one_the_pass_specified`
is a deliberate drift guard asserting the vocabulary is the seventeen pass-02 listed. 2c hit
the same wall and recorded it ("inventing an 18th was out of 2c's scope"). Adding
`effect_started` would have been an 18th type, a third synchronous fsync on every effecting
call, and an edit to that guard.

What it costs: a re-fold of the journal reconstructs every column of every ledger row
**except** whether dispatch began — `intended` and `started` both fold to "announced, never
resolved". The ledger holds one bit the journal cannot reproduce. That bit changes no decision
available to this pass (reconciliation, the only consumer that could use it, is the *Must
not*), and the conservative reading is the one a missing ledger forces anyway: **Pass 4, if it
ever finds the ledger absent or behind, must treat every announced-and-unresolved effect as
uncertain.** That is written into `ledger.py`'s module docstring, not only here.

**2. Journal first, ledger second — and they are not one transaction.** They could have been:
both tables are in one file behind one connection. They are not, because the ordering rule is
worth more than the atomicity. The journal is the source of truth and the ledger is derived
from it, so a crash between the two must leave the journal *ahead*: an `effect_intended` with
no row is recoverable by re-folding, a row with no event is a claim with nothing behind it.
This ordering is reasoned, not tested — there is no seam to kill between the two writes, and
inventing one would have tested the seam.

**3. The ledger lives in `journal.db`, which made 2a's open question 6 due.** `_migrate` ran
one `IF NOT EXISTS` script and bumped `user_version` whether or not anything happened. It is
now a ladder: `MIGRATIONS = {1: SCHEMA_V1, 2: SCHEMA_V2}`, applied in order from whatever the
file is at, with the version bumped one step at a time so an interrupted upgrade resumes
instead of claiming to be done. Verified against a **copy of the live file**
(`~/.local/share/agent/journal.db`, 34 events across 6 runs, `user_version=1`): it comes out
at 2 with all 34 events present and an empty `effect` table. The live file itself was not
touched. Postgres was not involved and `agent db migrate` was not run — the run journal is a
different store with a different rule.

**4. Two columns beyond the plan's sketch: `effect_id` and `attempt`** (plus `started_at`).
2b's event shape has `effect_id` *and* `idempotency_key` as separate fields, which only makes
sense if one identifies the attempt and the other the logical call. The primary key is the
idempotency key, so a second attempt lands on the same row — `attempt` is what keeps that from
erasing the fact that there were two, and `effect_id` is what ties a row to the specific pair
of journal events that describe its latest attempt.

**5. A call with no run gets a run of its own rather than a shared placeholder** (3a's open
question 2, resolved here). `policy/replay.execute_approved` runs a queued approval with
`ctx.run_id = None`; the MCP seam has no turn at all. Such a call is keyed under
`detached:<action_id>`, unique per attempt. The alternative — a constant like `"detached"` —
is this codebase's recurring bug wearing a different hat: a bucket is half a key, and every
keyless call in it would hash alike and read as a retry of every other one. A missing *step*
is treated the same way and does **not** fall back to `"s1"`, because `s1` is a position the
loop also hands out. The honest cost: a detached call cannot be deduplicated across attempts,
because outside a run there is no attempt identity to reproduce. Its two events do land in the
journal, in a run with no `agent_started` — see open questions.

**6. The complete-turn exit-criterion test from 2b changed.** A turn that calls `fs_read` now
journals `effect_intended` / `effect_committed` between `tool_started` and `tool_finished`,
and the sequence assertion says so. This is a real change to what a turn records, not a test
being loosened, and it is temporary in one respect: `fs_read` is in that pair only because it
still declares `UNAUDITED`. The test says so at the line, so whoever runs 3c knows the two
lines are expected to disappear.

**7. `read` calls get no row and no events.** The pass says "every effecting call"; `read` is
by definition not one. Rowing every `time_now` would cost two synchronous appends for a call
that changes nothing outside, and would bury the entries that matter under the ones that never
can. Encoded as `effects.EFFECTING`, derived as the complement of `read` so a fourth class
could never be added to one list and forgotten in the other.

### what is now true about the code that was not before

- **An unsafe write cannot happen without a record that preceded it**, from any caller. The
  announcement is made in `tools/executor.py`, the one chokepoint, and not in `loop.py` where
  the tool events live — so the approval-queue replay path and the MCP seam are covered too,
  which was 2b's open question 2 and baseline finding 3.
  `test_the_record_of_an_unsafe_call_is_on_disk_before_the_call_runs` asserts it from *inside
  the handler*: by the time the tool's own code runs, the event is on disk and the row says
  `started`.
- **Two attempts at the same logical call produce the same key, and that is pinned by a golden
  digest.** `test_the_derivation_itself_is_pinned` fails if the derivation changes at all,
  because a silent change is indistinguishable from "none of these calls ever happened".
- **Canonicalization failure is loud.** A value with no canonical JSON form raises
  `CanonicalizationError` naming the path and the type, and the executor refuses the call.
  The counter-example is in the tree: `repo_ops.args_hash` hashes with `default=str`, so an
  object with no JSON form hashes as a repr containing a memory address — a key that looks
  healthy and never matches itself. That function is untouched (it keys approvals, and
  changing it would invalidate queued rows), but the new module's docstring names it.
- **The storage refuses two states rather than trusting the writer**: an effect class or a
  ledger state outside its vocabulary, and a terminal row with a NULL `result_ref` ("done,
  outcome unrecorded" — the house bug in table form). The second has a test that goes around
  the Python API and gets an `IntegrityError`.
- **A failed effect always says why**, including when the tool said nothing: the error falls
  back to a sentence, never to `""` or `None`.
- **The journal's SQLite file can now be migrated.** First real schema change since 2a, and
  the ladder was exercised against a copy of the live file rather than only against fixtures.
- **Pruning a run takes its effects with it.** A run is entirely present or entirely gone.

**Mutation-checked rather than trusted for being green.** Five mutations, all caught: delete
`effect.dispatched()` (6 failures); collapse detached scope to a shared `"detached"` bucket
(2); stringify a non-JSON value instead of raising (2); drop `reason` from the exclusion list
(5) and drop `request_id` (1); move the intent record above the policy gate (1). The
`request_id` mutation is the one that found a real weakness in a test — see the exclusion-list
note below.

### schemas as actually implemented

```sql
-- journal.db, schema v2. MIGRATIONS = {1: SCHEMA_V1, 2: SCHEMA_V2}
CREATE TABLE IF NOT EXISTS effect (
  idempotency_key text PRIMARY KEY,
  run_id          text NOT NULL,
  step_id         text NOT NULL,
  tool            text NOT NULL,
  effect_class    text NOT NULL
                  CHECK (effect_class IN ('read','idempotent_write','unsafe_write')),
  args_hash       text NOT NULL,
  state           text NOT NULL
                  CHECK (state IN ('intended','started','committed','failed','orphaned')),
  result_ref      text,          -- "action:<uuid>", a pointer into the audit table
  effect_id       text NOT NULL, -- uuid7, identifies this *attempt*
  attempt         integer NOT NULL CHECK (attempt > 0),
  created_at      text NOT NULL,
  updated_at      text NOT NULL,
  started_at      text,          -- NULL until dispatch; that NULL means "never dispatched"
  CHECK (result_ref IS NOT NULL OR state NOT IN ('committed','failed'))
);
CREATE INDEX IF NOT EXISTS effect_run   ON effect (run_id, created_at);
CREATE INDEX IF NOT EXISTS effect_state ON effect (state);
```

States and the transitions that exist:

```
intended --> started --> committed        (ALLOWED, in ledger.py, enforced both in the
         \          \--> failed            handle and in the UPDATE's WHERE clause)
          \-> failed
orphaned:  written by nobody. Pass 4 sets it on a row a crash left at `started`.
uncertain: an effect_committed *status*, not a ledger state. Also Pass 4's to write.
```

Key derivation, exactly:

```python
KEY_VERSION = "effect/v1"

canonical = json.dumps(kept, sort_keys=True, ensure_ascii=False,
                       separators=(",", ":"), allow_nan=False)   # recursively re-keyed
args_hash        = sha256(canonical)
idempotency_key  = sha256(json.dumps([KEY_VERSION, run_id, step_id, tool_name, canonical], …))
```

The four parts are hashed as a JSON array, not joined with a delimiter: run `"a"` at step
`"b|c"` must not equal run `"a|b"` at step `"c"`, and any separator a caller could type is a
separator a caller can forge. `run_id` and `step_id` must be non-empty — an empty one raises
rather than producing a key that collides with every other key with the same hole.

**Canonicalization normalizes structure and never content.** Dict keys sorted at every depth;
tuples become lists; list order preserved. Explicitly *not* done: Unicode normalization,
whitespace trimming, case folding, number coercion. Each of those would make two different
strings hash alike, and a false "same call" suppresses an action and reports it as done, which
is strictly worse than a duplicate. Refused rather than coerced: a non-string object key
(`json.dumps` would silently merge `{1:"a"}` and `{"1":"a"}`), `NaN` / `Infinity`, and any
value with no JSON form.

**The full exclusion list — every field dropped before hashing**, with the reason each is
there. Excluded at the **top level only**, and only when the tool does **not** declare that
name as a parameter (a tool whose schema really has a `timestamp` is a tool for which two
timestamps are two different actions):

```
reason                          human justification, rewritten per attempt
idempotency_key                 the caller's own per-attempt key
request_id, client_request_id   per-attempt request identifier
client_token, nonce             per-attempt token / nonce
attempt, retry, retry_count     count the attempt, not the call
now, timestamp                  wall clock at the moment of the attempt
requested_at, sent_at           wall clock at the moment of the attempt
call_id                         the provider's id for this tool-call message
action_id                       the id of this attempt's audit row
trace_id, span_id, parent_span_id   observability context for this attempt
```

Not excluded, and worth naming because they look similar: `when`, `due_at`, `start`, `end`,
`date` — those are the user's intent, not the clock. Nested occurrences are content: a
`timestamp` inside a payload the user wrote is hashed.

**How much of that list actually bites today, honestly:** less than it looks.
`ToolExecutor.run` pops `reason` before anything else sees it, and `_argument_faults` reports
any *undeclared* argument name as a fault — so for a tool that declares its properties, a stray
`request_id` fails the call before a key is derived. The list therefore fires only for tools
whose schema declares no properties at all (the unknown-key check is skipped for those) and for
callers that reach the ledger by another road. It is kept as the specification, with reasons,
because the cost of it being latent is zero and the cost of rediscovering it is a duplicate
action. `test_two_attempts_at_the_same_call_share_one_row_and_count_the_attempt` was rewritten
to exercise the path where it does fire, after a mutation showed the first version passing for
the wrong reason.

`ToolExecutor.__init__` gained `ledger: EffectLedger | None`; the loop passes
`EffectLedger(self.journal_writer)` — a *callable*, resolved on each use, so an executor
constructed before anything has decided which journal file this process writes to still
announces its effects into the same file as the run that caused them. A `None` ledger resolves
to `get_ledger()` on the first effecting call. There is no "off".

### deferred items, and where they went

- **Anything that reads the ledger — Pass 4.** No reconciliation, no resume, no orphan
  detection, no dedup. `orphaned` and `uncertain` are written by nobody.
- **The classification — 3c / 3d**, unchanged and unstarted. `UNAUDITED_TOOLS` is still 26 of
  26 and `docs/records/effect-classification.md` still does not exist.
- **Retention refusing to prune a run with an open effect (2a #7).** Not built: the hook would
  have to *read* the ledger to decide, which is the first line of this pass's *Must not*.
  Pruning now deletes a run's effect rows along with its events, so the file stays consistent;
  the refusal is Pass 4's.
- **`agent journal` / `agent why` showing effects.** No CLI surface at all. The rows are
  readable with `EffectLedger.entries(run_id)` and with sqlite3.
- **`backup.create` still does not cover `journal.db`** (2a recorded why), and the ledger is
  in that same file, so the effect record is not backed up either.
- **`repo_ops.args_hash`'s `default=str`** was left alone deliberately; it keys queued
  approvals and changing it would invalidate pending rows. Two argument digests now exist in
  the codebase with different rules, which is worth knowing when reading either.

### open questions for later passes

**1. A detached call's events land in a run with no `agent_started`.** `detached:<action_id>`
appears in the journal as a run containing exactly two events. Nothing folds it yet; a Pass 4
`reduce` must not assume every run opens with `agent_started`, and somebody has to decide
whether a queued approval should instead *rejoin* the run that queued it — which would need
the approval row to have stored a run id, and it does not.

**2. The fsync cost is now real and still unmeasured.** An effecting call adds four
synchronous SQLite commits (two journal appends, two ledger writes) on the event-loop thread.
With every tool currently `unsafe_write`, that is four per call to `time_now`. 3c/3d will drop
most of it; until then it is the largest per-call cost this runtime has, and nobody has timed
it (2a #3, 2b #4, 2c #4, still open).

**3. The ledger holds one bit the journal cannot reproduce** (deviation 1). If Pass 4 wants
`intended` and `started` to be distinguishable from a cold fold, it needs an `effect_started`
event type — an 18th — and should add it there rather than here, where it would have been a
vocabulary change made by the wrong session.

**4. An effect's events are not tagged with `worker_id`.** `ToolContext` carries `run_id` and
`step_id` but not the worker, so a delegated call's effect events carry only the step
(`w-7.s1`, which does identify it). A fold that groups effects by worker has to parse the step
id, or `ToolContext` needs the field.

**5. Two attempts at one logical call are recorded and both run.** That is correct for this
pass and will look like a bug to whoever reads the table first: `attempt=2, state=committed`
means the action happened twice. The row is the evidence Pass 4 needs to stop the second one.

**6. Still open, untouched by 3b:** power-loss durability is reasoned rather than measured
(2a #2); a degraded turn is invisible to a fold (2c #1); `run_id != turn_id` on the REPL and
Telegram (2c #2); `risk` and `effect_class` will drift and cannot be cross-checked until the
audit exists (3a #4); the per-argument effect class of `fs_write` (3a #3) is now concrete —
the ledger records one class per *tool*, so an `fs_write` with `mode="append"` and one with
`mode="overwrite"` are the same class and different keys.

---

## Session 3c — Audit: reads and internal tools

Run autonomously under the orchestrator's standing policy: no human to ask mid-session,
default to the conservative class under any uncertainty, and record every uncertain call
in the table with the reasoning and the rejected alternative rather than deciding quietly.

### what shipped

| file | what it is | lines |
|---|---|---|
| `docs/records/effect-classification.md` | the audit table: 26 rows, 19 rulings, 1 deferral, 6 left to 3d | 91 |
| `src/agentd/tools/effects.py` | `UNAUDITED_TOOLS` 26 → 7; the comment now says why a name can be in it | +11 −24 |
| `src/agentd/tools/builtin_fs.py` | `fs_list`/`fs_read`/`fs_search` → `read` | +3 −4 |
| `src/agentd/tools/builtin_agenda.py` | all 8 ruled: 2 `read`, 2 `idempotent_write`, 4 `unsafe_write` | +13 −9 |
| `src/agentd/tools/builtin_memory.py` | 3 `read`, `memory_remember` → `unsafe_write`, `memory_search` deferred with the reason in place | +13 −3 |
| `src/agentd/tools/builtin_calendar.py`, `builtin_coursework.py` | `calendar_upcoming`, `coursework_due` → `read` | +2 −2 |
| `src/agentd/tools/builtin_delegate.py` | `delegate` → `unsafe_write` | +3 −2 |
| `src/agentd/tools/registry.py` | `tool_search` → `read` | +4 −2 |
| `tests/test_tool_effect_class.py` | the table and the declarations must agree | +42 |
| `tests/test_journal_events.py` | the complete-turn sequence loses its effect pair | +5 −8 |

Suite 657 → 658 passing. `.venv/bin/ruff check src tests scripts` clean. No tool was added,
removed, moved or renamed. Registry built in a real process: 26 tools, 11 `read`,
2 `idempotent_write`, 13 `unsafe_write` — of which 7 are the unruled placeholder.

### what deviated from the plan, and why

**1. One tool in 3c's scope was examined and deliberately left unruled: `memory_search`.**
The pass expects 3c to classify every tool that reads or touches only local state, and this
one does. `retrieval.pack` ends in `repo_memory.touch_accessed`, which runs
`access_count = access_count + 1` on every fact it returned, so re-execution does not
converge and the tool is not literally `read`. Nor is it plausibly `unsafe_write`: nothing
outside the machine changes, and a replay genuinely *is* another access, so the counter is
arguably right either way. Under the standing policy — take the conservative option and
record the alternative — the placeholder stays and the reasoning is written at the
declaration site and in the table. The cost of leaving it is visible and bounded: four
fsyncs on the most-called tool in the runtime, and a Pass 4 prompt asking the user to
confirm a search.

**2. `calendar_upcoming` and `coursework_due` were ruled here although 3d's scope sentence
names "calendar".** Both are pure `SELECT`s over the `raw_events` archive the daemon fills;
neither touches Google, D2L, a credential or the network, and both module docstrings argue
that at length. They are read-only local state, which is 3c's subject. If 3d wants them
back the class does not change.

**3. `tool_search` lives in `registry.py`, not in a `builtin_*` module**, and was easy to
miss: it is constructed by `tool_search_tool(reg)` at the bottom of `build_registry`. Ruled
`read` — a `SELECT` over `tools`, an embedding call that stores nothing, and an append to
`ctx.extra["added_tools"]` which is turn-scoped context, not state a crash can leave
half-done.

**4. The record is held to the code by a test, which the pass did not ask for.**
`test_every_tool_in_the_classification_table_declares_what_the_table_says` parses the
markdown table and fails if a row is missing, duplicated, or disagrees with the declared
class, and checks `unaudited` rows against `UNAUDITED_TOOLS` in both directions. An audit
whose justification and whose value can drift apart is worth less than either: the next
reader would find a reason for a decision nobody made. Mutation-checked both ways
(`fs_list` → `unsafe_write` in source; `notify_user` → `read` in the table).

**5. 3b's exit-criterion test changed, as 3b said it would.**
`test_a_complete_turn_is_readable_from_the_journal_alone` no longer expects
`effect_intended` / `effect_committed` around the `fs_read` call, because `fs_read` is now
`read` and a read gets no ledger row. The assertion is now positive about the absence
(`"effect_intended" not in by_type`) so that a `read` tool which starts journaling effects
again fails here.

### what is now true about the code that was not before

- **19 of 26 builtins carry a class somebody chose, with the reason in two places**: one
  line in `docs/records/effect-classification.md` and, for every tool that is not a plain
  `read`, a comment at the declaration saying what specifically makes it that class (the
  `ON CONFLICT (slug)`, the missing dedup key, the notification the daemon pushes).
- **Reading costs nothing again.** `time_now`, `fs_read`, `goals_list`, `memory_history`,
  `profile_read`, `calendar_upcoming` and the rest no longer write a ledger row or two
  journal events, so 3b's four synchronous commits per call now fall only on calls that
  can actually change something. 11 of 26 tools stopped paying it; `memory_search`, the
  hottest of them, still does, pending the ruling above.
- **Two tools are `idempotent_write`, and both earn it structurally rather than by
  intention**: `goal_upsert` through `INSERT ... ON CONFLICT (slug) DO UPDATE` with a slug
  derived deterministically from the title, `open_loop_close` through an `UPDATE ... WHERE
  id` that is a no-op the second time.
- **`UNAUDITED_TOOLS` now means two things and says which.** A name is in it either because
  nobody has looked (3d's six) or because 3c looked and could not settle it alone
  (`memory_search`). The comment in `effects.py` names both and points at the table.
- **The audit's remaining work is still data**: seven names, and the set is empty when the
  audit is done.

### schemas as actually implemented

No schema changed. The only structural change is the contents of one frozenset:

```python
UNAUDITED_TOOLS: frozenset[str] = frozenset({
    "fs_write", "gmail_message", "gmail_search", "memory_search",
    "shell_exec", "web_fetch", "web_search",
})
```

The classification table's format, which the test parses:

```
| `tool_name` | read | idempotent_write | unsafe_write | unaudited | one line of justification |
```

— three pipe-delimited cells per row, name first, class second, reason third; any row whose
second cell is not a class word is ignored, which is what lets the file carry prose tables
alongside.

### deferred items, and where they went

- **The six tools that touch the filesystem for writing, the shell, Gmail and the web —
  3d**, unchanged. The table records an expectation for each (`fs_write` and `shell_exec`
  unsafe; the four network reads probably `read`) explicitly marked as not a ruling.
- **`memory_search` — a human.** Deviation 1. It is the one row in the table's "deferred"
  section and the reasoning is written at the declaration too.
- **`agent tools list` still does not show the effect class** (3a's deferral). The audit is
  now readable from one markdown file instead, which was the reason that column was wanted.
- **Persisting the class to the `tools` table** — still not done, still needs an additive
  migration, still queried by nothing.
- **`docs/records/session-ledger.md` row for 3c** was not edited; the ledger is maintained
  by whoever commits the session.

### open questions for later passes

**1. `memory_search` is the pass's one unresolved judgement.** Read the deferred row in
`docs/records/effect-classification.md` before Pass 4 ships, because until it is settled the
most frequently called tool in the runtime is the one Pass 4 will most often ask about.

**2. `goal_upsert`'s idempotence is an argument-level property wearing a tool-level
label** — the same shape as 3a's open question 3 about `fs_write`, and now on a tool that is
*already* classified. It converges because `slug` defaults to `slugify(title)`, which is
deterministic. A caller that passes a per-attempt `slug`, or a change to `slugify`, silently
turns an `idempotent_write` into a duplicate-producing insert, and nothing tests that
relationship. The honest fix is an argument-aware class, which is Pass 8's.

**3. A replayed `delegate` is double-counted.** Its sub-agent's calls each write their own
ledger rows through the executor, so one delegation appears as itself and again as
everything underneath it. Conservative in the right direction, but a Pass 4 reconciliation
that re-runs a `delegate` row will re-run children that already have committed rows of their
own, and nothing yet relates a child row to its parent beyond the step id (3b open
question 4).

**4. `risk` and `effect_class` can finally be cross-checked** (3a open question 4). The
factual relationship now exists: `calendar_upcoming` is `risk="read"` and `read`;
`notify_user` is `risk="draft"` and `unsafe_write`; `memory_remember` likewise. No assertion
was written, because six tools are still unruled and the first version of that check would
be asserting over placeholders. It becomes writable when `UNAUDITED_TOOLS` is empty.

**5. Still open, untouched by 3c:** everything in 3b's open questions 1-6 except the fsync
cost, which is now smaller but still unmeasured.

---

## Session 3d — Audit: writes and integrations

Run autonomously under the orchestrator's standing policy: no human to ask mid-session,
the conservative class under any uncertainty, every uncertain call recorded in
`docs/records/effect-classification.md` with the reasoning and the rejected alternative.
Explicitly barred and observed: no live external API was called to settle a classification.
Every "does this API accept a client-supplied id" question was answered by reading this
repo's client code and the provider's documented contract. No mail was sent, no calendar
event created, no remote state mutated, and nothing was run against the live memory store.

### what shipped

| file | what it is | lines |
|---|---|---|
| `docs/records/effect-classification.md` | the 3d rulings, the id-acceptance table, the uncertain calls | +82 −20 |
| `src/agentd/tools/effects.py` | `UNAUDITED_TOOLS` 7 → 1; the comment now says the leftover is a deferral, not an omission | +17 −17 |
| `src/agentd/tools/builtin_fs.py` | `fs_write` → `unsafe_write`, with the backup argument at the declaration | +9 −2 |
| `src/agentd/tools/builtin_shell.py` | `shell_exec` → `unsafe_write` | +7 −2 |
| `src/agentd/tools/builtin_mail.py` | `gmail_search`, `gmail_message` → `read`, justified by the OAuth scope | +13 −3 |
| `src/agentd/tools/builtin_web.py` | `web_search` → `read`; `web_fetch` → `unsafe_write` | +14 −3 |
| `tests/test_tool_effect_class.py` | 3 tests | +49 |

Suite 658 → 661 passing. `.venv/bin/ruff check src tests scripts` clean. No tool was added,
removed, moved or renamed; no already-ruled class was changed. Registry built in a real
process: 26 tools, **14 `read`, 2 `idempotent_write`, 10 `unsafe_write`** — of which one
(`memory_search`) is still the unruled placeholder.

### what deviated from the plan, and why

**1. `web_fetch` is `unsafe_write`, reversing the expectation 3c wrote into the table.**
This is the session's one judgement call and the one most likely to be argued with. The
case for `read` is real: the tool cannot write, `GET` is defined as safe, and refetching a
page is normally nothing. It was rejected because "safe" is a promise the *server* makes
and routinely breaks — one-click unsubscribe links, email confirmation links and
GET-shaped API endpoints all act — this tool has no contract with the far end and cannot
tell them apart, and the URL is chosen by the model, frequently out of untrusted text this
same tool returned. The pass file's own asymmetry then settles it. The accepted cost is
four fsyncs per fetch and a Pass 4 prompt on a commonly used tool; recorded so a human can
overturn it cheaply, since overturning it is one word in two places.

**2. The two Gmail tools are `read`, and the reason is the OAuth scope rather than the
handlers.** `google_auth.GMAIL_SCOPE` is `.../auth/gmail.readonly`, so Google refuses any
mutation with that token regardless of what this codebase asks for; `users.messages.get`
does not clear UNREAD (only a labels modify does, which the scope forbids). Their
`private_output=True` raises `session.private`, which was considered and is not an external
effect: it is in-process session state that a replay merely re-raises. Rejected
alternative: `unsafe_write` because mail is private. A replay re-exposes the same mail to
the same agent in the same run, which is not a duplicated real-world action.

**3. The pass file's list of "calls worth arguing about" names two calls that do not
exist.** There is no `gmail send` tool and no `calendar create` tool in this registry — no
tool sends mail, and both Google scopes are read-only. Rather than skip those rows, the
record states what a future implementer needs: `users.messages.send` accepts **no**
client-supplied idempotency id (server-assigned id, a repeat is a second mail), while
Google Calendar's `events.insert` **does** accept a client-generated `id`, which is the
only thing that could make a calendar create `idempotent_write`. `reminder_set` — the
other name on that list — is a local watcher row and was already ruled `unsafe_write` by
3c; no external API is involved, so no id question arises.

**4. `memory_search` was left unruled, as 3c left it.** It is not in 3d's scope (it touches
no filesystem, shell, mailbox, calendar or external API) and 3c referred it to a human by
name. Ruling it here would have been this session overriding a deferral it was not asked to
resolve. Consequence: `UNAUDITED_TOOLS` is one name, not empty, so the pass's "every tool
covered" is met in the record (every tool has a row and an argument) but not in the sense
of "every tool has a decided class".

**5. Nothing in 3d is `idempotent_write`**, so the id-acceptance check gated nothing. It was
still performed and written down for all six integrations, because the value of the check is
mostly in the case where the answer would have changed a class, and the next reader cannot
tell "checked, no" from "never checked" unless it is recorded.

### what is now true about the code that was not before

- **Every registered tool but one carries a class somebody chose**, each with a one-line
  justification in `docs/records/effect-classification.md` and, for the non-obvious ones, a
  comment at the declaration saying what specifically decides it.
- **The two cheapest-looking network tools are split down the middle**, deliberately:
  `web_search` is `read` because the endpoint is fixed and the model supplies only a query;
  `web_fetch` is `unsafe_write` because the model supplies the URL. That distinction — who
  chooses the far end — is the operative one and is written at both declarations.
- **The Gmail ruling is pinned to the fact that justifies it.**
  `test_the_gmail_tools_are_read_because_the_scope_makes_them_read` fails if
  `GMAIL_SCOPE` or `CALENDAR_SCOPE` stops ending in `readonly`. Widening the scope later to
  mark a thread read would otherwise leave two tools classified `read` whose handlers still
  look exactly like reads.
- **`fs_write` cannot be quietly downgraded while `append` is still one of its modes.**
  `test_fs_write_cannot_be_downgraded_while_append_is_still_a_mode_it_accepts` reads the
  `mode` enum out of the tool's own schema, so the eventual (correct) argument that
  `overwrite` converges cannot land without either splitting the tool or removing `append`.
- **The audit's remaining work is an equality, not a subset.**
  `test_the_only_tool_left_unruled_is_the_one_a_human_was_asked_to_settle` asserts
  `UNAUDITED_TOOLS == {"memory_search"}`, so a tool added later with the placeholder fails
  the suite at the moment someone is still in a position to classify it.
- **Ten tools pay the ledger cost and sixteen do not.** `gmail_search`, `gmail_message` and
  `web_search` stopped writing a ledger row and two journal events per call.

**Mutation-checked rather than trusted for being green.** Three mutations, all caught:
`GMAIL_SCOPE` → `gmail.modify` (1 failure); `fs_write` → `read` in source (2, including the
table-agreement test); adding `web_fetch` to `UNAUDITED_TOOLS` (2). Registry built in a real
process to confirm the counts above rather than reading them off the suite.

### schemas as actually implemented

No schema changed. The only structural change is the contents of one frozenset:

```python
UNAUDITED_TOOLS: frozenset[str] = frozenset({
    "memory_search",
})
```

The classification table keeps 3c's three-cell row format, which the parser in
`tests/test_tool_effect_class.py` reads. The new id-acceptance table is deliberately
**four** cells wide so the parser ignores it: it is evidence about integrations, not a
claim about a tool's class, and a row in it must never be mistaken for a ruling.

### deferred items, and where they went

- **`memory_search` — a human**, unchanged from 3c. One row in the record's deferred
  section, the reasoning also at the declaration.
- **An argument-aware effect class — Pass 8.** `fs_write` (mode) and `goal_upsert` (slug)
  both carry a tool-level label over an argument-level property. Splitting a tool is
  explicitly out of this pass (*Must not*: do not change which tools exist).
- **A per-host policy for `web_fetch`.** If the `unsafe_write` ruling proves expensive, the
  cheap remedy is an allowlist of hosts whose GETs are known inert, not a downgrade of the
  tool. Not built, not designed.
- **The `risk` / `effect_class` cross-check (3a #4, 3c #4).** Now writable — the factual
  relationship exists for 25 of 26 tools — but not written, because one tool is still a
  placeholder and the first version of that check would be asserting over it. It belongs to
  whoever rules `memory_search`.
- **`agent tools list` still does not show the effect class**, and persisting the class to
  the Postgres `tools` table is still undone (additive migration, nothing queries it).
- **`docs/records/session-ledger.md`** was not edited by this session; the orchestrator's
  dispatch row was already there.

### open questions for later passes

**1. `web_fetch` is the row to review first.** It is the only reversal of a previous
session's stated expectation, it falls on a frequently used tool, and it is one word in two
places to overturn. The argument for and against is in the record.

**2. `memory_search` is still the pass's one unresolved judgement**, and it is the most
frequently called tool in the runtime — so it is the one Pass 4 will most often ask about.
Settling it is now the whole of what stands between `UNAUDITED_TOOLS` and empty.

**3. The Gmail rulings are scope-shaped, and the scope is a config-time decision.** The test
pins the constants, but a *new* mail tool authorised under a wider scope would be a new
ruling, not an extension of these. Anyone adding one should read the 3d section of the
record before reusing `read`.

**4. Nothing relates an effect class to the arguments it was chosen for.** The record now
contains three tools (`fs_write`, `goal_upsert`, `web_fetch`) whose honest class varies by
argument, all three labelled at their worst case. That is safe and lossy, and it is the
first thing an argument-aware design in Pass 8 should take.

**5. Still open, untouched by 3d:** everything in 3b's open questions 1-6 and 3c's 2, 3 and
5 — the detached-run fold, the unmeasured fsync cost (now smaller again), the one bit the
journal cannot reproduce, worker attribution on effect events, and the two-attempts-both-run
property that Pass 4 exists to stop.

---

## Pass 3/4 boundary — the two owed rulings, settled by Dylan

Sessions 3c and 3d ran autonomously under the standing policy in
`docs/plans/orchestrator-prompt-auto.md` and left two rulings owed to a human. Both were
put to Dylan at the pass boundary, before Pass 4 was dispatched, and both are recorded here
rather than being folded silently into the 3c/3d sections above — the sessions did not make
these calls and the record should not read as though they did.

**`memory_search` → `read`, as a deliberate exception.** Not a clean fit: `access_count` is
non-idempotent and does not converge on replay, so the tool does not strictly satisfy
`read`. Ruled `read` because the drift is bookkeeping rather than state the system's
correctness depends on, and because escalating the runtime's most-called tool would make
resume prompt on memory lookups — which trains the user to blind-confirm and destroys the
value of `uncertain` for the cases that matter.

The ruling was made conditional on one check, which was run before it was committed:
**does `access_count` feed retrieval ranking or scoring anywhere?** It does not.
`access_count` and `last_accessed_at` are written at `db/repo_memory.py:286` and read
nowhere in `src/`. The retrieval score at `memory/retrieval.py:710` is
`0.60·rrf + 0.15·recency + 0.10·importance + 0.10·confidence + 0.05·prior`, with no access
term; `recency` reads `valid_from`/`recorded_at`/`created_at` (`_claim_when`), never
`last_accessed_at`. Had the counter fed ranking, the ruling would have been
`idempotent_write` with a counter reset on reconcile, because replay-inflated counts would
bias what memory surfaces later.

`UNAUDITED_TOOLS` is now empty. It is kept as an empty frozenset with its test asserting
equality, not deleted: emptiness is the invariant, and a new tool added with the
`UNAUDITED` placeholder must still fail that test.

**`web_fetch` → `unsafe_write` confirmed.** 3d's reversal of 3c's expectation stands. The
implementation was re-read at the ruling to confirm there was nothing worse in it than the
argument assumed: `builtin_web.web_fetch` issues `client.get` only, with up to 5 manually
re-checked redirect hops, no POST anywhere, no `Authorization` header, and a fresh
`httpx.AsyncClient` per call so no cookie jar or credential is carried across calls. It is
safe by method and unauthenticated. That does not change the class, because the argument
for `unsafe_write` was never about the method — it is about argument provenance. Rate
limits and cost are real but they are not correctness and they do not belong in
`effect_class`.

### deferred item — one finding, for Pass 10 tuning

Filed here rather than against Pass 8. Pass 8 is tool-surface reduction and this has
nothing to do with where a tool lives.

> **`effect_class` is a property of the tool; effect risk is a property of the call.**
> `memory_search` is non-idempotent but harmless. `web_fetch` is safe by method and unsafe
> by argument provenance. Both were forced into a per-tool class that cannot express the
> distinction. Candidates for later: a fourth class, a per-call annotation, or
> classification functions that take `canonical_args`. **Do not decide now** — collect
> entries until Pass 10 and decide against real traces.

Every tool that breaks this way from here on is appended to that list. Current entries:

| tool | class it was forced into | what the class cannot express |
|---|---|---|
| `memory_search` | `read` | Non-idempotent counter drift, judged harmless. |
| `web_fetch` | `unsafe_write` | Safe by method; unsafe only for some URLs, by provenance. |

This supersedes 3d's open question 4, which proposed Pass 8 as the home for argument-aware
classification. The three tools it names (`fs_write`, `goal_upsert`, `web_fetch`) are the
same observation; `fs_write` and `goal_upsert` belong on the list above if a later session
confirms they break the same way.

**Appended at the Pass 4/5 boundary by Dylan — the same problem one level down, at the
ledger rather than at the registry.** 3b ruled that a call which returned a failure did not
produce its effect, and the whole recovery path inherits it: `Disclosure.failed` and 4c's
`effect_failed → blocked` mapping both rest on it.

> **A failure response does not prove the effect did not land.** A fetch can fail after the
> server acted — which is precisely why `web_fetch` is `unsafe_write` in the first place.
> `status="failed"` is a statement about what came back down the wire, and the ledger reads
> it as a statement about the world.

This is the same shape as the two entries above: a property of the *call* (did anything
actually happen out there) collapsed into a property of something coarser (what the
response said). It goes in the same collection and is decided at Pass 10 against real
traces, not now.

| where | what it asserts today | what it cannot know |
|---|---|---|
| `ledger` / 3b | a `failed` effect produced no effect | whether the failure arrived before or after the remote side acted |

**What changed immediately, and what deliberately did not.** 4d's fork disclosure no longer
infers anything from a failure — it now reads `(1 web_fetch call after that point returned
an error.)` instead of `…returned a failure, so it changed nothing outside`. One rule for
every tool, so there is no per-class branch in the renderer. The ledger's own `failed` state
and 4c's `effect_failed → blocked` mapping are **unchanged**, because changing them is a
behaviour change and this is a collection entry, not a ruling.

### requirements this places on Pass 4, binding rather than suggested

Both come from the same reasoning that produced the `memory_search` ruling: a prompt the
user cannot act on is worse than no prompt, because it trains blind confirmation.

1. **4b/4c: the reconciliation prompt for an orphaned `web_fetch` must show the URL.**
   "Confirm this fetch?" with no URL trains the user to hit yes, and a blind-confirmed
   one-click link is the exact outcome this ruling exists to prevent. This is a
   requirement on 4c's user-facing wording, not a suggestion.
2. **4b: if a resume has several orphaned fetches, group them into one prompt** rather
   than one prompt each. Same reasoning.
