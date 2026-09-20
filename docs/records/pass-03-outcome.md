# Pass 3 — Effect Class & Effect Ledger — outcome

Sessions completed: **3a**. 3b (ledger and key derivation) and the two audit sessions 3c/3d
are untouched; the pass's exit criteria are **not** met, and cannot be until 3b writes the
ledger. What 3a delivers is the first half of the first criterion: every registered tool
has an `effect_class`, and a tool that does not have one cannot be registered.

The word that matters in the rest of this record is **unaudited**. Every one of the 26
builtin tools now declares `unsafe_write`, and *nobody has judged a single one of them*.
That is what 3c and 3d are for, and the session ledger marks both as human-run. The
declaration is a placeholder with a loud name, not a classification — see the deviation
section, which is the most important part of this record.

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
