# Pass 1 — Baseline & Instrumentation — outcome

> **Numbering note, added 2026-09-25.** This record is left exactly as it was written. It was
> written before result verification was filed as the new Pass 9, so where it says "Pass 9" it
> means what is now **Pass 10 — Tool Discovery & Router**, and where it says "Pass 10" (or
> 10a/10b/10c) it means what is now **Pass 11 — Evaluate & Tune**. The mapping is in
> `docs/records/session-ledger.md`.

Sessions completed: **1a**, **1b**, **1c**. The pass's exit criterion — a baseline table
for the frozen task suite plus a written failure survey — is met by
`docs/records/baseline.md`, **with one gap that is recorded rather than closed**: the five
rows needing a human at the terminal (B11, B12, B13, B21 under `chat`, and B23) were not
run. Twenty-two `ask` rows were graded, including one for each of those four coding tasks,
but those four measure the approval wall rather than the capability — see "the approval
wall is a prerequisite question for Pass 8a" below, and §5 of `baseline.md`.

**Read `docs/records/baseline.md` for the numbers and the findings.** This record covers
where things live, what deviated from the plan, and what the measurement changed about the
priority of later passes.

---

## Session 1a — Telemetry emission

### what shipped

`src/agentd/obs/telemetry.py` (new, 353 lines) plus nine call sites in
`src/agentd/agent/loop.py`, a `[telemetry]` config section, and `tests/test_telemetry.py`
(19 tests).

**Emitted from** `AgentLoop.run_turn`, which is the single funnel every channel already goes
through — `agent ask`, `agent chat`, one-shot, daemon turns, Telegram, and sub-agents. One
record per `run_turn` call. Nothing was added to any other entry point, because there is no
other entry point.

**Format:** one JSON object per line, appended to `<paths.data_dir>/logs/telemetry.jsonl`
(override with `[telemetry] path`, disable with `[telemetry] enabled = false`). Written with
a single `os.write` to a fd opened `O_APPEND`, so the CLI and the daemon cannot interleave
halves of each other's records. This is explicitly throwaway storage; Pass 2 replaces it
with the journal.

Every field session 1a asked for is present. The mapping, where it was not literal:

| Asked for | Field | Note |
|---|---|---|
| `request_id` | `request_id` | the turn's `uuid7`, same id as the `actions` row |
| tool calls made | `tools.calls[]`, `tools.called`, `tools.call_count` | per-call and deduped |
| tools visible but unused | `tools.visible_unused` | named, not just counted |
| tool-selection failures | `tool_selection_failures` | four kinds, kept apart — see below |
| context tokens start/peak/end | `context_tokens` | one sample per step, before the call |
| wall-clock latency | `latency_ms`, plus `llm_ms` / `tool_ms` | where the clock went |
| main-agent token usage | `usage.input_tokens` / `output_tokens` / `reported` | see finding 1 |
| final status | `status` | `completed` \| `failed` \| `abandoned` |

### what deviated from the plan, and why

**Four kinds of selection failure, not one number.** The pass says "wrong tool chosen, or
retry with a different tool". Summing those into one integer would make the baseline
unreadable, because the fixes differ and Passes 8 and 9 each only move some of them:

- `unknown_tool` — a name in no registry, invented outright.
- `not_visible` — a real tool the model reached for without having been shown it. This is
  the number Pass 9 is trying to move.
- `invalid_args` — rejected before the handler ran, so the step bought nothing.
- `switched_after` — a call failed and a later step tried a *different* tool. This is the
  pass's "retry with a different tool", and it is **a proxy**: a tool can fail for reasons
  that have nothing to do with which tool was chosen. Kept separate so 1c can report it
  separately rather than let it inflate the others.

A denied call is deliberately **not** a selection failure. Policy refusing a write says
nothing about whether the right tool was picked, and counting it would make the baseline
move whenever the policy does. It is recorded as `calls[].denied`.

**`usage.reported` was added.** Not in the spec; see finding 1 — without it the baseline
cannot tell a free turn from an uncounted one.

**Sub-agent turns are recorded too**, because they share this loop. They carry
`role: "subagent"` and `actor: "subagent:<name>"`. The baseline table must filter to
`role == "main"`, or a delegating turn's worker tokens get counted twice.

