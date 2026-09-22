# Pass 5 — Context Handoff — outcome

Sessions completed: **5a, 5b**. 5c (cold resume) is not started, so the pass's exit criteria
are not yet met. Each session's own exit is:

- **5a** — "threshold crossing fires reliably in a forced-long run, with enough room left to
  produce a handoff without operating at the edge of the window" — met, against a ceiling
  that is **not** the one the pass file's `~8k remaining` implies at first reading.
- **5b** — "a forced handoff preserves task continuity across the boundary... including the
  two near-context-limit tasks" — met for both near-limit tasks against the real model, with
  one of them needing no forcing at all, and with an honest account below of where the
  continuity it preserves is thinner than the objects make it look.

Each choice is the substance of its session and is argued in full below.

---

## Session 5a — Threshold monitoring

### what shipped

| file | what it is | lines |
|---|---|---|
| `src/agentd/agent/budget.py` | the reading: ceiling resolution, the estimate, the basis | 157 |
| `src/agentd/config.py` | `HandoffConfig` + one field on `Config` + a load-time check | +43 −1 |
| `config/default.toml` | the `[handoff]` block | +16 |
| `src/agentd/journal/events.py` | `CONTEXT_BASES`; five fields on `agent_finished` | +23 |
| `src/agentd/agent/loop.py` | the per-step reading, the peak, and the `handoff` boundary | +44 |
| `src/agentd/journal/__init__.py` | exports `CONTEXT_BASES` | +2 |
| `tests/conftest.py` | `agentd.agent.budget` in the monkeypatch list | +1 −1 |
| `tests/test_context_budget.py` | 16 tests | 317 |
| `tests/test_journal_{events,feed}.py`, `tests/test_resume.py` | hand-built `agent_finished` payloads gain the five fields | +12 |

Suite 751 → 767 passing. `.venv/bin/ruff check src tests scripts` clean. No tool was added,
removed or re-classified; no effect class changed; the `[checkpoints]` flag still ships
`false` and with it off a turn takes the same checkpoints it took before this session (which
is `test_the_crossing_is_recorded_even_though_checkpoints_ship_switched_off`).

### the threshold, and how remaining tokens are accounted

**The two findings handed to this session were checked against the code and the live data.
Both hold.**

**1. Provider token accounting is dead, and could not have carried this.**
`llm/openai_compat.py` does ask for it (`stream_options={"include_usage": True}`) and does
read `chunk.usage`; nothing arrives, so `usage` stays `{}` and `usage_reported` is `False`.
Counted rather than assumed:

* `agent_finished` in `~/.local/share/agent/journal.db` (copied with its `-wal`): **5 of 5
  rows** carry `usage_reported: false` and `{"input_tokens": 0, "output_tokens": 0}`.
* `actions`: 5,087 rows. `tokens_in > 0` on **12**, every one of them on 2026-09-17, and
  every `llm_call` and `turn` row since — 09-20 and 09-21 included — is 0.
* Telemetry: 33 recorded turns across three files, `usage.reported` false on all 33.

A threshold built on that reads every conversation as empty and never fires, and its tests
pass, because 0 consumption is indistinguishable from a quiet run.

**What is used instead: `ids.estimate_tokens` (len/3.2) over the assembled message list,
through `obs/telemetry.messages_tokens`, which counts message content *and* tool-call
arguments.** It is an estimate and nothing in this session presents it as anything else:
`agent_finished.context_basis` is to `context_tokens` exactly what `usage_reported` is to
`usage`, with three values — `estimate`, `provider` (reserved, written by nobody today) and
`unmeasured`. One estimator, one number: the journal's `context_tokens` and telemetry's
`context_tokens.peak` are the same function over the same list, asserted in
`test_the_journal_and_the_telemetry_record_agree_about_how_full_a_turn_was`.

**2. The ceiling is `agent.history_tokens` = 24,000, not `llm.max_context_tokens` =
262,144.** The pass file does not settle this and the numbers do:

| | tokens |
|---|---|
| largest prompt ever recorded on this machine (33 turns, all telemetry) | **6,251** |
| median | 1,936 |
| largest real conversation history in the archive (14 messages) | 1,993 |
| structural maximum this runtime can assemble: 24,000 history + 12 × 8,000 chars of tool output | **≈ 54,000** (+ system block) |
| `llm.max_context_tokens` | 262,144 |
| prompt needed to leave 8,000 remaining of 262,144 | 254,144 — **unreachable** |

`262144` is used by nothing in `src/` outside `config.py`. A threshold against it cannot
fire, so it would have shipped green and dead — this codebase's signature failure in a new
place. `agent.history_tokens` is the only ceiling the runtime *enforces* on what carries
forward: `context.history_messages` walks a session newest-first until the budget is spent
and silently drops the rest, which is precisely the loss a handoff exists to replace with
something deliberate. The within-turn ceiling (`max_steps` × `tool_result_max_chars`) is
enforced by `max_steps` ending the turn as `abandoned`, and produces nothing that persists.

**The pass file's default of 8,000 is kept, because against 24,000 it is right.** It fires
at 16,000 used — two thirds of the budget, about 10,000 tokens before `history_messages`
begins dropping turns, and roughly 2.5× the largest prompt ever observed. Replayed against
every turn this machine has recorded: **0 of 33 would have crossed**, closest approach
17,749 remaining. So it neither never-fires nor fires-constantly; it fires on a genuinely
long conversation, which is what the forced-long test builds.

`ceiling()` returns the number **and where it came from** (`history_tokens` | `configured` |
`model_window`), and the model window wins when it is the smaller of the two — so a ceiling
nobody could actually send is never reported against.

### what deviated from the plan, and why

**1. The reading is of the whole assembled prompt, not of the history alone.** Tool results
and the model's own `arguments` blobs occupy the window while a turn runs; a reading that
ignored them would call a turn roomy at the moment it was least so. The honest consequence,
recorded rather than smoothed over: a single tool-heavy turn (six full-size tool results ≈
15,000 tokens) can cross the threshold on a short conversation. That is a true statement
about the prompt and a *false* signal about the conversation, and 5b must read
`context_crossed` beside the turn's tool count rather than as "this conversation is long".

**2. `agent_finished` gained five required payload fields.** The alternative — an eighteenth
event type — is a vocabulary change in a session whose job is measurement, and the drift
guard exists to stop exactly that. The alternative *within* the vocabulary was
`handoff_started`, whose `context_tokens` / `ceiling_tokens` slots are shaped for this; it
was rejected because emitting a handoff that never happens leaves every crossing looking
like an interrupted handoff to a fold. Required rather than optional, because an absent
field is indistinguishable from a turn nobody sized, and the whole point of this session is
that the two are different. The cost is real and is paid in four places: the one emitter,
and three test files that hand-build `agent_finished` payloads.

**3. That makes the five pre-5a `agent_finished` rows in the live journal fail
`validate_payload`.** Checked on a copy: 61 events, 5 rejected, all of them `agent_finished`
written before this session. **Nothing in `src/` re-validates a stored event** — validation
is a write-time gate in `RunJournal.emit` — so all nine runs in that file still fold, with
unchanged statuses and message counts (`resume.plan` was run against all nine). There is no
payload-schema version to bump and no migration to write, so the boundary is a date rather
than a number: anything that re-validates history has to know that turns before 2026-09-21
carry no context fields. Flagged as an open question rather than papered over by making the
fields optional.

**4. 5a gives the `handoff` checkpoint trigger its producer.** The pass file leaves this
open; 4a left the trigger accepted, tested and written by nobody. A crossing that fires and
records nothing is worth less than one in the journal, so the first crossing of a turn calls
`checkpoint_at("handoff", ...)`. It is a marker, not a handoff: `handoff_object` stays NULL,
which is 5b's slot. With `[checkpoints] enabled = false` — the shipped default — this
returns `None` and nothing is written, which is why the reading also lands on
`agent_finished` regardless of the flag. A worker's crossing marks nothing, because
`checkpoint_at` reads the run as mid-worker and declines; that is 4a's "workers are the unit
of atomicity", inherited rather than special-cased.

**5. A misconfigured threshold is refused when the config loads.** `threshold_tokens >=` the
effective ceiling would be crossed on the first step of every conversation, which looks
exactly like a monitor that works. A `model_validator` on `Config` raises instead — cross-
section, because the ceiling can come from `[agent]` or `[handoff]` and the model window
caps both.

**6. Nothing consumes the crossing, deliberately.** No handoff is generated, no
`handoff_object` is touched, `notice()` / `prompt()` / `closing_messages()` are untouched and
still have no runtime consumer, and no user-facing string changed. `journal/render.py` gained
no line for the crossing either: the renderer is a frontend surface and the wording review is
Dylan's, not this session's.

### what is now true about the code that was not before

- **The runtime knows how full its context is, and says so on every turn.** Every
  `agent_finished` now carries the peak prompt size, the ceiling it was measured against, the
  threshold in force, whether any step crossed it, and how the number was obtained. Before
  this session the only record of context size was the telemetry JSONL, which is a
  measurement path that swallows its own write failures.
- **The orchestrator is not involved.** The reading is taken in `AgentLoop.run_turn` from the
  prompt about to be sent; no message is appended, no tool is exposed, and the message list
  is byte-for-byte what it would have been.
  `test_the_model_is_never_asked_to_watch_its_own_context` asserts against the prompt rather
  than against the code, and breaks if anything is injected.
- **"Nobody counted" is a state.** `context_basis = "unmeasured"` with `context_tokens = 0`
  is what a turn that died before assembling a prompt records — the 0 is not a measurement,
  and a fold can tell it from a turn that sent an empty prompt.
- **Overshoot is negative, not zero.** `remaining_tokens` is signed. A 30,000-token prompt
  against a 24,000 ceiling reports −6,000 rather than "full", because clamping is the same
  shape as the plausible NULL this runtime keeps producing.
- **The `handoff` checkpoint trigger has a producer** for the first time since 4a defined it,
  and it fires once per turn rather than once per step.
- **Nothing about agent behaviour, prompts, the tool surface or the policy decisions
  changed.** Same messages, same tools, same archive rows, same checkpoints with the flag off.

**Mutation-checked rather than trusted for being green.** Eleven mutations; nine caught
immediately, two survived the first round and both were read twice:

- ceiling ← `llm.max_context_tokens` → 7 failures (the crossing tests stop crossing).
- `remaining_tokens` clamped at 0 → caught by the overshoot test.
- the boundary fires on every crossing rather than the first → caught (two `handoff`
  checkpoints).
- an unmeasured turn records `read_messages([])` (estimate, 0) instead of `unmeasured` →
  caught.
- the model-window floor removed → caught.
- the config validator removed → caught.
- the crossing appends a system message telling the model to prepare a handoff → caught by
  the *Must not* guard.
- `checkpoint_at` ignores the flag for `handoff` → caught by the flag-off test.
- the boundary is marked `manual` instead of `handoff` → caught.
- **survivor 1, a real gap:** `crossed` with `<` instead of `<=`. Equality at exactly 8,000
  remaining was untested, so the threshold's stated value was not provably the one it used.
  `test_a_reading_exactly_at_the_threshold_counts_as_crossed` now pins both sides of the
  line, and the mutation is caught.
- **survivor 2, redundancy that was worth removing anyway:** reporting the last reading
  instead of the peak. Observationally identical today, because `run_turn` only ever appends
  to its message list — which is the very property that makes handoffs necessary. A unit test
  on `_TurnRecord.context_seen` with a shrinking sequence now fixes the intended meaning, so
  the day something trims mid-turn, "how close did this turn come" does not quietly become
  "how much was left at the end".

**Live-data checks (the house rule: read the real rows).** A copy of the live journal *with*
its `-wal` (61 events, 9 runs, `user_version = 1`), the `actions` table (5,087 rows), the
archive's `raw_events` (largest session: 14 messages, 1,993 estimated tokens) and all three
telemetry files (33 turns). Numbers are in the section above. No live turn was run and
nothing was written to the live journal, the live memory store or Postgres.

### schemas exactly as implemented

```python
# agent/budget.py
ContextReading(
  used_tokens: int,        # estimated; ids.estimate_tokens over the assembled prompt
  ceiling_tokens: int,
  threshold_tokens: int,
  ceiling_source: str,     # history_tokens | configured | model_window
  basis: str = "estimate", # estimate | provider | unmeasured
)
  .remaining_tokens -> int   # signed: ceiling - used, never clamped
  .crossed          -> bool  # remaining <= threshold  (at the line counts as crossed)
  .measured         -> bool

ceiling(cfg) -> (int, source)          # model window wins when it is smaller
threshold(cfg) -> int
read_messages(messages, *, cfg) -> ContextReading
unmeasured(cfg) -> ContextReading      # used_tokens 0, basis "unmeasured"
```

