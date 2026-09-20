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