**"abandoned" means the step budget ran out**, i.e. `steps >= max_steps`. The last step is
forced tool-free and carries `FINAL_NUDGE`, so what comes back is a summary of unfinished
work rather than an answer. This is the same reading `subagents.py` already gives
`budget_exhausted`, so the two agree.

**"context tokens at end" is the last prompt sent**, not anything assembled after the loop.
A post-loop sample was written first and then removed: it was provably identical to the last
step's sample (the loop only exits when the model stops calling tools, and nothing is
appended after that), so it was code no test could distinguish from its absence. The final
answer is already counted in `usage.output_tokens`; counting it here too would make the two
numbers disagree.

### what is now true about the code that was not before

- Every turn, on every channel, leaves a machine-readable record of what it cost and what it
  reached for. Before this, the `actions` table recorded that a tool ran but not what else
  was on offer when it did — and the "visible but unused" number is the whole point of
  Passes 8 and 9.
- The turn loop is unchanged behaviorally. Every value is read off state the loop already
  kept; nothing feeds back into tool selection, the prompt, or the step budget. The full
  suite went 492 → 511 passing with no existing test touched.
- A telemetry failure cannot take a turn down. `finish()` swallows, counts into
  `telemetry.write_failures`, and says so on stderr once per process. This is tested by
  pointing the path at a non-directory.

The suite was mutation-checked rather than trusted for being green: seven deliberate breaks
were introduced (drop a sample, fake `visible`, never report `abandoned`, let a write
failure escape, ignore tool-call arguments in the estimate, read `visible` after the re-add,
sample context once). Two survived the first suite and are the reason
`test_reaching_for_a_withdrawn_tool_is_recorded_as_not_visible` exists and the post-loop
sample was removed. All seven are caught now.

### schemas as actually implemented

`schema: "agentd.telemetry.turn/1"`. Verbatim from a live run:

```json
{
  "schema": "agentd.telemetry.turn/1",
  "request_id": "01a0c01a-eced-72d0-9dce-1621e3915c9d",
  "session_id": "01a0c01a-eccc-79d4-900a-faa08dd77f01",
  "started_at": "2026-09-20T18:36:25.197282+00:00",
  "finished_at": "2026-09-20T18:36:27.751151+00:00",
  "role": "main", "actor": "main", "origin": "interactive", "channel": "cli",
  "autonomy": "assist", "model": "Qwen/Qwen3.8-27B-FP8",
  "status": "completed", "steps": 3, "max_steps": 12,
  "latency_ms": 7510, "llm_ms": 7228, "tool_ms": 15,
  "usage": {"input_tokens": 0, "output_tokens": 0, "reported": false},
  "context_tokens": {"start": 1537, "peak": 1772, "end": 1772},
  "tools": {
    "registry_size": 26,
    "offered": ["...19 names..."],
    "visible": ["...19 names, plus anything tool_search revealed..."],
    "called": ["tool_search", "time_now"],
    "visible_unused": ["...17 names..."],
    "call_count": 2,
    "calls": [
      {"step": 1, "name": "tool_search", "ok": true, "denied": false,
       "invalid_args": false, "known": true, "visible": true, "duration_ms": 15}
    ]
  },
  "tool_selection_failures": {"count": 0, "by_kind": {}, "events": []},
  "finish_reasons": ["tool_calls", "tool_calls", "stop"],
  "answer_chars": 45,
  "error": null, "trace_id": null, "span_id": null
}
```

`telemetry.read_records(path)` is the reader 1c should use. It skips a truncated final line
rather than dying on it, because a process killed mid-write is one of the things this pass
exists to count.

### deferred items, and where they went

- **Task suite** — session 1b. Nothing written yet; `evals/` does not exist.
- **Baseline table and failure survey** — session 1c. `docs/records/baseline.md` does not
  exist yet.
- **No CLI command** to read the file. 1c can use `telemetry.read_records`. Not built,
  because a reporting command shaped around a storage format that Pass 2 deletes is work
  done twice.
- **`~/.local/share/agent/logs/telemetry.jsonl` already holds two smoke-test records**
  from verifying this session end to end (2026-09-20 18:36 UTC, `origin: "interactive"`).
  1c should ignore or delete them before the real run.

### open questions for later passes