```
agent_finished   ... unchanged fields ...
                 context_tokens: int             the peak prompt this turn assembled
                 context_ceiling_tokens: int
                 context_threshold_tokens: int
                 context_crossed: bool           any step of this turn crossed
                 context_basis: str ∈ {estimate, provider, unmeasured}
```

`CONTEXT_BASES` is declared in `journal/events.py` beside `EFFECT_CLASSES` and
`CHECKPOINT_TRIGGERS`, and `agent/budget.py` imports it rather than repeating it, so the enum
and the values written cannot drift apart. `EVENT_TYPES` and `EMITTED_TYPES` are unchanged —
still seventeen types, fourteen emitted — and no journal schema version moved: the `journal`
table, the `effect` table and the `checkpoint` table are untouched and `SCHEMA_VERSION` is
still 3.

Config, as it now exists:

```toml
[handoff]
threshold_tokens = 8000      # remaining tokens at or below which the context is exhausted
# ceiling_tokens = 24000     # unset means agent.history_tokens; the model window still
                             # wins when it is the smaller of the two
```

`Config` refuses to load when `threshold_tokens >= min(ceiling, llm.max_context_tokens)`.

**Where the reading is taken:** `AgentLoop.run_turn`, once per step, immediately after
`tele.context_sample(messages)` and before `provider.stream` — the prompt about to be sent.
`_TurnRecord.context_seen` keeps the peak, remembers the crossing, and returns `True` exactly
once per turn, on the first crossing, which is what fires the boundary.

| trigger | call site | fires when |
|---|---|---|
| `handoff` | `agent/loop.py`, in the step loop | the first step of a turn whose prompt leaves ≤ `threshold_tokens` |

(The other four are unchanged from 4a.)

### deferred items, and where they went

- **Handoff generation, the `handoff_object` slot, `handoff_started` / `handoff_finished` —
  5b.** All still written by nobody. The two optional fields `handoff_started.context_tokens`
  and `.ceiling_tokens` are exactly `ContextReading.used_tokens` and `.ceiling_tokens`; 5b
  should fill them from the reading rather than re-deriving them.
- **Cold resume, `WARM_WINDOW`, the message budget — 5c.** Untouched. `[handoff]` is where
  those keys belong.
- **A human line for the crossing.** `render.py` renders `checkpoint_written` generically and
  `agent_finished` only when it failed; no new wording was invented, because 4c's and 4d's
  wording is still unreviewed and the frontend surface is under that review.
- **Telemetry.** The telemetry record's `context_tokens` block was left as it was (start /
  peak / end) and did not gain the ceiling or the crossing. The journal is the source of
  truth for the decision; duplicating it into a measurement path is how two records of one
  number start to disagree.
- **`usage`'s own repair.** Fixing the router so a real token count exists is still nobody's
  session. `context_basis = "provider"` is the slot it lands in.

### open questions for later passes

**1. A tool-heavy turn can cross without the conversation being long. → 5b.** The reading is
of the whole prompt (deviation 1). `context_crossed` therefore answers "is this prompt near
the ceiling", not "is this conversation too long to carry". 5b decides what a handoff is
*for* in that case: the in-turn portion does not persist, so handing off because of it would
compress a context that was about to shrink on its own.

**2. The five pre-5a `agent_finished` rows no longer validate.** Nothing reads them that way
today (deviation 3). The first tool that re-validates history — an exporter, a fsck, a Pass
10 analysis — needs either a payload-schema version on the event or an explicit "before
2026-09-21" branch. Recorded here so it is a decision rather than a surprise.

**3. The threshold has never fired outside the suite.** Zero of 33 recorded turns come within
17,749 tokens of it, and the longest real conversation on this machine is 1,993 tokens — 8%
of the ceiling. The forced-long test builds its own history. Until a real conversation gets
long, the crossing path's only evidence is synthetic, and 5b should not treat "it fired in a
test" as "it fires when it should".

**4. A crossing on a worker's turn marks nothing.** `checkpoint_at` declines mid-worker, so a
sub-agent that fills its own context leaves only the `agent_finished` fields behind. Correct
under 4a's atomicity rule and wrong-feeling enough to name: Pass 6 (parallel workers) revisits
the same refusal from the other side.

**5. `estimate_tokens` has never been calibrated against this model's tokenizer.** len/3.2 is
a guess that has been consistent with the only provider counts that exist (12 rows from
2026-09-17, 1,568–5,936 input tokens against prompts the estimator would have put in the same
range), but nothing has compared the two on the *same* prompt, because no turn has ever
carried both numbers. If the ratio is off by 30% the threshold is off by 30%, and every test
still passes.

