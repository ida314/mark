# Pass 5 — Context Handoff — outcome

Sessions completed: **5a**. 5b (handoff generation) and 5c (cold resume) are not started, so
the pass's exit criteria are not yet met. 5a's own exit — "threshold crossing fires reliably
in a forced-long run, with enough room left to produce a handoff without operating at the
edge of the window" — is met, against a ceiling that is **not** the one the pass file's `~8k
remaining` implies at first reading. That choice is the substance of this session and is
argued in full below.

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