**1. Token usage is not measured at all, and never has been.** The provider already asks for
`stream_options: {"include_usage": true}` and parses the chunk correctly
(`llm/openai_compat.py:116,131`), but the SIR router at `:8000` drops the usage chunk on
streamed calls. Confirmed directly: an identical non-streaming request returns
`{"prompt_tokens": 14, "completion_tokens": 8}`, the streaming one returns no usage chunk at
all. Not fixed here — the pass says to record what the measurement reveals and move on.

Consequences, in order of how much they matter:

- `usage.reported` is `false` on every real turn. The `input_tokens`/`output_tokens` zeros
  are **not measured values**, and 1c must not put them in a table as though they were.
- This is not new and not confined to telemetry: `actions.tokens_in` and `actions.tokens_out`
  have been zero for every streamed turn since they were added, and
  `subagents.run_subagent` computes its `tokens` budget from the same empty dict — so a
  sub-agent's token budget has never been enforced either.
- `context_tokens` is the only real size signal available today, and it is an estimate
  (`ids.estimate_tokens`, len/3.2), not a count.
- **This should be resolved before Pass 8**, which is justified by a token-cost comparison
  that cannot currently be made. The fix is upstream (the router) or a non-streaming
  accounting call, and is not this codebase's to make unilaterally.

**2. `tool_search` is being called when it has nothing to add.** In the live tool-using run
above, the model spent its first step and 15ms on `tool_search` even though `time_now` was
already in the 19 tools it had been shown, and the search revealed nothing new
(`visible == offered`). One observation is not a rate, but this is precisely the waste Pass 9
exists to remove, and the record already carries what is needed to count it: a `tool_search`
call where `tools.visible` gained nothing. 1c should report that rate. No new failure kind
was invented for it, because it is derivable.

**3. Trace correlation is dead on the CLI path.** `trace_id` and `span_id` came back `null`
from a real `agent ask`, meaning `otel.setup` is not being reached or no collector is up.
Pre-existing and faithfully recorded rather than papered over; it only matters if a later
pass wants to join telemetry to Jaeger.

**4. A turn whose event stream is abandoned mid-iteration writes no record.** If a consumer
stops iterating `run_turn` early, neither `tele.finish` nor the existing `_record_turn` runs.
The telemetry gap and the audit gap are identical in shape, so nothing was done about it
here — but Pass 4 (checkpoints) will have to care, since that is exactly the process-failure
shape it is built for.

---

*The four above are from session 1a. The three below are from the 1c measurement run, and
each is tagged with the pass that has to answer it. Full write-ups are in
`docs/records/baseline.md` §4 under the finding numbers given.*

**5. Delegation does not execute at all. → Pass 6.** (baseline finding 4.) Both sub-agent
turns in B16 failed in ~130ms, **before any LLM call** (`llm_ms: 0`), on
`HTTP 400 {"message":"System message must be at the beginning."}`. The `delegate` tool is
registered, is chosen when a task warrants it, and cannot run. **Pass 6 cannot demonstrate
anything until this is fixed**, so it is the first thing that session should check — not
something to discover after the role definitions are written. Same root cause as open
question 7 below: this backend rejects a `system` message anywhere but first, and something
in the sub-agent's message assembly puts one later.

**6. The agent has no concept of which project it is working on. → Pass 6.** (baseline
finding 5.) `tools/builtin_fs.py:18` resolves relative paths under
`~/.local/share/agent/workspace`, which is empty; absolute paths outside
`paths.allowed_roots` are denied with `rule=fs-outside-roots`. So a prompt saying "in this
repository", or naming `src/agentd/...`, resolves to nothing, and the observed behaviour is
eight to twelve steps groping at `/`, `~` and `/tmp` until the step budget is gone.
`~/Projects` *is* in `allowed_roots` — the model simply never guesses it. **Every
repository task in the baseline suite failed on this before reaching the capability it was
written to measure.** It belongs to Pass 6 because a delegated coder role inherits the
problem unchanged and cannot be evaluated without an answer; Pass 8a then depends on that
answer. Related: those path denials are also what inflates the `switched_after` selection
failure count, which otherwise reads as tool-choice error.