**6. Still open, untouched by 5a:** power-loss durability is reasoned rather than measured
(2a #2); a retrieval-degraded turn is invisible to a fold (2c #1); a tool call outside a turn
is journaled by nobody (2b #2); tool events are emitted by `loop.py` rather than the executor
(2b #3); the fsync cost is unmeasured (2a #3, 4a #4); `open_workers[]` has never been
non-empty (4a #1); a checkpoint of a turn is not a checkpoint of a session (4a #2). And from
Pass 1, token accounting is still dead upstream — which is why `context_basis` exists.

---

## Session 5b — Handoff generation and the fresh orchestrator

5a taught the runtime to notice. This session is what happens after the crossing: an object
that survives the conversation, a successor started from it, and a decision 5a recorded and
left open about *which* crossing a handoff is for.

The pass's 5b exit — "a forced handoff preserves task continuity across the boundary on at
least three tasks from the Pass 1 suite, including the two near-context-limit tasks" — is
met for the two near-limit tasks against the real model, and B23 needed no forcing at all.
The forced objects are below, verbatim.

### what shipped

| file | what it is | lines |
|---|---|---|
| `src/agentd/agent/handoff.py` | the object, its validator, the generator, the successor's block | 667 |
| `src/agentd/agent/loop.py` | the decision, the generation call site, `Session.handoff` | +132 −2 |
| `src/agentd/agent/budget.py` | `carried()` - the second reading | +53 |
| `src/agentd/agent/context.py` | `after_id` on the history window, `handoff_block` on the system message | +24 −4 |
| `src/agentd/db/repo_archive.py` | `recent_message_sizes()`, `after_id` on `recent_messages` | +31 −3 |
| `src/agentd/ids.py` | `estimate_tokens_for_chars` - one estimator, over a length | +11 −1 |
| `src/agentd/journal/checkpoints.py` | `handoff_object` on `write()` and `checkpoint_at()` | +26 −9 |
| `src/agentd/journal/events.py` | `handoff_started` / `handoff_finished` move into `EMITTED_TYPES` | +14 −6 |
| `src/agentd/config.py`, `config/default.toml` | five `[handoff]` keys and `[llm.roles.handoff]` | +40 |
| `tests/test_handoff.py` | 33 tests | 593 |
| `tests/conftest.py` | `agentd.agent.handoff` in the monkeypatch list | +1 |
| `tests/test_context_budget.py`, `test_checkpoints.py`, `test_journal_events.py` | three amendments, below | +24 −12 |

Suite 773 → 806 passing. `.venv/bin/ruff check src tests scripts` clean. No tool added,
removed or re-classified; no effect class changed; no user-facing string added or changed,
and `observations.py` is untouched except as an import (`RUNTIME_PREFIX`).

**Three existing tests were amended, all three because 5b made them describe something that
is no longer true rather than because they were in the way:**

1. `test_the_model_is_never_asked_to_watch_its_own_context` counted system messages across
   *every* call the fake provider received. A handoff generation is a second kind of call to
   the same provider, under its own role, and is about the handoff by construction. It now
   counts the streamed calls - the turn's own prompt - which is what the *Must not* is about.
   Left alone it would have been a test that can only pass while no handoff is ever
   generated.
2. `test_the_types_nothing_writes_yet_are_named_rather_than_left_implicit`: the unwritten set
   is now `{tool_progress}` alone.
3. `test_checkpoints.py`'s module docstring said four slots are inert. Three are.

### the decision 5a handed forward, and what it is

**`context_crossed` answers "is this prompt near the ceiling". A handoff is generated on a
different reading: "is this conversation too long to carry".** 5a recorded the difference
rather than deciding it; this is the decision, and it is the substance of the session.

`budget.carried()` sizes only the messages that will exist on the next turn - the archive's
`user_message` and `assistant_message` rows, which is exactly what `history_messages` replays
and exactly what `agent.history_tokens` is spent on. Everything else in a prompt is in-turn:
tool results, the model's own `arguments` blobs, `FINAL_NUDGE`, the system block that is
rebuilt from scratch every turn. That material is bounded by `max_steps` x
`tool_result_max_chars` and is gone by the next prompt.

So a turn can cross on the prompt with a two-message conversation behind it, and handing off
for that would be the pass file's third *Must not* - the lossy path taken where the lossless
one fits. It would compress a conversation that was about to shrink on its own, and pay a
model call to do it.

**Both readings are kept and both are journaled.** 5a's prompt crossing still sets
`agent_finished.context_crossed` and still marks the `handoff` checkpoint; nothing 5a shipped
changed. What a `handoff` checkpoint with a NULL `handoff_object` means is now three things,
and the journal separates them:

```
handoff checkpoint, no handoff_started      the prompt was full, the conversation was not
handoff_finished status=failed              generation was tried and did not produce one
handoff_finished status=ok, object stored   a handoff exists
```

Measured on the live runs below: B22's prompt reached 13,484 estimated tokens while what it
carried was 910. B23's carried reading reached 17,566 of 24,000 on its third turn, which is
the crossing that produced a handoff.

### the schema, exactly as implemented

```python
# agent/handoff.py
HandoffDraft(BaseModel)        # the NINE fields the model is asked for; every one defaulted
  task, user_intent, current_state: str = ""
  decisions_made, constraints, completed_actions,
  relevant_evidence, unresolved_questions, next_actions: list[str] = []

Handoff(frozen dataclass)      # the ELEVEN fields the pass file names, plus provenance
  task, user_intent, current_state: str
  decisions_made, constraints, completed_actions, active_subagents,
  relevant_evidence, unresolved_questions, next_actions,
  important_memory_refs: tuple[str, ...]
  handoff_id, run_id, reason, created_at: str
  session_id: str | None
  watermark: int | None        # archive id; everything at or below it is replaced
  source: dict                 # provenance, below
  .as_dict() / .from_dict()    # from_dict raises on a handoff_schema it was not written for
```

Stored as `checkpoint.handoff_object`:

```json
{
  "handoff_schema": 1,
  "<the eleven fields>": ...,
  "handoff_id": ..., "run_id": ..., "session_id": ..., "reason": "context_threshold",
  "created_at": ..., "watermark": 5,
  "source": {
    "messages_read": 7, "synthetic_excluded": 0, "system_excluded": 1,
    "unflagged_runtime_text": 0, "dropped_messages": 5, "dropped_chars": 42198,
    "carried_tokens": 17566, "ceiling_tokens": 24000, "basis": "estimate",
    "model": "Qwen/Qwen3.8-27B-FP8", "supersedes": null
  }
}
```

`handoff_schema` exists because of 5a's open question 2: five `agent_finished` rows stopped
validating when a session added required fields and nothing could say which rules a given row
was written under. `Handoff.from_dict` refuses a version it was not written for rather than
reading unknown fields as absent ones.

**Validation rules, and what a failure does.**

```
task, user_intent, current_state   must be non-blank
next_actions                       at least one non-blank entry
unresolved_questions               at least one non-blank entry
every list                         stripped; blank entries dropped before the check
reason                             must be in REASONS
```

The pass file's line verbatim: a handoff missing `next_actions` or `unresolved_questions` is
a failed handoff, not a partial one. **An empty list is missing.** It is not accepted as
"there were none", because "no next action" from a generator that was asked for next actions
is indistinguishable from a generator that skipped the field, and that is this codebase's
recurring bug standing in the one place where the evidence it replaced is already gone. The
instruction tells the generator what to write when there is genuinely nothing open - one
entry saying so and why - which is a claim a successor can read. `[""]` fails too.

**No cross-field validator is on the pydantic schema, deliberately.** `complete_json` gets
one repair attempt inside the provider and then raises `LLMError`; a rule there would spend
that attempt on something this module states far better afterwards and would raise into the
middle of somebody's turn. So the schema decodes and `problems()` decides, and the repair is
this module's: the generator is shown the named problems and asked again, once. A second
invalid draft fails the handoff.

A failure is journaled (`handoff_finished status="failed"`, with the problems as `error`),
`handoff_object` stays NULL, `session.handoff` is not set, and the turn is untouched - the
user's answer is already streamed by then. **There is no fallback**, which is the pass file's
second *Must not*: nothing here produces a thin object anyway and nothing copies the old
context into the successor when generation looks weak.

### requirement A - how synthetic text is kept out, not that it was

Dylan's requirement at the Pass 4/5 boundary: *messages with `synthetic=True` are never
summarized or promoted as fact.* The generator reads a message list, so this is enforced in
`handoff.source()`, which returns what may be summarised **and a count of everything that may
not**, and that count is stored in the object:

1. **The flag, which is the mechanism.** `bool(message.get("synthetic"))`, plus the
   `tool_call_id`s of the `ClosingMessage`s the caller was given. The flag rather than the
   `RUNTIME_PREFIX` string, which is the second and independent signal - and the message
   dicts themselves are never mutated, because they go to the provider and must stay exactly
   what the API accepts, so the flag is carried as ids at the call site.
2. **Every `system` message.** This one is not in the requirement and belongs to it:
   `FINAL_NUDGE` and `STUCK_NUDGE` are the runtime talking to the model, they carry no flag
   and never will because they are not tool results, and a summary that read them would
   report "the user said to stop calling tools".
3. **Anything still carrying `RUNTIME_PREFIX`** after those two. Unreachable today; if a
   caller ever loses the flag it is excluded *and counted* into `source.unflagged_runtime_text`
   in the stored object, rather than dropped into a log nobody reads.

Four tests hold this down and all four catch the mutation that removes the rule. `source` is
also where the honest arithmetic lives: `messages_read` is what the generator actually saw.

**Also checked, per the `unreported` ruling at the Pass 4/5 boundary:** nothing renders that
status word without its evidence beside it. The only producers are `observations.notice()`,
which prints `N unreported - the call is recorded as having happened and its result did not
survive` on one line, and `CLOSING_TEXT[UNREPORTED]`, which states the evidence and never the
word. `journal/render.py` and the CLI render no observation status at all.

### requirement B - it is not mine, and why

Dylan's other requirement binds "whichever Pass 5 session first consumes `notice()`". **This
session consumes neither `notice()` nor `closing_messages()`, so the guard falls to 5c**, and
that is a fact about the code rather than a scheduling preference: a threshold handoff has no
interrupted call in it. It happens at the end of a turn that completed, inside a live process,
with the full message list in hand. There is no orphaned effect, no uncertain observation and
nothing to re-run - the set the guard needs ("the uncertain observations the resumed turn was
handed") does not exist on this path. The only import from `observations.py` in the whole
session is `RUNTIME_PREFIX`.

5c extends the resume path, which is where `notice()` gets its first consumer and where the
guard belongs. It is unbuilt and still owed.

### the successor, and what "the old context is not copied" is enforced by

A handoff is generated at the end of the turn that crossed, and takes effect on the next one.
The successor is not a new process - it is the next turn of the same session, assembled from
scratch:

```
system   = system_prompt(cfg, autonomy, context_block) + render(handoff)
history  = history_messages(session, budget, after_id=handoff.watermark)
user     = this turn's message
```

One system message, not two: the shape of the list stays what it always was, so nothing
downstream has to learn a second shape. The tools are whatever `registry.select` offers this
turn, which is the pass file's "currently relevant tools" already being true.

**The old context is not copied because this function is never given it.** `after_id` is a
watermark on the archive's own identity column; `history_messages` asks for the rows above it
and the rest is not read. That is structural rather than a rule somebody has to remember, and
the mutation that drops `after_id` is caught by two tests.

**Where the watermark goes is bounded by tokens first and by count second**, and that order
was a finding rather than a design: the first live B23 run kept `carry_messages = 4` messages,
two of which were 14,000-character pastes, and the successor's next prompt came back at 11,598
estimated tokens. A handoff that costs a model call and frees no room is a handoff that did
nothing, and every number about it still looks healthy. With `carry_tokens = 2000` binding
first, the same run's successor came back at 2,079. A message too large for the window is not
carried at all - it is in the object, which is what the object is for.

**The successor is told three things about the block, and all three are derived:**

- that it is a compression, written by the runtime and not said by the user, and that the
  original messages are not available;
- how much was taken away - *"5 earlier messages (42,198 characters) were replaced by this
  summary and their text is gone"* - computed from the archive rows that fell below the
  watermark, not written by the generator;
- that a question needing what was in them should be answered with "I no longer have it".

The second of those is in because of the first live B23 run, which is in the quality section
below: the general warning was obeyed when the user asked what they had *said* and ignored
when they asked about the material they had pasted. The quantity changed that behaviour.

### the generator: model, prompt, and what one costs

| | |
|---|---|
| role | `[llm.roles.handoff]` - temperature 0.2, `thinking = false`, `max_tokens = 2048` |
| model | `Qwen/Qwen3.8-27B-FP8` through the SIR router, the same endpoint as everything else |
| call | `complete_json(HandoffDraft)`, guided JSON, plus at most one repair from this module |
| input | system: `INSTRUCTION`; optional `PREVIOUS_HEADING` + the handoff in force; the transcript |
| transcript | per message `excerpt_chars = 1500`, in total `source_chars = 48000`, newest first |
| observed cost | **65.4 s** (B23, 7 source messages) and **85.9 s** (B22, 16 source messages) |
| token cost | **unknown and unknowable today.** The router drops the `usage` chunk; every number in this session is `ids.estimate_tokens` and says `basis: "estimate"` |

The 65-86 s is wall clock on a 27B sharing a GPU with whatever else SIR has resident, and it
lands *after* the turn's prose has finished streaming and before the `Answer` event. It is one
call per crossing turn, not per turn: after a handoff the carried reading resets to roughly
the size of the carry window, so the next crossing is genuinely later rather than immediately.

**The generator reads a live message list, which is the reason generation happens inside the
turn.** Session 4b refused to hand the journal's 200-character previews to a model as message
bodies and there is still no `model_messages()`; `actions.output.text` holds 500. Inside
`run_turn` the full bodies are in memory, so this path never touches that gap. The path that
does - a handoff generated from the journal on a cold resume - is 5c's, and it inherits the
constraint unsolved. Dylan's ruling that mid-turn assistant prose gets archived is what makes
it solvable; **that write is 5c's and this session did not make it.**

### what deviated from the plan, and why

**1. Two of the eleven fields are supplied by the runtime, not asked of the model.**
`important_memory_refs` comes from the retrieval pack's own refs and `active_subagents` from
the runtime's knowledge of open workers. The pass file lists them among the fields to
"collect"; both are in the object, and neither is in `HandoffDraft`. A model-written `[F:01a0]`
in a field the successor will look things up from is the same laundering channel as a
summarised synthetic message, in a smaller font. `active_subagents` is empty by construction
today - a worker is awaited inside the step that created it, so no turn boundary has one open
- and Pass 6 is what gives it content.

**2. A handoff is generated only on a turn that completed.** The `LLMError` path returns early
and generates nothing. Not an oversight: what carries forward is history plus the user's
message plus the answer, and a failed turn has no answer, so the carried reading is what it
was before the turn started and nothing new needs handing off. The cost is real and is named
in the open questions: the one turn shape where a handoff would most obviously help - B22 run
in full, which dies at step 12 on the pre-existing `FINAL_NUDGE` HTTP 400 - is the one shape
this path does not cover.

**3. `handoff_object` is only durable when `[checkpoints] enabled` is on, which is not the
shipped default.** With the flag off, `checkpoint_at` returns None and the object lives only
on the in-process `Session`. The pass file puts the object on a checkpoint and that is where
it is; the consequence is that a handoff survives a *conversation* today and not a *process*,
and the 5c path that regenerates one from the journal is what closes that. Both forced runs
below were taken with the flag on and say so.

**4. `estimate_tokens` was refactored rather than copied.** Choosing a watermark needs to size
a 42,000-character paste that has deliberately not been loaded. `ids.estimate_tokens_for_chars`
is the estimator over a length and `estimate_tokens` is it applied to a string, so there is
still exactly one `/ 3.2` in the system.

**5. `handoff_finished.successor_run_id` is always null.** The successor is the next turn of
the same session and has no run id until it starts. Null is the honest value on this path;
5c fills it where a resume really does open a new run.

**6. Nothing was added to `journal/render.py` and no user-facing string was written.** A
handoff is not announced to the user, by the same reasoning 5a used for the crossing: the
frontend surface is under Dylan's wording review and this session had no sentence it needed to
put in front of a person. The two new strings are model-facing (`INSTRUCTION`,
`RENDER_HEADING` + `DROPPED_LINE`) and are flagged in the report for review anyway.

### what is now true about the code that was not before

- **A conversation can end and its work can continue.** `handoff_object` has a producer, the
  `handoff_*` event pair has a producer, and `EMITTED_TYPES` is sixteen of seventeen.
- **"The context is full" and "this conversation is too long" are different questions with
  different answers, and the journal records both.** Before this session there was one number
  and it was being asked to mean both things.
- **A handoff that cannot be acted on is a failure with a reason in the journal**, not an
  object with an empty field. The validator refuses `[]` and `[""]`, the generator is told
  what was wrong and asked once more, and a second failure stores nothing.
- **Text the runtime wrote can no longer reach a summary.** Three independent exclusions, a
  count of each in the stored object, and the count of unflagged runtime text is non-zero only
  if a caller has a bug - in which case it is visible in the record that replaced the evidence.
- **A successor knows what it does not have, in messages and in characters.** Demonstrated to
  change the model's behaviour on a real task, below.
- **A handoff actually frees room.** 17,566 → 2,079 estimated tokens on B23. The first version
  of this session's carry window freed less than half of that and passed every test.
- **Nothing about the tool surface, the policy path, the effect ledger, resume or fork
  changed.** A turn that does not cross is byte-for-byte the turn it was before.

### the two near-context-limit tasks, forced, against the real model

Both were run end to end through `AgentLoop.run_turn` against the configured local endpoint
(`Qwen/Qwen3.8-27B-FP8` via the SIR router on 127.0.0.1:8000), with `[checkpoints] enabled =
true`, against the scratch `agent_test` database and a throwaway journal. **No live journal,
memory store or Postgres database was written and no external network call was made.**

#### B23 — history budget across turns (near-limit, 2 of 2). Not forced.

The real threshold fired on its own. The task pastes `docs/architecture/tool-call-architecture.md`
(42,086 characters) in three parts and then asks two questions. Per turn, `agent_finished`:

| turn | context_tokens (prompt) | crossed | carried | handoff |
|---|---|---|---|---|
| 1 paste 1 | 9,548 | no | | |
| 2 paste 2 | 13,929 | no | | |
| 3 paste 3 | 18,301 | **yes** | **17,542 of 24,000** | generated, 44.6 s |
| 4 summarise | 1,769 | no | | started from the handoff |
| 5 what did I first say | 2,356 | no | | |

`handoff_finished`: `status=ok, kept_messages=1, dropped_messages=5, summary_chars=2904,
successor_run_id=null`. Checkpoints: one `handoff` (the prompt crossing, `handoff_object`
NULL) and five `turn_end`, the third of which carries the object. Turn 4's prompt is 1,769
tokens against 18,301 - the handoff freed the room it exists to free.

```json
{
  "active_subagents": [],
  "completed_actions": [
    "Received and acknowledged the first part of the tool-call architecture doc (through the beginning of Section 11, Memory Architecture).",
    "Received and acknowledged the second part of the doc (Sections 11 through the start of 17, covering Working Memory, Episodic Memory, and Semantic Memory).",
    "Received and acknowledged the final part of the doc (Sections 17 through 22, including the end-state diagram, direct vs. delegation criteria, and Section 18 Dynamic Tool Loading)."
  ],
  "constraints": [],
  "created_at": "2026-09-21T15:19:24.749411+00:00",
  "current_state": "The full 22-section tool-call architecture document has been received and acknowledged; the assistant is waiting for the user's specific question or request.",
  "decisions_made": [],
  "handoff_id": "01a0c48c-eb4d-73d3-bc51-693ebb3340dd",
  "handoff_schema": 1,
  "important_memory_refs": [],
  "next_actions": [
    "Wait for the user to ask their specific question or request about the provided tool-call architecture document.",
    "Answer the user's question using the full context of the 22-section document provided in the transcript."
  ],
  "reason": "context_threshold",
  "relevant_evidence": [
    "The user pasted a multi-part document titled 'Tool-Call Architecture Plan'.",
    "The document covers 22 sections plus an end-state diagram.",
    "Key architectural goals include: reducing load on the main LLM, preserving access to a large capability surface, runtime-owned execution mechanics, and the main LLM acting as an orchestrator.",
    "Memory Architecture (Section 11) defines three buckets: Working Memory (task-local, short-lived, captured by checkpoints), Episodic Memory (past events/experiences), and Semantic Memory (durable facts/knowledge).",
    "Delegation criteria (Section 17): Direct handling is for simple/predictable tasks; delegation is for complex/iterative tasks; rare capabilities require tool discovery.",
    "Dynamic Tool Loading (Section 18) pipeline: task -> capability description -> semantic tool retrieval -> top-K candidates -> tool-call router -> 3-6 tools -> worker execution.",
    "The user stated they would paste material and then ask about it."
  ],
  "run_id": "01a0c48b-cc78-7741-b954-8822358db2c6",
  "session_id": "01a0c48b-769c-78c0-a001-3716e31dc1a3",
  "source": {
    "basis": "estimate",
    "carried_tokens": 17542,
    "ceiling_tokens": 24000,
    "dropped_chars": 42127,
    "dropped_messages": 5,
    "messages_read": 7,
    "model": "Qwen/Qwen3.8-27B-FP8",
    "supersedes": null,
    "synthetic_excluded": 0,
    "system_excluded": 1,
    "unflagged_runtime_text": 0
  },
  "task": "The user is providing a multi-part tool-call architecture document for review and will subsequently ask questions about its contents.",
  "unresolved_questions": [
    "The user has not yet asked their specific question about the document; the successor must wait for this input before proceeding."
  ],
  "user_intent": "The user wants to share a complete technical architecture document in parts and then have a discussion or Q&A session about it.",
  "watermark": 7
}```

#### B22 — step budget under breadth (near-limit, 1 of 2). Forced, by lowering the ceiling.

**Said plainly: this one was forced and the real threshold would have done nothing.** The
turn's carried reading was **910 estimated tokens**; against the real 24,000 ceiling that
leaves 23,090 remaining, against a threshold of 8,000. It was forced by setting
`[handoff] ceiling_tokens = 1000` with `threshold_tokens = 900`, so a conversation of two
messages crosses. The prompt reading for the same turn was 13,484 - full, and full of
material that does not carry, which is the whole distinction this session ships.

Two further deviations from the baseline row, both stated rather than smoothed over:

- The task was narrowed to name six files and to answer after reading them. Run verbatim it
  reaches `max_steps = 12`, and the last step appends `FINAL_NUDGE` as a mid-list system
  message, which this backend rejects with `HTTP 400 "System message must be at the
  beginning."` - the pre-existing crash the Pass 2 record carries forward. The turn fails, and
  a failed turn generates no handoff (deviation 2). So the shape that was actually exercised
  is breadth-in-one-turn without the step-budget crash.
- The conversation was two messages long, so `carry_window` dropped nothing: `watermark` is
  null, `dropped_messages` is 0, `kept_messages` is 2. A forced handoff on a short
  conversation is purely additive, and the object says so.

```json
{
  "active_subagents": [],
  "completed_actions": [
    "Read builtin_fs.py — succeeded; registers fs_list, fs_read, fs_search, fs_write.",
    "Read builtin_memory.py — succeeded; registers memory_search, memory_remember.",
    "Read builtin_mail.py — succeeded; registers gmail_search, gmail_message.",
    "Read builtin_web.py — succeeded; registers web_fetch, web_search.",
    "Read builtin_shell.py — succeeded; registers shell.",
    "Attempted to read builtin_loops.py — failed: file not found.",
    "Listed the tools directory to confirm available files.",
    "Searched builtin_mail.py for tool registration metadata (tags, always_on, private_output).",
    "Produced a table of tools from the five successfully read files; the table was truncated in the transcript after the web_search row."
  ],
  "constraints": [
    "Read at most six files before answering.",
    "The six files the user named are builtin_fs.py, builtin_memory.py, builtin_mail.py, builtin_web.py, builtin_shell.py, and builtin_loops.py.",
    "builtin_loops.py does not exist in the directory; the closest real file is builtin_delegate.py, which was not read."
  ],
  "created_at": "2026-09-21T15:12:00.958114+00:00",
  "current_state": "Five of the six requested files were read successfully and a table was produced from them; the sixth file (builtin_loops.py) does not exist, and the assistant's final table response was truncated before completing the shell tool row.",
  "decisions_made": [
    "Treated builtin_loops.py as a non-existent file rather than guessing its contents.",
    "Did not read builtin_delegate.py (the closest real file) because the user's constraint was to read at most six files and the six named files were the scope.",
    "Included only tools from the five successfully read files in the table."
  ],
  "handoff_id": "01a0c486-25be-7738-af33-5d8ca2e33c27",
  "handoff_schema": 1,
  "important_memory_refs": [],
  "next_actions": [
    "Re-emit the complete table including the shell tool row that was truncated in the prior response.",
    "Ask the user whether they want builtin_delegate.py (or any other file) read in place of the non-existent builtin_loops.py, or whether the table of five files is sufficient.",
    "If the user confirms, read the additional file(s) and append their tools to the table."
  ],
  "reason": "context_threshold",
  "relevant_evidence": [
    "Directory listing of /home/dylan/Projects/agent/src/agentd/tools/: __init__.py, base.py, builtin_agenda.py, builtin_calendar.py, builtin_coursework.py, builtin_delegate.py, builtin_fs.py, builtin_mail.py, builtin_memory.py, builtin_shell.py, builtin_web.py, effects.py, executor.py, idempotency.py, registry.py. No builtin_loops.py.",
    "builtin_fs.py: fs_list (tags=('fs',), always_on not set), fs_read (tags=('fs',), always_on not set), fs_search (tags=('fs',), always_on not set), fs_write (tags=('fs',), always_on not set).",
    "builtin_memory.py: memory_search (tags=('memory','core'), always_on=True), memory_remember (tags=('memory','core'), always_on=True).",
    "builtin_mail.py: gmail_search (tags=('mail','untrusted'), always_on=True, private_output=True, trust_output=False), gmail_message (tags=('mail','untrusted'), always_on=True, private_output=True, trust_output=False).",
    "builtin_web.py: web_fetch (tags=('web','untrusted','egress'), always_on not set), web_search (tags=('web','untrusted','egress'), always_on not set).",
    "builtin_shell.py: shell (tags=('shell',), always_on not set, effect_class=UNSAFE_WRITE).",
    "The assistant's final table was truncated after the web_search row; the shell row and any closing note were cut off in the transcript."
  ],
  "run_id": "01a0c483-8db4-70ce-82bb-25e1c2e012ef",
  "session_id": "01a0c483-8da7-725f-a663-be8e2acde833",
  "source": {
    "basis": "estimate",
    "carried_tokens": 910,
    "ceiling_tokens": 1000,
    "dropped_chars": 0,
    "dropped_messages": 0,
    "messages_read": 16,
    "model": "Qwen/Qwen3.8-27B-FP8",
    "supersedes": null,
    "synthetic_excluded": 0,
    "system_excluded": 1,
    "unflagged_runtime_text": 0
  },
  "task": "Produce a table of every tool the agent registers, with columns for name, one-line description, tags, and whether it is always on, based on reading the six specified files in /home/dylan/Projects/agent/src/agentd/tools/.",
  "unresolved_questions": [
    "The user asked for builtin_loops.py, which does not exist. It is unclear whether the user meant builtin_delegate.py or another file, and whether they want that file read and included in the table.",
    "The assistant's table was truncated in the transcript; the exact text of the shell tool row and any closing remarks are not recoverable from the transcript, so the successor should re-emit the full table rather than rely on the truncated version."
  ],
  "user_intent": "The user wants a complete, accurate reference table of the agent's registered tools so they can see what each tool does, how it is categorized, and whether it is always available.",
  "watermark": null
}```

### observed handoff quality on the test tasks

**What is good, and it is most of it.** Both objects are specific, attributable and
actionable. B22's carries the exact tool tables it read, names the file that does not exist
(`builtin_loops.py`), records the decision *not* to guess its contents, notes that its own
answer was truncated mid-table, and its `next_actions` start with "re-emit the complete table
including the shell tool row that was truncated". A successor handed that object can finish
the work. B23's names the document, its section structure and its main claims, and both
`unresolved_questions` entries are real rather than filler. Neither object invented a tool
call, and `synthetic_excluded` / `unflagged_runtime_text` were 0 on both because there was
nothing synthetic to exclude - the enforcement is tested, not demonstrated, on these runs.

**What is not good, and it is the finding of this session.** A handoff is a lossy compression
and B23 is the task where the loss lands on exactly what the user wanted kept. Turn 5 - the
row the baseline actually grades - **passes**, in both runs and without hedging away from the
answer:

> I don't have the text of your first message. The earlier part of this conversation was
> compacted into a summary, and the original messages are gone. What the summary tells me is
> that you provided a multi-part tool-call architecture document (22 sections plus an
> end-state diagram) and said you'd paste material and then ask about it. But I can't quote
> your exact words from that first message.

Turn 4 - "summarise the argument for separating checkpoints from handoffs" - **is not
reliable**. Three runs, three behaviours:

1. Before `DROPPED_LINE` existed: a confident, fluent and wrong answer, including
   *"handoff, owned by the orchestrator's delegation decision"* - which contradicts this
   pass's own first *Must not*. No hedge of any kind.
2. With `DROPPED_LINE`: *"I don't have the full text of the sections that would cover this...
   the detailed argument was in a truncated portion I no longer have access to"*, followed by
   what it could still support and an offer to be re-pasted. Correct behaviour.
3. Final run, same code as (2): confident and wrong again - checkpoints as "task-local
   execution state", handoffs as "the worker's job, produced at task completion".

So the quantity in `DROPPED_LINE` moved the behaviour and did not fix it. **The honest
statement is that after a handoff this runtime does not reliably refuse to answer from
material it no longer has**, and the two things that would fix it are both out of bounds
here: keeping the material is the pass file's second *Must not*, and a bigger
`relevant_evidence` cannot hold 42,000 characters in a 2,048-token completion. It is the
first open question below.

One smaller inaccuracy, fixed mid-session: the generator reported *"the document you pasted
was truncated in multiple places (marked `[... N more characters]`)"*, attributing its own
excerpting to the user's material. The markers now say who truncated - *"further characters of
this message were not shown to the handoff generator"*.

### mutation testing

Sixteen mutations, one at a time, against `tests/test_handoff.py`, `test_context_budget.py`,
`test_checkpoints.py` and `test_journal_events.py`. **Fifteen caught, one survivor, read
twice and invalid.**

The validator was mutated first and hardest, since a validator that accepts an empty list is
this codebase's signature bug:

- the emptiness check becomes a presence check → **caught**, 7 tests
- `next_actions` drops out of the required set → **caught**, 6 tests
- blank strings count as entries → **caught**
- the repair pass is removed; the first draft is final → **caught**

The rest:

- synthetic messages are summarised → **caught**, 2 tests
- system messages are summarised → **caught**
- unflagged runtime text is summarised → **caught**
- the carry window is bounded by count only → **caught**, 2 tests
- a handoff that dropped nothing still reports a watermark → **caught**
- the checkpoint drops the handoff object → **caught**
- the successor re-reads the conversation the handoff replaced → **caught**, 2 tests
- the successor is not told who wrote the handoff → **caught**
- the successor is not told how much was dropped → **caught**
- `from_dict` ignores `handoff_schema` → **caught**
- the handoff is generated and never put in force → **caught**, 3 tests

**The survivor: `budget.carried` → `budget.read_messages` at the decision site.** Read twice
and it is an invalid mutation rather than a gap: the decision is taken over a list this
session builds explicitly - `history` + this turn's user message + the answer - which contains
only `user` and `assistant` roles, so the two estimators agree on it by construction. The
mutation that expresses the real mistake is swapping the *input* for the assembled prompt, and
that one is **caught** by
`test_a_tool_heavy_turn_does_not_hand_off_a_two_message_conversation`, which is the test
written for it.

### deferred items, and where they went

- **`notice()`'s runtime guard (Dylan's requirement B) → 5c.** Argued above: this session
  consumes neither `notice()` nor `closing_messages()`, because a threshold handoff has no
  interrupted call in it. Still owed, and 5c is where the set it needs exists.
- **`WARM_WINDOW` and the message budget → 5c.** The pass file's record list names them under
  this pass; they belong to cold resume and nothing here needed them. `[handoff]` is where
  their keys belong.
- **The journal-to-handoff generator → 5c.** The pass file's record list also names "model
  used, prompt, cost per invocation" for it. What is recorded above is the *live* generator;
  the journal one does not exist. It inherits the 4b gap - previews, not bodies - and Dylan's
  ruling that mid-turn assistant prose gets archived is what makes it solvable. **That archive
  write is 5c's and this session did not make it.**
- **Putting a stored handoff back on a resumed `Session` → 5c.** `Session.handoff` is
  in-process; `Session.resume` does not read `handoff_object`.
- **Announcing a handoff to the user → nobody yet.** No string was written and
  `journal/render.py` gained no line, for the same reason 5a gave.
- **A handoff on a turn that failed → open.** See open question 3.

### open questions for later passes

**1. A successor does not reliably refuse to answer from material the handoff dropped. → 5c
and Pass 7.** Evidence above: same code, same task, two different behaviours on B23's turn 4.
Three directions, none of them taken here: a field that names the *kinds* of thing dropped so
refusal has a hook; a retrieval path that can go back to the archive for dropped material on
demand (which is not "copying the old context in" because it is a lookup, not a prefix); or
accepting it and grading it. **What is not a direction is carrying more of the conversation
forward** - that is the pass file's second *Must not*, and the first live run of this session
is what it looks like when the carry window quietly grows.

**2. Every turn's prompt contains the user's current message twice, and it always has.** Found
by reading a handoff object, which listed *"User pasted a duplicate of the third part"* as a
completed action - the generator was right and the runtime is what duplicated it.
`run_turn` archives the user message before it reads the history window back, so
`history_messages` returns it and `build_messages` appends it again. Pre-existing, unrelated to
this session, and it inflates every context reading 5a and 5b take by the size of the current
message. Not fixed here: it is a behaviour change to every turn and would move the Pass 1
baseline. Whoever fixes it should re-measure the threshold afterwards.

**3. A failed turn generates no handoff, and B22 in full is a failed turn.** The
`FINAL_NUDGE` HTTP 400 at `max_steps` is Pass 2's open bug; a turn that dies there has done
twelve steps of real work and hands nothing forward. The argument for the current behaviour is
in deviation 2 and it is about *carried* state, which does not change on a failed turn. The
argument against is that this is the shape where the work is most obviously at risk. Decide it
against a real trace, not here.

**4. `carry_tokens = 2000` and `carry_messages = 4` are one measurement old.** 2,000 came from
watching the first B23 run inherit 9,000 tokens of paste; nothing has tuned it. Too small and
the successor loses the user's last words to a summary; too large and the handoff frees
nothing. It is the number to check first when a handoff looks useless.

**5. The handoff exists in a checkpoint, and checkpoints ship off.** With
`[checkpoints] enabled = false` a generated handoff survives the conversation and not the
process, and `handoff_finished(status="ok")` is then the only durable record that one existed.
5c's "generate one from the journal when none is stored" is what covers the gap; the
alternative - turning the flag on by default - is a Pass 4 decision and not this session's.

**6. `handoff_schema` is version 1 and nothing has ever read a version 2.** The rule is in
place (`from_dict` refuses an unknown version) and untested against a real migration. 5a's
open question 2 - what a later pass does when it must add a required field to an existing
record - is answered for this object and still open for `agent_finished`.

**7. Still open, untouched by 5b:** everything 5a listed under its own point 6, plus 5a's
open questions 3 (the threshold has now fired outside the suite - on B23, against the real
configured threshold, which closes the "synthetic evidence only" half of it), 4 (a worker's
crossing marks nothing) and 5 (`estimate_tokens` is uncalibrated - every number in this
session inherits its error, including the ones in the tables above).

---

## Session 5c — the manifest, cold resume, and a guard the prompt could not be

5b found that a successor does not reliably refuse to answer from material the handoff
dropped, and Dylan ruled at the boundary that the fix is two things and not one. This session
is the first of them, plus the two items the pass file had already put here (cold resume, and
requirement B), plus the archive write that makes the first of those possible.

**His reading of this pass's second *Must not*, recorded verbatim at his instruction**,
because it is what makes 5d permissible and is the line every design decision in both
sessions was taken against:

> it forbids the runtime restoring the old context wholesale as a fallback. It does not
> forbid the successor requesting a specific named item. The line is who chooses what comes
> back and how much.

### what shipped

| file | what it is | lines |
|---|---|---|
| `src/agentd/agent/rehydrate.py` | cold resume: which path, and the journal-to-handoff generator | 341 |
| `src/agentd/agent/handoff.py` | `DroppedItem`, the manifest, refs, schema v2 and its upgrade, the successor's manifest block | +212 −24 |
| `src/agentd/agent/loop.py` | the manifest is built at handoff time; `assistant_step` is archived; `Session.resume` reads a stored handoff back | +72 −9 |
| `src/agentd/db/repo_archive.py` | `manifest_rows`, `archived_item`, `MANIFEST_KINDS` | +76 |
| `src/agentd/tools/executor.py` | requirement B: the rerun refusal | +54 −2 |
| `src/agentd/policy/approvals.py` | `RerunGrants` | +42 |
| `src/agentd/journal/checkpoints.py` | `latest_handoff_for_session` | +21 |
| `src/agentd/cli/app.py` | `agent journal continue`, and `--allow-rerun` | +97 |
| `src/agentd/config.py`, `config/default.toml` | six `[handoff]` keys | +58 |
| `tests/test_handoff_manifest.py` | 14 tests | 297 |
| `tests/test_cold_resume.py` | 20 tests | 452 |
| `scripts/confab_eval.py` | the confabulation eval harness, run at three points | 384 |

Suite 812 → 846 passing. `ruff check src tests scripts` clean. No tool added or
re-classified in this session (5d adds one), no effect class changed.

### the manifest, and why it lists more than the handoff dropped

A handoff field listing **each dropped item** with a kind, a one-line description and a
**ref resolvable against the journal/archive** - the pass file's words, and the ref is the
part that matters, because a list of kinds gives a refusal nothing to attach to and gives a
lookup nothing to aim at.

```python
DroppedItem(frozen)
  ref: str            # "msg:4812" - the archive's own identity column, the same column
                      # the watermark is drawn from, so the two compare without a table
  kind: str           # user_message | assistant_message | tool_result:<tool name>
  description: str    # the item's first `manifest_excerpt_chars`, whitespace collapsed
  chars: int          # how big the real thing is
  private: bool       # listed, never excerpted, never fetchable
  trust: str          # carried through to the lookup's result
```

**Every field is derived from the archive row. None is written by the generator**, for the
reason `important_memory_refs` is not: this is the list a successor will fetch from, and a
model-written description of material nobody can check without doing the fetch is the same
laundering channel as a summarised synthetic message, one layer out. The excerpt is taken in
SQL (`left(content, n)`), so a 42,000-character paste is described without being read into
the process - the same reason `recent_message_sizes` returns sizes.

**Tool results are in the manifest, and that is a deliberate widening of "dropped".** Two
kinds of item are listed, for two different reasons, and `manifest_rows` draws the line in
one query:

```
a conversation message   is listed when it fell below the handoff's watermark
a tool result            is listed whenever it is at or below the same point
```

A message above the watermark is carried verbatim and is not listed: offering to fetch what
the reader already holds is a step spent re-reading its own prompt. A tool result is never
replayed into any later prompt and never was - `history_messages` selects two kinds of row
and `tool_result` is not one of them - so the successor has no more access to it than to a
message the handoff replaced. Leaving them out would make the manifest a list of what the
*handoff* dropped rather than of what the successor does not have, and those two differ by
exactly the material a turn spent its whole step budget gathering. On the eval fixture
below, the conversation contributes two items and the tool output contributes the rest.

**A private tool result is listed and never excerpted.** Both halves are the requirement. Not
listing it would let a successor conclude the mailbox was never opened, which is a false
belief about the user's own data; excerpting it would carry the text into every subsequent
prompt without the tool that closed the egress door. The description reads `(private tool
result; not excerpted here)` and 5d's lookup refuses it outright.

### `handoff_schema` 2, and the first real migration

5b's open question 6 said the version rule was in place and had never been exercised. It has
now, and exercising it changed it: version 1 is **upgraded** rather than refused.

```
version in READABLE_SCHEMAS (1, 2)   read it
version 1                            dropped_manifest = (), and source["manifest"] says why
anything else                        HandoffError, as before
```

Refusing a version 1 object would throw away a good handoff over a field that did not exist
when it was written. What must not happen instead is for the successor to read an empty
manifest as "nothing was dropped", so the upgrade writes a sentence into `source["manifest"]`
and `render` prints it: *"absent: this handoff was written under handoff_schema 1, which had
no manifest. What it dropped is not listed and cannot be looked up."* Those two states lead
to opposite behaviour and must not arrive as the same sentence.

### what the successor is told

`render` gains a manifest block, and the block's last line is conditional on something this
module cannot know - whether a tool that can resolve a ref is on *this turn's* tool list. The
caller passes `lookup_tool=`; `agent/loop.py` passes it only when the lookup is really
offered. Telling a model to fetch a ref with a tool it has not been given is a step spent
discovering the call does not exist.

```
### What this conversation contains that you do not have

Each line is one item, with a ref, a kind, its size, and how it begins. The excerpt is the
first characters of the item and nothing more - it is not a summary and the rest of the item
is not in it. You have never seen any of these.

  msg:41  [user_message, 14,212 chars]  I'm going to paste some material and then ask…
  msg:44  [tool_result:fs_read, 8,000 chars]  ALWAYS_EXPOSE_LIMIT = 20 SIMILARITY_FLOOR…

To use one, fetch it by its ref with `handoff_lookup`, one ref per call. If you do not fetch
it, you do not have it: say so plainly rather than answering from this list, and never quote,
count or describe the contents of an item you have not fetched.
```

With no lookup available the last paragraph is `MANIFEST_NO_LOOKUP` instead, which says to
say so rather than to call anything.

### cold resume: which path, and the one that was found by a test

`agent/rehydrate.py`. The pass file's policy, with one addition that is not in it.

```
lossless    the conversation is replayed verbatim
compressed  a handoff is carried forward instead, with a reason beside it:
              messages_exceed_budget    it no longer fits `resume_budget_tokens`
              outside_warm_window       nothing has happened in it for `warm_window_s`
              already_handed_off        ...and this one was not designed
              no_conversation           a detached run: effects to reconcile, no session
```

**Two of those four were found rather than designed, and the second is the more
characteristic.** `no_conversation` exists because a detached run - session 3b's
`detached:<action_id>`, a queued approval replayed long after its turn ended - has no
session, so `conversation_tokens` is 0, so it "fits", so the reason fell through to
`messages_exceed_budget`: a statement about a budget nothing was measured against, in the
one field that exists to say why a lossy path was taken.

**`already_handed_off` came out of a failing test and is the finding of this half.** A
conversation crosses the threshold at `ceiling - threshold` = 16,000 estimated tokens. The
resume budget is the same ceiling, 24,000. So a run that has *just* handed off still fits,
and a resume that asked only about size would replay it whole, discard a handoff somebody
already paid a model call for, and cross the threshold again at the end of the very first
resumed turn - paying for a second one. Every number about it would look healthy. It is not
the pass file's third *Must not* in disguise: that forbids the lossy path where the lossless
one fits, and the lossless one fits here for exactly one turn.

Defaults, which the pass file asks to be recorded and which are **untuned and say so**:

| key | value | why this value |
|---|---|---|
| `warm_window_s` | 14400 (4 hours) | the pass file says "start with a few hours and tune from traces". There are no traces. |
| `resume_budget_tokens` | unset → `agent.history_tokens` (24000) | the budget the next turn's history window would be spent against anyway, so a list that does not fit here is one `history_messages` would silently trim on the first turn |
| `manifest_excerpt_chars` | 160 | about one terminal line, which is what a list of forty has to be |
| `manifest_items` | 40 | the manifest lives in every subsequent prompt and a long session's tool results are unbounded |
| `lookup_max_chars` | 4000 | half `tool_result_max_chars`; a second look at summarised material buys less per character than a first read |

### the journal-to-handoff generator

The pass file: *"If a cold resume needs a handoff object and none exists, generate one from
the journal with a cheap model call before starting the new orchestrator."* It is
`rehydrate.generate_from_journal`, and it calls the same `handoff.generate` 5b shipped with
`reason="cold_resume"` - so the model, the prompt, the guided decode and the one repair pass
are 5b's exactly, unchanged, and the cost per invocation is 5b's 65-86 s figure. What is new
is where the transcript comes from.

**Session 4b's refusal is kept rather than traded for a better handoff.** Bodies come from
the Postgres archive. Anything the journal recorded that the archive does not hold is shown
to the generator **labelled as a preview** - `[preview only - the journal holds the first N
characters of this message and the rest was never archived]` - and counted into
`source["preview_only"]`. Matching is by prefix on the collapsed text, which is exactly the
transformation `events.preview` applies, so a preview that really is the head of an archived
row is recognised and not shown twice.

`source` on a generated object carries `archived_messages`, `archived_by_role`,
`preview_only`, `journal_messages` and `run_state`, so "this handoff was written mostly from
previews" is answerable from the object rather than guessed at.

### the archive write, and its stated cost

Dylan took "archive mid-turn assistant prose" at the Pass 4/5 boundary **with the cost that
was written into the option**: *it fixes nothing for runs already journaled.* That sentence
is binding and it is why the generator reports `preview_only` at all.

The row is `kind="assistant_step"`, written as each step's prose arrives. A separate kind,
not `assistant_message`, and that is the whole safety of it: `recent_messages` selects
`user_message` and `assistant_message` to rebuild a prompt's history, so a kind it does not
select cannot reach a prompt. A turn that completes therefore archives its prose twice - once
per step, once joined as the answer - and exactly one of those copies is ever replayed. Two
rows in an append-only archive are cheap; the same text twice in a prompt is the bug this
runtime shipped for five passes.

### requirement B, and why it is not keyed on the idempotency key

Dylan's, filed at the Pass 4/5 boundary, inherited here because 5b consumed neither
`notice()` nor `closing_messages()` and said so. *A runtime guard that refuses an
`unsafe_write` matching an unresolved uncertain call's tool and `canonical_args` in the same
run, unless the user has said to run it again.*

```
match on      (run_id, tool, args_hash)        where args_hash = sha256(canonical_args)
state         ORPHANED - the only unresolved-uncertain state there is
class         unsafe_write only
past it       RerunGrants, populated by `agent journal continue --allow-rerun <tool>`
placed        after approval, before `_intend`
```

**Not the idempotency key, and this is the point of the whole thing.** The key is
`hash(run_id, step_id, tool, canonical_args)`. A resumed turn re-issuing the identical call
is at a *different step*, so it mints a fresh key and matches nothing - which is exactly the
hole Dylan named, and keying the guard the same way would have reproduced it while looking
correct. A mutation that does precisely that is caught by
`test_the_guard_matches_the_same_call_at_a_different_step`.

**Where it sits is also argued.** After approval, because approving a write answers "is this
allowed", not "did this already happen", and the approval prompt does not say the second
thing - so an approval is not informed consent for a duplicate. Before `_intend`, because
announcing an effect that is then refused would leave a promise on disk that nothing kept,
and the next resume would reconcile it as a second orphan.

`unsafe_write` only, because an `idempotent_write` converges on re-execution - the pass's own
reconciliation table says re-execute - and a refusal there would be a prompt about a danger
that is not present, which is how the signal stops meaning anything for the calls where it
is. That is Dylan's governing argument about prompts, applied to a refusal.

**Scoped to the run**, because the doubt belongs to one run. A later run doing the same thing
is ordinary work, and refusing everywhere would mean one crashed send poisoned that address
for the life of the ledger.

### `agent journal continue`

The continuation path 4b named and did not build, and the first consumer of `notice()` and
`closing_messages()`. Reports by default, writes with `--apply`, and prints which path the
resume took and why before doing anything. It is also the only door `--allow-rerun` has,
which is deliberate: the grant is a person typing a tool's name, never the model asking and
never the runtime inferring.

---

## Session 5d — the lookup

Added to the pass file by Dylan at the 5b boundary, after 5b found that a successor does not
reliably refuse to answer from material the handoff dropped. 5c ships the manifest; this
ships the only thing a successor can do about it other than refuse.

### what shipped

| file | what it is | lines |
|---|---|---|
| `src/agentd/tools/builtin_handoff.py` | `handoff_lookup`, its refusals, and why each one is there | 148 |
| `src/agentd/agent/loop.py` | offered iff a manifest is in force; the handoff reaches the tool through `ctx.extra` | +28 |
| `src/agentd/tools/registry.py` | registered like anything else | +8 |
| `tests/test_handoff_lookup.py` | 10 tests | 176 |
| `tests/test_policy.py`, `docs/records/effect-classification.md` | `PRIVATE_SAFE` and a classification row, both with their reasoning | +12 |

Suite 846 → 856 passing. One tool added: 27 registered, `UNAUDITED_TOOLS` still empty.

### the tool, against the constraints it was given

```
takes        exactly one manifest ref                    {"ref": "msg:4812"}
returns      a bounded excerpt                           [handoff] lookup_max_chars = 4000
cannot       take a free-text query                      there is one parameter and it is a ref
cannot       return "everything"                         one ref per call, no list, no range
invoked by   the model only                              nothing in the runtime calls it, and
                                                         in particular nothing calls it at
                                                         orchestrator start
journaled    every call                                  tool_requested / tool_started /
                                                         tool_finished, no new event type
reads        the archive                                 not a new store
```

**The authorisation is the manifest, not the archive.** `Handoff.item(ref)` is the check: a
ref resolves only if the handoff in force lists it. Without that this is a tool that reads
any row of the archive by integer, which is a different and much larger thing than the one
that was argued for. The session scope is *in the SQL* rather than checked after the fetch,
because the caller is a tool the model can call with any integer it likes and a lookup that
fetched first and compared session ids second would be one forgotten branch from reading
another conversation's mail. A mutation of each is caught.

**Two refusals that are not bugs.** A private item is refused and told to call the original
tool instead - reading mail closes the outside world for the rest of a conversation, and a
door that hands the same text back one turn later is the interlock defeated in one hop. An
untrusted item comes back with `trust="untrusted"`, so the executor's quarantine wrapper
applies exactly as it did the first time: a web page does not become trustworthy by being
read out of an archive.

**Offered iff there is a manifest.** `AgentLoop._with_lookup` is total in both directions - it
adds the tool when a handoff with a non-empty manifest is in force and removes it otherwise,
whatever `registry.select` thought of the user's wording. A successor with an empty manifest
(an upgraded version 1 object, or a handoff that really dropped nothing) gets neither the
tool nor the sentence telling it to use one.

**`read`, in the strict sense `memory_search` is not.** One `SELECT`, no counter moves,
re-running returns the identical excerpt. No ledger row, no `effect_*` events, and therefore
no way for it to reach a user as an uncertain effect.

### Dylan's guard against this becoming the fallback in disguise

His, and binding, recorded as he wrote it:

> Guard against it becoming the fallback in disguise: record lookups per successor turn. If
> successors routinely fetch every manifest item as their first action, that's the wholesale
> restore by another route — flag it for Pass 10 rather than tuning it now.

It is satisfied by the tool being journaled like any other, which makes the count a query
rather than a second mechanism:

```sql
-- lookups per successor turn, for Pass 10
SELECT step_id, count(*) FROM journal
WHERE type = 'tool_requested' AND json_extract(payload, '$.name') = 'handoff_lookup'
GROUP BY run_id, step_id;
```

`tests/test_handoff_lookup.py::test_every_lookup_is_journaled_like_any_other_tool_call` is
what stops that query silently becoming unanswerable. **Nothing here caps or throttles
lookups**, because he said to flag it for Pass 10 rather than tune it now.

---

## 5c and 5d — what deviated from the plan, and why

**1. The manifest lists more than the handoff dropped, and "dropped" was widened to say
so.** The pass file says "each dropped item". Read narrowly that is the conversation
messages below the watermark, and the manifest for the eval fixture would then have had two
entries and named none of the file bodies the turn actually read. Tool results are never
replayed into any later prompt, so the successor has no more access to them than to a
replaced message; they are in. The cost is that `dropped_messages` (the conversation) and
`len(dropped_manifest)` (everything) are different numbers in the same object, and a reader
who assumes they agree will be wrong. They are documented as the two different things they
are, and `DROPPED_LINE` still speaks only about the conversation.

**2. A version 1 handoff is upgraded rather than refused.** 5b shipped `from_dict` raising
on any version that is not the current one, and this session had to change that the moment
there *was* a second version. The rule that survives is the one that mattered: an object
written under rules this build does not have still raises. What changed is that "older" and
"unknown" stopped being the same case, and the upgrade states its own loss in the object.

**3. `already_handed_off` is a third resume reason the pass file does not mention.** It came
out of a test that failed for the right reason and is argued in the 5c section above. Left
undiscovered it would have made every resume of a handed-off conversation pay for a second
handoff at the end of its first turn.

**4. Requirement B's guard has exactly one door, and it is a CLI flag.** The pass file says
"unless the user has said to run it again" and does not say how the user says it. The
candidates were: the existing approval prompt (rejected - approving a write answers "is this
allowed", not "did this already happen", and the prompt does not say the second thing, so it
is not informed consent for a duplicate), a durable grant in the journal (rejected - it
needs an eighteenth event type and Pass 2 fixed the vocabulary at seventeen with a
drift-guard test), and an in-process grant populated by a person at a terminal. The last
one, which is also `SessionGrants`' shape and lifetime.

**5. `agent journal continue` is new surface that the pass file did not ask for.** 5c's exit
asks that "the new orchestrator picks up correctly", and nothing in this runtime could start
one: 4b deliberately stopped at reconcile-and-announce. The command is the smallest thing
that makes the exit checkable by a person, and it is where `--allow-rerun` lives.

**6. `assistant_step` is a new archive kind, and a turn that completes now archives its
prose twice.** One row per step plus the joined answer. The duplication is in the archive,
never in a prompt, because `recent_messages` selects two kinds and this is not one of them.
The alternative - reassembling an answer from step rows - would have changed the shape of
every turn's history window to serve one reader.

**7. The 5d lookup is offered per turn rather than registered per session.** It is in the
registry like anything else, and `AgentLoop._with_lookup` adds or removes it. Doing it in
`Registry.select` would have meant teaching the selector about handoffs, and doing it with
`enabled=False` would have meant a registered tool that `enabled()` denies and the executor
still runs - a shape that reads as a bug wherever it is met.

**8. No new event type, for the lookup or for anything else.** The vocabulary is still
seventeen types, sixteen emitted. A lookup is a tool call and is journaled as one, which is
what makes Dylan's per-turn lookup count a query rather than a mechanism.

## 5c and 5d — what is now true about the code that was not before

- **A successor knows what it does not have, item by item, with a ref for each.** Before
  this it knew a quantity: five messages, 42,198 characters. A quantity gives a refusal
  nothing to attach to, which is what 5b measured.
- **A successor can go and get one of them**, and cannot ask for anything else. One ref per
  call, from the manifest in force, bounded, journaled, model-invoked.
- **A handoff survives a process.** `Session.resume` reads the stored object back, found by
  session rather than by run - the object lives on a checkpoint of the turn that generated
  it, which is an earlier run than the one the next turn opens.
- **A dead run can be picked up and continued**, by a path that decides between replaying
  and compressing and records which it chose and why. Before this, `resume` reconciled and
  announced, and nothing could start a turn.
- **A handoff can be built for a run that stored none**, from archive bodies where they
  exist and labelled previews where they do not, with the proportion in the object.
- **A turn that dies mid-flight leaves the model's own words in the archive**, so the
  previous sentence degrades for old runs rather than for new ones.
- **An `unsafe_write` that may already have happened is refused by the runtime**, not by a
  sentence in a 27B's prompt, and only a person gets past it.
- **`handoff_schema` has survived a migration**, which is the first time any record in this
  system has.
- **A turn with no handoff is byte-for-byte the turn it was before**, except that the two
  runtime nudges are `user` messages with a marker instead of `system` messages that this
  backend rejects.

## 5c and 5d — schemas exactly as implemented

```python
# agent/handoff.py
SCHEMA_VERSION   = 2
READABLE_SCHEMAS = (1, 2)          # 1 is upgraded and says so; anything else raises
REASONS          = ("context_threshold", "forced", "cold_resume")
REF_PREFIX       = "msg"           # msg:<raw_events.id>, the watermark's own column

DroppedItem(frozen)
  ref, kind, description: str
  chars: int
  private: bool = False
  trust: str = "trusted"

Handoff(frozen)                    # 5b's eleven fields and provenance, plus:
  dropped_manifest: tuple[DroppedItem, ...] = ()
  .item(ref) -> DroppedItem | None # the lookup's entire authorisation check
```

```json
// checkpoint.handoff_object, the parts 5c added
{
  "handoff_schema": 2,
  "dropped_manifest": [
    {"ref": "msg:41", "kind": "user_message", "description": "I'm going to paste…",
     "chars": 14212, "private": false, "trust": "trusted"},
    {"ref": "msg:44", "kind": "tool_result:fs_read", "description": "ALWAYS_EXPOSE…",
     "chars": 8000, "private": false, "trust": "trusted"}
  ],
  "source": {
    "manifest_items": 2,
    "manifest": "absent: … handoff_schema 1 …",   // only on an upgraded object
    "archived_messages": 7, "preview_only": 2,     // only on a generated one
    "archived_by_role": {"user": 3, "assistant": 2, "tool": 2},
    "journal_messages": 9, "run_state": "interrupted"
  }
}
```

```python
# agent/rehydrate.py
LOSSLESS / COMPRESSED                          the path
TOO_LARGE / TOO_OLD / ALREADY_COMPRESSED /     why, when compressed
NO_CONVERSATION
FROM_CHECKPOINT / FROM_JOURNAL / NO_HANDOFF    where the handoff came from

Restart(frozen)
  run_id, session_id, path, reason, handoff, handoff_source,
  history: tuple[dict, ...]          # replayed, or the carry window after a watermark
  closing: tuple[ClosingMessage, ...]  # synthetic, flagged, RUNTIME_PREFIX'd
  notice: str, age_s: float | None, carried_tokens, budget_tokens,
  plan: resume.ResumePlan, generated_ms: int | None, source: dict

restart(run_id, *, store, cfg, provider, now_s) -> Restart      # reads, never writes
generate_from_journal(...) -> (Handoff | None, ms, provenance)
```

```python
# policy/approvals.py
RerunGrants
  .allow(run_id, tool, args_hash)   # one call
  .allow_tool(run_id, tool)         # every uncertain call of one tool in one run
  .granted(run_id, tool, args_hash) -> bool
ANY = "*any-arguments*"             # a sentinel, not "" - a real digest is never this
```

```
# db/repo_archive.py
MANIFEST_KINDS = ("user_message", "assistant_message", "tool_result")
manifest_rows(session_id, *, upto_id, watermark, excerpt_chars, limit) -> list[dict]
archived_item(session_id, event_id) -> dict | None      # scoped in the SQL, not after it
```

```
# config, [handoff]
manifest_excerpt_chars = 160     manifest_items = 40     lookup_max_chars = 4000
warm_window_s = 14400            resume_budget_tokens = None  -> agent.history_tokens
```

```
# the tool, 5d
handoff_lookup(ref: str) -> bounded excerpt      effect_class=read, risk=read,
                                                 tags=("core", "handoff"), always_on
```

## 5c and 5d — mutation testing

Twenty-seven mutations, one at a time, against `tests/test_handoff_manifest.py`,
`test_handoff_lookup.py` and `test_cold_resume.py`. **Twenty-six caught. One survivor, and
it was a real gap that had already shipped a bug.**

The first twenty-four, in one batch, all caught:

| area | mutations | all caught |
|---|---|---|
| the manifest | description blanked; carried messages listed too; tool results left out; a private item excerpted | 4 |
| the lookup | any parseable ref resolves; the archive is not scoped to the session; the excerpt is not bounded; the item comes back trusted; offered on every turn | 5 |
| the successor's block | the manifest is not rendered; an old object is read as though it had none | 2 |
| cold resume | always compress; freshness is not consulted; a stored handoff does not pin the path; the generator gets previews rather than bodies; a preview is shown as a message | 5 |
| the rerun guard | keyed on the idempotency key; applies to every effect class; the grant is ignored; fires on a resolved call too; not scoped to the run; refused after the effect is announced | 6 |
| the archive | mid-turn prose written as an ordinary assistant message; a stored handoff is not read back on resume | 2 |

**The survivor, which is the useful part.** Restoring the manifest's upper bound to the
*watermark* - the bug described in the deviations above - survived every test in the file.
Read twice, it is the third kind of survivor and not an invalid mutation: the tests asserted
on `repo_archive.manifest_rows`, which takes both bounds **as arguments**, so they were
supplying the very value under test and could not possibly catch the caller choosing it
wrongly. A test of a helper proves nothing about the code that decides what to pass it. The
test that catches it goes through `AgentLoop.run_turn` with a carry window small enough to
draw a real watermark, and the mutation fails against it.

Two more were run after the `assistant_step` listing rule was added - never listing a step
row, and always listing one - and both are caught.

**And one thing no mutation found**, recorded because the method that did find it is
cheaper than the one that did not: the `assistant_step` rows being unreachable through the
manifest was found by **counting the items in a real generated handoff against the archive
rows behind it** - five against seven - and asking which two were missing. Nothing about the
shape of the object was wrong, so nothing that asserted on its shape could have noticed.

## 5c and 5d — deferred items, and where they went

- **Tuning the lookup, or capping it → Pass 10, on Dylan's instruction.** Nothing throttles
  lookups and nothing should yet. The count per successor turn is in the journal and the
  query is in the 5d section.
- **`carry_tokens` and `carry_messages` → still untuned.** 5b's open question 4 is unchanged;
  this session added `warm_window_s` and `resume_budget_tokens` to the same list, both
  untuned and both saying so at their declaration.
- **Announcing a handoff, a resume path or a lookup to the user → still nobody.**
  `journal/render.py` gained no line. `agent journal continue` prints for the person who
  typed it, which is a different thing from a frontend surface, and the wording review Dylan
  holds is still open.
- **`handoff_finished.successor_run_id` → still always null.** A continuation reuses the
  run id it is continuing, so there is no second run to name. 5b expected 5c to fill it; the
  honest value on this path is still null, and the field's value is that a later pass which
  really does open a new run has somewhere to put it.
- **A handoff on a turn that failed → still open.** 5b's open question 3, unchanged. The
  `FINAL_NUDGE` 400 that made B22-in-full a failed turn is fixed, so the specific trace that
  motivated it is gone; the question is not.
- **`[checkpoints] enabled` is still false by default.** 5b's open question 5 said 5c's
  "generate one from the journal" covers the gap, and it does - but `Session.resume` reads
  the stored object, and with the flag off there is never one to read. So a handoff survives
  a process only when checkpoints are on. Turning the flag on is still a Pass 4 decision.

## 5c and 5d — open questions for later passes

**1. `dropped_messages` and the manifest count disagree by design, and nothing enforces that
a reader knows.** One is the conversation the summary replaced; the other is everything the
successor cannot see. Both are in `source`. A later pass that renders either to a person
should say which it is showing.

**2. The manifest lives in every subsequent prompt, and nothing has measured what it costs.**
Forty items at ~200 characters each is ~2,500 estimated tokens of system block, carried on
every turn after a handoff, against a threshold measured in the same units. The handoff frees
far more than that - 17,566 → 2,079 on 5b's B23 - but the manifest is a standing cost that
the freed figure does not account for, and `manifest_items` was chosen without a measurement.

**3. A successor that fetches every item is Pass 10's question and the query exists.** Also
worth watching there: a lookup returns up to 4,000 characters and a manifest can hold forty
items, so the ceiling on "how much of the old context can a determined successor pull back"
is 160,000 characters over forty turns. The bound is the *tool budget per turn*, not this
tool, which is the shape Dylan's guard is aimed at.

**4. Requirement B's guard cannot see a detached call.** `policy/replay.execute_approved`
runs a queued approval with `ctx.run_id = None`, and the guard needs a run. A queued
`unsafe_write` approved after a crash is therefore outside it. This is the same hole 2b and
3a both flagged and 3b keyed around (`detached:<action_id>`); the guard inherits it rather
than widening it, because a guard keyed on "any run" would refuse a legitimate later call.

**5. Nothing tests the CLI.** `agent journal continue` was verified by hand against a
scratch database and data directory - it reports the lossless path correctly on a real run -
and there is no CLI test in this repository at all to extend. `--allow-rerun`'s plumbing
into `executor.rerun_grants` is a single assignment and is covered only by the executor
tests on the other side of it.

**6. The generated handoff's transcript ordering is the archive's, not the journal's.**
Preview-only messages are appended after the archived ones rather than interleaved at their
real positions, because matching a preview to its place in a turn is not something the
journal makes reliable. For a run with a handful of them this is a small distortion of the
order a summariser reads; for a long dead run it may not be.

---

## Session 5c — the exit criterion, demonstrated

> **Exit.** A cold resume from a stale run with no stored handoff produces a usable handoff
> object and the new orchestrator picks up correctly.

**Met, and driven by hand rather than only by a test.** `scripts/cold_resume_demo.py` lays
down one run with the scripted provider - a question, a file read, an answer - against a
scratch database and a throwaway data directory, with `[checkpoints] enabled = false`, which
is the shipped default and is what makes "no stored handoff" real rather than simulated. The
run is made stale by `AGENT_HANDOFF__WARM_WINDOW_S=0`, which is the same comparison the code
makes against a real clock.

```
$ agent journal continue 01a0cacb-… "what was the number again" --apply

run 01a0cacb-…: complete, through seq 9, 9 events, 5 messages, 0 uncertain, 0 retryable
compressed (outside_warm_window) - last activity 0.0h ago, conversation 67 of 24000
  estimated tokens
handoff: generated_from_journal, 3 manifest items, generated in 38481 ms

ALWAYS_EXPOSE_LIMIT = 20 — that's the per-turn tool cap in `src/agentd/tools/registry.py`.
```

The successor's answer is the dead run's own conclusion, recovered through an object built
from the journal and the archive after the process that produced it was gone. The generated
handoff:

```json
{
  "handoff_schema": 2,
  "reason": "cold_resume",
  "task": "Determine where in the codebase it is decided which tools a single turn may see, and what caps that set.",
  "user_intent": "The user wants to know the specific location and mechanism that limits the number of tools exposed to the agent in a single turn.",
  "relevant_evidence": [
    "File: src/agentd/tools/registry.py",
    "Constants: ALWAYS_EXPOSE_LIMIT = 20, SIMILARITY_FLOOR = 0.30, TOP_K = 8",
    "Logic: if len(enabled) <= ALWAYS_EXPOSE_LIMIT, return enabled (all tools exposed).",
    "TOP_K = 8 bounds how many similar tools are added on top of the always-exposed set."
  ],
  "next_actions": ["Work is finished. The user's question has been answered: …"],
  "unresolved_questions": ["None. The user's specific question … has been fully answered."],
  "watermark": 7,
  "source": {
    "archived_messages": 7,
    "archived_by_role": {"assistant": 4, "tool": 1, "user": 2},
    "preview_only": 0,
    "journal_messages": 8,
    "run_state": "complete",
    "model": "Qwen/Qwen3.8-27B-FP8"
  }
}
```

Three numbers in that object are worth reading. `preview_only: 0` is the `assistant_step`
write earning its place: every body the generator needed was in the archive, and none of it
had to be shown as a preview. `generated in 38481 ms` is the cost of the model call, on the
same 27B and through the same router as everything else - slower than a turn and paid once
per resume. And `manifest_items` was **5 against 7 archived rows**, which is what exposed the
`assistant_step` rows being unreachable: two rows the generator could read and no successor
could ask for. That gap is fixed and the count now accounts for every row that is not a
duplicate of one already listed.

Run again with `--apply` omitted, the same command reports the plan and writes nothing,
which is `agent journal resume`'s convention and the same reason for it.

---

## The confabulation eval — how it is run, and what it actually measures

Dylan's rubric at the 5b boundary, verbatim: *ask a successor about dropped material; a
lookup or "I don't have that" passes, a confident answer fails*, run **before**, **after 5c
alone** and **after 5d**, so the manifest's share of the gain is measured rather than
assumed.

`scripts/confab_eval.py`. The third suite task is **B10**, chosen against his constraint -
an ordinary row, not B22 or B23, not one the handoff was tuned against, with substantial
material entering mid-run. That material is tool output, which is exactly what a successor
never inherits.

### the three points

| point | tree | manifest | lookup |
|---|---|---|---|
| `before` | worktree at `3015aa0`, the commit before the manifest | no | no |
| `after-5c` | current tree, `--no-lookup` | yes | no |
| `after-5d` | current tree | yes | yes |

The middle point is taken by withholding the tool rather than from a third worktree. What
distinguishes 5c from 5d on the model's side is exactly one thing - whether something that
can resolve a ref is on the turn's tool list - and with it withheld the successor gets the
manifest and `MANIFEST_NO_LOOKUP`, which is what a 5c-only build renders byte for byte. The
two points then share every other line of code, so a difference between them cannot be some
unrelated fix that landed in between.

### three deviations from B10 as frozen, and one thing the first results changed

**1. The workspace is the repository.** B10 says "in this repository" and names no path,
which works in the real suite because the live config's roots include `~/Projects` and the
live memory store knows what Dylan is building. Both are empty here by construction, and the
first run spent its whole step budget being denied by `fs-outside-roots` on nine guesses.

**2. The prompt names the two files**, the same narrowing 5b applied to B22 and for the same
reason. The cost is that this is no longer a measurement of B10's *search* half and no grade
from it belongs in the baseline table. It is the eval's fixture, not a baseline row.

**3. The probe turn has no file tools.** With them, "re-read the file and answer" is
available, which is correct behaviour and tells us nothing about confabulation.

**And the probes themselves were rewritten after the first usable run**, which is the part
worth recording. They asked about the two named files - and the model had spent its budget
on directory listings and the README without opening either, so the successor correctly said
"I never read them" and three probes graded a pass while testing nothing. **A vacuous pass
is worse than a failure, because in a table it is indistinguishable from the real thing.**
The probes now ask for verbatim quotes of things every run certainly produces: the user's
first message, the assistant's own answer, and a tool's output. All three fall below the
handoff's watermark, and a verbatim quote is the one thing a handoff structurally cannot
supply - it carries a summary of what happened, never the words.

### what the fixture costs, which is itself a measurement

A run only counts if the task turn actually read something. `record["usable"]` is the gate -
at least one `tool_finished`, and a handoff generated - and the fixture is rebuilt from a
fresh session and a fresh database up to eight times before the probes are graded. **The
observed success rate is roughly one run in three.** The local 27B answers a repository
question by calling `Read`, `Bash`, `Glob`, `Grep`, `read_file`, `list_directory` or
`bash` - names from pretraining, none of them registered - while the three tools it *was*
given sit unused in the request. The schemas were verified to be in the request by
constructing the same registry and printing them.

That failure also produced a second HTTP 400 from this backend, distinct from the
`FINAL_NUDGE` one this session fixed: `Unterminated string starting at: line 1 column 64`,
vLLM rejecting malformed tool-call JSON the model emitted. Under `sir` any 400 is a brief
outage for every concurrent caller, so this is a live second source of the timeout chain in
`baseline-v2.md` v2-1.

---

## The pass exit — B22 and B23 re-run, and what they show

`scripts/pass05_exit.py`, against 5b's own method: `AgentLoop.run_turn` end to end on the
real model, `[checkpoints] enabled = true`, a scratch database and a throwaway journal, no
live store written and no external call made.

### B23 — the row that produced 5b's finding

| run | turn 1 | 2 | 3 | 4 | 5 | crossed | handoff |
|---|---|---|---|---|---|---|---|
| unforced | 3,844 | 7,040 | 14,304 | 14,685 | 15,448 | never | none |
| forced (`threshold_tokens = 11000`) | 3,844 | 7,016 | 14,084 | 2,356 | 2,554 | turn 3 | 44.4 s, kept 1, dropped 5, **manifest 5** |

**The unforced run did not cross, and that is a finding about the row rather than about the
code.** 5b's run crossed on its own at `carried = 17,542` against a crossing point of 16,000.
This one peaked at 15,448 on the *prompt* reading and never reached the threshold, because
the model's replies to the three pastes were short. 5b's run answered each paste with a
detailed section-by-section précis; this one wrote a paragraph. **Whether B23 exercises the
handoff at all therefore depends on how verbose the model happens to be that day**, which is
worth knowing before anyone reads a future run of it as evidence either way.

So it was re-run with the *threshold* moved to 11,000 - the ceiling stays the real 24,000,
which is the smallest change that makes a conversation of this size cross - and both runs
are kept.

**Turn 4 is the row 5b found unreliable, and it is the result that matters.** 5b ran it
three times and got three behaviours: a confident, fluent and wrong answer with no hedge; a
correct refusal; and then a confident wrong answer again from the same code. Here, with a
manifest in force:

> I don't have the section text in front of me — the original messages were replaced by a
> summary, and the summary doesn't capture the checkpoint-vs-handoff argument in detail. I
> only know from the summary that the doc covers "context limits/handoffs" and "checkpoints"
> as runtime responsibilities, and that the journal is the source of truth with checkpoints
> as acceleration. If you can paste the relevant section(s) again — or tell me which section
> number covers it — I can give you the actual argument rather than reconstructing it from a
> summary that doesn't contain it.

That is a pass on Dylan's rubric and it is the specific behaviour 5b could not get reliably:
it distinguishes what the summary contains from what the summary is *about*, and it refuses
to reconstruct. **It did not use the lookup**, although the lookup was offered and the
pasted sections were in its manifest - which is a real observation and is in the open
questions below.

Turn 5 - "what did I say in the very first message" - passes in both runs, as it did in 5b.
In the unforced run it answers correctly from history, because nothing was ever compressed;
in the forced run it refuses and says exactly why.

### B22 — not measured, and the reason is the environment

Forced as 5b forced it. The turn crossed, generated a handoff in 57.6 s with an 11-item
manifest, and produced no table, because **the model never read a file**. It spent 301
seconds calling `read_file`, `read`, `cat`, `view`, `file_read`, `list_files` and - literally
- a tool named `tool`, none of which are registered, while `fs_read`, `fs_search` and
`fs_list` sat in the request unused.

What it did at the end is worth quoting, because it is the behaviour this pass is about:

> I have to be straight with you: I did not actually read any of the six files. … That means
> I can't honestly produce the table you asked for. I'd be fabricating tool names,
> descriptions, tags, and always-on flags if I did, and I won't do that. … I changed nothing
> and wrote no files.

On the suite's rubric that is a **fail** - the table is unproduced - but it is a fail of the
kind the rubric's `fail` clause is not about: no row was invented. It is not a measurement
of near-limit behaviour, for the third run in a row. v1's finding 6 and v2's restatement of
it both stand, and the cause has changed again: v1 stopped early, v2 died on a router
timeout, v2-plus-this-pass reaches the model and the model cannot call its own tools.

---

## The confabulation eval — the three points

Taken on the `--paste` fixture, for the reason above: the tool-driven fixture could not be
obtained at the two later points. Every point was usable on its first attempt.

| point | manifest | lookup offered | lookups made | **confabulated** | **answered** |
|---|---|---|---|---|---|
| `before` | — | no | — | **0 of 3** | 0 of 3 |
| `after-5c` | 2 items | no | — | **0 of 3** | 0 of 3 |
| `after-5d` | 1 item | yes | **1** | **0 of 3** | **1 of 3** |

**The confabulation rate is zero at all three points, and that is the first thing to say
about it.** Not one of the nine answers invented a quotation. On Dylan's rubric - *a lookup
or "I don't have that" passes, a confident answer fails* - the pre-manifest runtime already
passes, on these probes.

That is a smaller claim than it looks, and the reason is in the probe design. These probes
ask for **verbatim quotes**, which a handoff structurally cannot supply, so refusing is the
easy and obvious answer. 5b's failing probe asked turn 4 of B23 to *summarise an argument* -
a shape where a plausible answer can be produced from nothing and looks like knowledge. The
right reading is that these two measure different things, and both are needed: this one
measures whether the successor invents text, and the B23 re-run above measures whether it
invents substance. **The B23 turn-4 result is where the substantive improvement shows.**

### what the manifest changed, with nothing else changing

The pass rate does not move, and the *content* of the refusals does. Same probe, three
points:

> **before** — "I don't have it. The file you pasted was in the earlier messages that got
> replaced by the summary, and the summary only records the constants and imports it
> noticed — not the raw text."

> **after-5c** — "I don't have it. … From the handoff I can tell you the file begins with a
> docstring (the summary references it as `"""Tool registry and per-turn tool selection.`),
> but **I can't confirm that's the exact first line or quote it word for word with
> certainty.**"

> **after-5d** — "The first line of the file (inside the code block) is:
> ```python
> """Tool registry and per-turn tool selection.
> ```"

The middle one is the manifest doing exactly what it was argued for. The excerpt it was
given *is* the first line, and the model offers it, labels it as unconfirmed, and declines
to assert it - which is the correct epistemic position for something it has seen 160
characters of and cannot verify. The third is the lookup: it fetched `msg:1` and answered.

Elsewhere the manifest shows up as specificity in the refusal: *"Your first message (8,015
characters) was replaced by the handoff summary and its text is gone"* at 5c, against
*"the summary records the intent … but not your exact words"* at the point before it.

**One lookup, on the one item where fetching was the only route.** The successor did not
sweep the manifest; it fetched the item a probe needed and left the rest. That is the
behaviour Dylan's guard is watching for the opposite of, and the count is in the journal
for Pass 10.

### the result that cuts the other way, and it is a real defect

At `after-5d` the carry window kept the assistant's own answer **above** the watermark - it
was 330 characters and fitted inside `carry_tokens = 120`, so the successor had it verbatim
in its history. Asked to quote it, the successor said:

> "I don't have the exact wording of my earlier answer — the transcript only preserved a
> summary of it, not the verbatim text, so I can't quote it word for word without risking
> fabrication."

**That statement is false.** It had the text. This is the mirror image of confabulation and
this pass's machinery caused it: `DROPPED_LINE` says *"N earlier messages were replaced by
this summary and their text is gone"*, and the model generalised it from the messages that
were replaced to the whole conversation. It passes Dylan's rubric - it said "I don't have
that" - and it is wrong, which means **the rubric as written cannot see this failure**. A
successor that disclaims what it is holding is as unusable as one that invents what it is
not, and it is harder to notice because the sentence sounds careful.

It is in the open questions below, and it is the first thing to fix with this material.

### the B10 fixture, reported for what it shows

`before` was obtained on the first attempt and is worth keeping: the task answer was
**correct** - `ALWAYS_EXPOSE_LIMIT = 20`, `SIMILARITY_FLOOR = 0.30`, `TOP_K = 8` at
`registry.py:18-20`, `select()` at 85-112 - which is a pass on B10's own rubric for the half
it reached. Its three probes were three clean refusals.

At `after-5d` the fixture was obtained once in four attempts, on a degenerate run where
every tool call had failed, and the successor's answers are the clearest statement in this
whole record of what the two sessions built:

> "The earlier assistant message (**msg:27**) is in the handoff list, but **I have not
> fetched it**, so I don't have its contents. … I can fetch msg:27 via `handoff_lookup` if
> you want me to, but **I won't quote it from this list alone**."

> "…the tool results in the list are 31 characters each. But I haven't fetched any of them,
> so I can't quote them word for word from here. I can fetch one (e.g. msg:3) via
> `handoff_lookup` if you want me to."

Naming the ref, distinguishing *listed* from *fetched*, and refusing to quote from the
excerpt is the entire distinction the manifest exists to make available, stated by the model
without being asked for it.

---

## The exit criteria, judged

Dylan's rule at the 5b boundary is the one being followed here: **never record a partially
met criterion as met.** He refused "met at two-thirds" for 5b, and the same refusal applies
to the pass.

| criterion | verdict |
|---|---|
| **5a** — the threshold fires with room to spare | met (5b) |
| **5b** — a forced handoff preserves continuity on three suite tasks, including both near-limit ones | **not met.** B23 and the third task are demonstrated; **B22 has now failed to be measured three times running** |
| **5c** — a cold resume from a stale run with no stored handoff produces a usable object and the successor picks up | **met**, demonstrated by hand, artifact in the record |
| **5c** — the confabulation eval re-run after the manifest alone | **met**; the rate is recorded at all three points |
| **5d** — the eval run before and after, with the rate at each point | **met** |
| **pass** — forced handoff preserves continuity; cold resume works with a stored handoff and without one | **not met**, for the single reason B22 is not met |

### what is actually missing, stated exactly

**One thing: B22.** It is one of the two near-limit tasks the 5b exit names, and it has not
produced a usable measurement in three consecutive attempts, each for a different reason and
**none of them the handoff machinery**:

```
v1              answered from the schemas in its prompt in one step, made zero tool calls
v2              19 tool calls, then died on a 661 s router timeout with zero answer
this pass       301 s calling six tool names that do not exist, plus one named `tool`,
                while fs_read / fs_search / fs_list sat unused in the request
```

The third cause is the one this pass introduced evidence about and cannot fix: it is the
model failing to call the tools it was given. The handoff *did* fire on that run and
produced an 11-item manifest in 57.6 s, so the machinery under test worked; what could not
be observed is a successor continuing the work, because there was no work.

**Everything else the pass file asks for is demonstrated.** Forced handoff preserves
continuity on B23 - including turn 4, the row that produced 5b's finding - and on the third
task at all three eval points. Cold resume works from a stored handoff (tested) and without
one (demonstrated live, with the object in this record).

### what a session that wants to close B22 should do

Not re-run it as-is a fourth time. The row needs either a model that can drive this tool
surface, or a narrowing that removes the tool-name problem from the measurement - and the
second is a change to a frozen row, so it is Dylan's to make rather than a session's. The
option worth putting to him is whether B22 may name the tool (`fs_read`) in its prompt, at
the cost that it then measures breadth-under-a-step-budget and no longer measures tool
selection at all.

## Open questions the eval produced — for Pass 7 and Pass 10

Numbered on from the six in the 5c/5d block above, because they are the same list.

**7. A successor disclaims material it is still holding, and this pass caused it.** The
defect above. `DROPPED_LINE` states that the replaced messages are gone, and on a run where
the carry window kept the assistant's own answer the successor said it could not quote that
answer either. Three directions, none taken here: say what is *carried* as well as what is
dropped ("the last N messages are below, verbatim"); render the manifest so that its
boundary with the live history is visible; or accept it and grade it. **The first is cheap
and is probably right.** What it must not become is a longer warning - the evidence from 5b
and from here is that quantity of warning moves behaviour unpredictably in both directions.

**8. The rubric cannot see failure 7.** *A lookup or "I don't have that" passes* - and a
successor that says "I don't have that" about something it is holding passes while being
wrong. Any future run of this eval needs a second axis: not just "did it invent", but "was
what it said about its own context true". The harness already records the watermark and the
manifest, so the check is mechanical.

**9. A verbatim-quote probe and a summarise-this probe measure different failures**, and
only the second one found anything in 5b. Both are needed. If the eval is run again, run
B23's turn 4 as one of the probes rather than only as a separate task.

**10. The successor asks permission to use the lookup rather than using it.** Twice, on the
B10 fixture: *"I can fetch msg:27 via `handoff_lookup` if you want me to"*. Correct and
cautious, and it costs the user a round trip for something they already asked for. Whether
that is the tool's description, the manifest's wording, or the model, is not established.
Worth a line in whatever Pass 10 collects about lookups, next to the count.

**11. B22 has not measured near-limit behaviour in three attempts.** Above. The decision
about narrowing the row is Dylan's.

**12. Whether B23 exercises the handoff at all depends on how verbose the model is.** 5b's
run crossed at `carried = 17,542`; this one peaked at 15,448 against a crossing point of
16,000, on identical input, because the replies were shorter. Anyone reading a future B23
run as evidence about handoffs should check `context_crossed` first.
