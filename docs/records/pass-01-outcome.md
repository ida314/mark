# Pass 1 — Baseline & Instrumentation — outcome

Sessions completed: **1a**. Sessions 1b (task suite) and 1c (measurement run and failure
survey) are not started; append to this record when they are.

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