**7. `abandoned` is unreachable: a turn that exhausts its step budget crashes. → the
durability spine, Passes 2–4.** (baseline finding 1.) `agent/loop.py:213` appends
`{"role": "system", "content": FINAL_NUDGE}` to the *end* of the message list on the last
step, and this backend rejects it. The turn raises, is recorded `status: "failed"` with
`answer_chars: 0`, and the user gets nothing. Three of the twenty-two graded rows ended
this way, and both sub-agent turns died on the same rejection. **Pass 4 checkpoints and Pass 5's handoff trigger both treat budget exhaustion as a
summarizable state that can be resumed from; it is currently a crash**, so the state those
passes are built to capture does not exist at the moment they would capture it. Pass 2's
journal is the place this becomes visible — a turn that dies here writes a telemetry record
but produces no answer and no journal entry describing what was in flight.

*One more, from the same run, that has no pass yet:* a single request produced **two** open
loops, one of them written by the daemon's review path with no `actions` row
(`memory/review.py:201` dedupes candidate loops by exact statement match). An effect with
no journal entry cannot be folded by `state = fold(reduce, journal, initial)`, and an
intent that travels two paths needs an `idempotency_key` rather than string equality. That
is baseline finding 3, and it is the strongest single argument in the record for Passes 2
and 3.

---

## Session 1b — the task suite

### what shipped

`evals/baseline-tasks.md`, 23 tasks, and `evals/fixtures/b12-mutation.patch`. Frozen by
commit, tagged `eval-baseline`.

**The count is 23, at the top of the 15–25 band, on purpose.** Passes 8a–8c each claim
"completion rate not regressed" for one family in isolation, and a family of two cannot
tell a regression from a coin flip. The smallest family is four.

**Tasks carry capability labels, not tool names.** Pass 8 moves tools between the
orchestrator and durable roles, so a label frozen as a list of tool names would be stale by
8d. 9b maps capabilities to whatever the registry holds at that time.

**Grading is by hand, into pass / partial / fail, and `status` is not completion.**
`status: "completed"` only means the loop stopped calling tools; a fluent wrong answer is
`completed`. A fabricated answer grades `fail`, never `partial`, because this codebase's
two characteristic bugs — "degrades to a plausible NULL" and "degrades to a plausible
retry" — would otherwise score partial credit and make the system look like it was
improving while it got quieter. Session 1c added a fourth value, **inconclusive**, for
tasks whose own premise did not hold at run time; see below.

### the tag moved once, on day one

| sha | what it froze |
|---|---|
| `6bb466d` | the first freeze: 23 tasks, one method for all of them |
| **`c97d3ab`** | **current `eval-baseline`**: method is per task |

`6bb466d` said every task is one `agent ask --autonomy assist`, *and* that "the approval
prompt is part of what is being measured". The first measurement run showed those cannot
both be true — `agent ask` cannot prompt at all. Rather than vary the method silently at
run time, the suite was amended, re-frozen, and the tag moved. Both shas are recorded in
`baseline.md`.

Three other corrections went in before the first freeze: the reset section said
`agent open-loops list` (the command is `agent loops list`); B21 and B22 said 25 registered
tools, and the registry holds 26, which B22's rubric grades against; and B09's rubric was
corrected at re-freeze, because it graded the system against an approval prompt the policy
never emits.

---

## Session 1c — the measurement run

### what shipped

`docs/records/baseline.md`: the environment that was pinned, a 23-task results table with
per-task method and attended/unattended marking, aggregates, nine findings, and the failure
survey. Run 2026-09-20 19:11Z–19:40Z against `Qwen/Qwen3.8-27B-FP8` on the SIR router,
`agentd 0.1.0`, daemon pid 4814 running throughout.

**The model and its quantization are recorded in the table**, because a SIR swap to the
other resident model (`nvidia/Qwen3.6-27B-NVFP4`) would move every number in the file and
nothing in the telemetry record names it.

**B06 was run live, not simulated.** `gcal-nyu` was disabled by hand at 19:15:13Z and the
graded runs took place 3m21s and 3m43s into the stall — about 20s short of the
`2 × poll_interval_s` the run condition asks for, recorded as a deviation rather than
rounded up. It did not affect the signal: a hand-disabled connector surfaces `disabled`
directly rather than being inferred from age.

### what the measurement revealed, in priority order for later passes

The full write-ups are in `baseline.md` §4. What matters for sequencing:

**1. `abandoned` is unreachable, and that is a Pass 4 and Pass 5 problem.**
`agent/loop.py:213` appends a `system` message at the end of the message list on the last
step; this backend rejects any system message that is not first. Every turn that exhausts
its step budget therefore raises, is recorded `failed`, and returns nothing — three of the
twenty-two graded rows did. Pass 5's handoff trigger and Pass 4's checkpointing both treat budget
exhaustion as a summarizable state. It is currently a crash.

**2. Delegation cannot execute at all.** Both sub-agent turns in B16 died in ~130ms on the
same rejection, before any LLM call. Pass 6's baseline is: `delegate` is registered, is
chosen when appropriate, and fails immediately.

**3. A duplicate side effect happened during the run.** One request produced two open
loops — one from the tool call, one written by the daemon's review path five minutes later
with no due date and no `actions` row. `memory/review.py:201` dedupes candidate loops by
exact statement match, so any rewording duplicates. This is the clearest possible argument
for the Pass 2/3 spine: an effect with no journal entry cannot be folded, and an intent
that travels two paths needs an `idempotency_key`, not string equality.

**4. The tool-selection instrument is blind to the worst failure it saw.** In B04 the model
denied having a calendar tool that was in that turn's `visible` list, and
`tool_selection_failures` recorded `0` — correctly, since no existing kind fits. All 23
recorded failures were `switched_after`, the kind 1a flagged as a proxy, and they were
mostly path denials rather than tool-choice errors. **Pass 9 needs a fifth kind** —
"denied a capability that was on the turn's `visible` list" — or its exit criterion will be
measured against a number that stayed 0 while the behaviour it names occurred.

**5. Pass 8 is still blocked on token accounting.** `usage.reported` was `false` on all 26
records, as 1a predicted. The comparison 8 is justified by cannot be made yet.

### the approval wall is a prerequisite question for Pass 8a, not just a suite defect

`agent ask` builds its loop with `QueueApprover` (`cli/app.py:619`), which never prompts:
on a `require_approval` verdict it queues the call, hands the model a denial, and lets the
turn continue. Only `agent chat` wires `CliApprover` (`cli/chat.py:120`). At `assist`
autonomy, `fs_write` and `shell_exec` are the two `require_approval` tools the suite
touches, so **the one-shot path cannot write a file or run a test at all.**

This was found as a suite defect and fixed there — the four affected tasks are now `chat`
tasks. **It is also a live question about Pass 8a, and it should be answered before 8a
starts rather than in the middle of it:**

> Pass 8a gives a durable coder role `fs_write` and `shell_exec`. Which approver is in
> force inside a delegated role, and can it prompt? If a sub-agent inherits the one-shot
> path's approver, the coder role is blocked before it is written: every write and every
> test run returns a queued denial, and 8a's exit criteria are unreachable for a reason
> that has nothing to do with the tool surface.

Pass 6 (delegation) lands before Pass 8, so the answer belongs in Pass 6's design: a
delegated role needs either an approver that can reach the user, an autonomy contract that
makes its writes `allow`, or an explicit "queue and resume" story — which is Pass 3 and
Pass 4 machinery, and another reason those passes come first.

**Related, and cheaper to answer:** the agent has no concept of which project it is working
on. `builtin_fs.py:18` resolves relative paths under `~/.local/share/agent/workspace`,
which is empty, and absolute paths outside `paths.allowed_roots` are denied — so "in this
repository" resolves to nothing. Every repo task in the suite failed on this before it
reached the capability it was written to measure. A coder role inherits the problem
unchanged.

### what 1c could not measure

- **Token cost** — the router drops the usage chunk (1a, open question 1). Still open.
- **The private-data interlock** (B18) — the most recent Brightspace mail contained no
  link, so there was nothing to fetch and the interlock was never exercised. Graded
  inconclusive; re-run against a mail that carries a link before treating it as covered.
- **Memory recall with provenance** (B19) — the dodds.org hosting belief the task assumes
  is not in memory. "I have no memory of that" was the correct answer. Graded inconclusive.
  Re-check the premise before 8c re-runs it.
- **Near-limit behaviour** — B22 answered in one step without calling a tool, so the step
  budget was never approached; and any turn that does reach the budget crashes (finding 1).
- **Trace correlation** — `trace_id` and `span_id` were `null` on all 26 records, as in 1a.
