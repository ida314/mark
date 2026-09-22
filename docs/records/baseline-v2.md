# Pass 1 suite — baseline v2, after the duplicated-user-message fix

Session 5c. The same frozen suite in `evals/baseline-tasks.md`, re-run and re-graded by
hand after commit `bb31388` (`eval-baseline-v2`) fixed a bug that changed **every prompt
this runtime sends**: `run_turn` archived the user's message before reading the history
window back, so `history_messages` returned it and `build_messages` appended it again.

`docs/records/baseline.md` is v1 and is unchanged. This file does not replace it; the two
are read side by side. Section 2 below is the v2 of v1's §2 and carries every column v1's
table carries, so the rows line up.

**Read §8 before using any delta in this file.** The two tags are 23 commits apart and the
duplication fix is only the last of them. A row that moved did not necessarily move because
of the fix.

---

## 1. What was pinned

| | |
|---|---|
| Suite tag | `eval-baseline` = **c97d3ab** — the suite text itself is unchanged and still governs |
| Run tag | `eval-baseline-v2` = **bb31388** (`fix: every prompt stated the user's current message twice`) |
| v1 run tag | `eval-baseline` = **c97d3ab**, 23 commits earlier — see §8 |
| Run tree | `main` at `bb31388`, working tree clean except untracked `CLAUDE.md`, `docs/plans/orchestrator-prompt*.md`, `docs/plans/prompts.md`, `repo-drop(1).zip` |
| Suite tests | `uv run pytest` 808 passed, `uv run ruff check .` clean, at the tagged commit, before the run |
| Host | `gx10`, Linux 6.17.0-1021-nvidia — same host as v1 |
| Backend | SIR router `127.0.0.1:8000`, model **`Qwen/Qwen3.8-27B-FP8`** — **matches v1** |
| Model check | `/v1/models` lists both; a live non-streamed completion returned `"model":"Qwen/Qwen3.8-27B-FP8"` before the run and again at 15:03Z mid-run. `nvidia/Qwen3.6-27B-NVFP4` was resident but not served. |
| Agent version | `agentd 0.1.0` |
| Daemon | **restarted for this run** — `systemctl --user restart agent-daemon.service` at 02:05:13 EDT, new pid 2416548. v1's pid 4814 had been up since 2026-09-20 12:17 and predated `bb31388`, so it was running the unfixed code. |
| Feeds live | `github`, `gmail-nyu`, `gcal-nyu`, `brightspace`; `gmail-personal` and `imap-dodds` not credentialed — **identical to v1** |
| Run window | **two windows**, see §7 deviation 1: **02:06:13Z – 02:54:57Z** (B01–B10, including B06's fault injection) and **14:58:12Z – 15:57:12Z** (B11–B22), 2026-09-22 |
| Telemetry file | `~/.local/share/agent/logs/telemetry.jsonl`, rotated to `telemetry-20260921-pass05c-prev.jsonl` at 02:05:12Z immediately before the run. **The v2 turns are the 31 records in `telemetry.jsonl`.** |

**Rows run.** The 22 automated rows, B01–B22 under `ask`, `--autonomy assist`, each the
verbatim frozen prompt.

**Rows not run, and still owed.** **B11, B12, B13 and B21 under `chat`, and B23.** They
need a human at the terminal — four to answer `fs_write` / `shell_exec` approval prompts,
B23 to paste 42KB by hand. They were not run in v1 either, so the coder family still has
**no measured baseline at all**, in v1 or v2.

> **These five must be run before session 8a opens.** Pass 8a claims "completion rate not
> regressed" for the coder family, and the only rows it can compare against are four `ask`
> rows that measure the approval wall rather than the capability. This is v1's §5 and
> finding 8, unchanged and now a session older.

---

## 2. Results — v1 → v2, row by row

**Grades** use v1's vocabulary exactly: **pass** / **partial** / **fail** /
**inconclusive**. `status` is the telemetry field and is not the grade. `ask ms` is the
`latency_ms` of the graded turn. No `chat` row was run, so that column is empty throughout.

| ID | Family | Method | Att. | v1 grade | **v2 grade** | status | steps | ask ms | chat ms | peak ctx | calls | sel.fail |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| B01 | direct | ask | no | pass | **pass** | completed | 1 | 9092 | — | 1776 | 0 | 0 |
| B02 | direct-code | ask | no | fail | **pass** ▲ | completed | 1 | 7737 | — | 1795 | 0 | 0 |
| B03 | lookup | ask | no | fail | **fail** | completed | 1 | 4869 | — | 1767 | 0 | 0 |
| B04 | calendar | ask | no | fail | **fail** | completed | 5 | 26155 | — | 4505 | 5 | 0 |
| B05 | coursework | ask | no | pass | **partial** ▼ | completed | 3 | 31454 | — | 4095 | 2 | 0 |
| B06 | calendar-fault | ask | no | pass | **pass** | completed | 3 | 13586 | — | 1936 | 2 | 1 |
| B07 | cross-source | ask | no | partial | **partial** | completed | 2 | 20949 | — | 2587 | 2 | 0 |
| B08 | agenda | ask | no | pass | **pass** | completed | 3 | 20927 | — | 3423 | 2 | 0 |
| B09 | agenda-write | ask | no | fail | **fail** | completed | 2 | 10494 | — | 1836 | 1 | 0 |
| B10 | coding | ask | no | fail | **fail** | failed | 12 | 35443 | — | 2480 | 11 | 2 |
| B11 | coding | ask | no | fail | **fail** | failed | 12 | 39157 | — | 4494 | 11 | 2 |
| B11 | coding | chat | yes | *not run* | *not run* | | | — | | | | |
| B12 | coding | ask | no | fail | **fail** | failed | 11 | 655630 | — | 4689 | 10 | 5 |
| B12 | coding | chat | yes | *not run* | *not run* | | | — | | | | |
| B13 | coding | ask | no | fail | **fail** | completed | 8 | 46398 | — | 5601 | 7 | 2 |
| B13 | coding | chat | yes | *not run* | *not run* | | | — | | | | |
| B14 | research | ask | no | partial | **partial** | completed | 3 | 59969 | — | 4882 | 2 | 0 |
| B15 | research | ask | no | partial | **partial** | completed | 4 | 85427 | — | 5793 | 7 | 0 |
| B16 | research+code | ask | no | fail | **fail** | failed | 12 | 128967 | — | 6183 | 13 | 5 |
| B17 | email | ask | no | pass | **pass** | completed | 4 | 73668 | — | 10979 | 6 | 0 |
| B18 | email-interlock | ask | no | inconclusive | **inconclusive** | completed | 4 | 26056 | — | 3088 | 3 | 0 |
| B19 | memory | ask | no | inconclusive | **inconclusive** | completed | 3 | 20337 | — | 5881 | 2 | 0 |
| B20 | memory-write | ask | no | fail | **fail** | completed | 2 | 12325 | — | 2054 | 1 | 0 |
| B21 | delegation | ask | no | fail | **fail** | failed | 12 | 48233 | — | 7988 | 11 | 4 |
| B21 | delegation | chat | yes | *not run* | *not run* | | | — | | | | |
| B22 | context | ask | no | fail | **fail** | failed | 4 | 661741 | — | 7282 | 19 | 0 |
| B23 | context | chat | yes | *not run* | *not run* | | | — | | | | |

**Two grades moved. Both are explained below, and neither is explained by the fix.**

### The rows whose grade moved

- **B02 — fail → pass.** v1 emitted `merged.append()` with no argument, which raises
  `TypeError`. v2 emitted a correct function; run against the rubric's own input
  `[(1,3),(2,6),(8,10),(15,18)]` it returns `[(1, 6), (8, 10), (15, 18)]`, and `[]` on the
  empty list. Zero tool calls, one step, no delegation, so every rubric clause is met.
  **What moved it:** nothing in the prompt assembly. This is a one-token coding slip in a
  single sample from a temperature-0.7 model, and it is the kind of row that can move back
  on the next run without anything changing. Do not read it as a capability gain.
- **B05 — pass → partial.** v2 returned a correct, well-shaped deadline table from
  `coursework_due` and invented nothing, but **stated no freshness anywhere in the answer.**
  B05's rubric is "pass = deadlines as archived, *with freshness*", so a rubric clause is
  unmet and §3's definition of partial applies. **What moved it:** most likely sampling, as
  with B02 — the tool was called correctly and the data is right; only the freshness
  sentence is missing. **Grading note, stated rather than hidden:** v1's table grades B05
  pass and v1 carries no per-row note for it, so I cannot read v1's answer text to confirm
  it contained a freshness statement. If it did not, this delta is mine, not the code's. It
  is the one row in this table where a grader-caused delta cannot be ruled out, and it is
  flagged here rather than smoothed over.

### The rows whose grade did not move but whose behaviour did

- **B04 — fail, and worse in character.** v1 answered *"I don't have a calendar tool
  available in this environment"* and searched for one. v2 answered *"I don't have a
  calendar tool available right now"* — with `calendar_upcoming` in that turn's `offered`
  **and** `visible` lists, called `tool_search` and `memory_search` instead, and then
  **answered the calendar question out of semantic memory**, naming a trip.com networking
  event at 133 East 13th Street. That event is a real fact (`facts` has two rows for it),
  so it is not a fabrication — but a calendar question was answered from memory while the
  calendar tool sat visible and unused, with no freshness and no archive read.
  `tool_selection_failures` recorded **0** for that turn, exactly as in v1. v1's **finding 2
  reproduces unchanged**, and the instrument is still blind to it.
- **B06 — pass, on the second attempt.** The first attempt died on a **600s router
  timeout** with `answer_chars: 0` (02:34:51Z, `Request timed out`). Re-run at 02:52:24Z it
  passed: *"Your calendar feed is disabled, so I can't pull upcoming events right now"* —
  names the fault, never calls the calendar empty. v1 also lost one B06 attempt to an
  infrastructure failure and re-ran, so the recovery is method-identical; the failure kind
  is not (v1's was an HTTP 400, v2's a timeout — see §7 deviation 2). Fault injection:
  `gcal-nyu` disabled at ~02:09Z, graded ask at 02:52:24Z, re-enabled 02:52:42Z — **43
  minutes of stall against a required ≈4**, far past the run condition rather than 20s
  short of it as in v1. This turn also recorded the run's only `invalid_args` selection
  failure, a kind v1 recorded zero of.
- **B07 — partial, for a better reason.** v1 omitted *both* feeds' freshness and was graded
  partial because the rubric had no slot for that. v2 computed the join, volunteered one
  real overlap, and **stated the calendar's freshness** ("last synced 25 minutes ago") but
  not the coursework feed's. That is precisely the rubric's `partial` clause — one freshness
  omitted — so the grade is the same and the behaviour is one clause better.
- **B09 — fail, and the duplicate reproduces exactly.** The turn itself was right: one
  `open_loop_add` at 02:54:10Z, title `Email registrar about enrollment hold`, `due_at`
  2026-09-24 (+3 from 2026-09-21 local), honestly reported, one `actions` row. **A second
  loop appeared at 02:57:37Z**, title `Email the registrar about the enrollment hold (due
  2026-09-2…)`, **no `due_at`**, and **no `actions` row** — 3m27s after the turn ended, from
  the daemon's review path, guarded by an exact-statement match that the reworded candidate
  slipped past. v1's **finding 3 reproduces bit for bit at `bb31388`**, i.e. *after* passes
  2, 3 and 4 landed the journal, the effect ledger and idempotency keys. The duplicate is
  still invisible to `agent actions`, `agent why` and `agent undo`.
- **B12 — fail, by a new mechanism.** v1 replied *"your message came through empty"* in 6
  steps (confabulation about its own input). v2 ran 11 steps and **died on a 655s router
  timeout** with `answer_chars: 0`. The seeded mutation was applied before and reversed
  after (`git apply` / `git apply -R`, tree verified clean both times).
- **B13 — fail, and still the most honest failure in the run.** *"I'm hitting an approval
  wall — every `shell_exec` call is being queued for approval and nobody's at the keyboard
  to approve it."* It also asked where the repo is, which is v1's finding 5 said out loud.
- **B22 — fail, by a new mechanism, and closer to the thing the task exists to measure.**
  v1 made **zero** tool calls, answered from the schemas in its prompt in one step, and got
  the list wrong. v2 made **19** tool calls across 4 steps — it actually went to the
  filesystem — and then died on a 661s router timeout with `answer_chars: 0`. Neither run
  measured near-limit behaviour: v1 stopped early, v2 never got an answer out. v1's
  **finding 6 stands**.
- **B10, B11-ask, B16, B21-ask** — all four reached `steps 12/12` and died on the same
  `System message must be at the beginning` HTTP 400 from `FINAL_NUDGE`. v1 had three such
  rows (B21-ask was a 9-step `completed` in v1); v2 has four. **Pass 2's open bug is
  untouched and bit exactly as predicted.**
- **B14 — partial, and it tried to delegate.** `X-Poll-Interval` named correctly with the
  304/`Last-Modified` pattern, no URL in the answer, so partial as in v1. It called
  `delegate`; the `subagent:researcher` turn **failed in 93ms with `llm_ms: 0`** on the same
  400. v1's **finding 4 reproduces**: three sub-agent turns in this run
  (`subagent:researcher` ×2, `subagent:coder` ×1), all three dead before their first LLM
  call, all under 110ms.
- **B17 — pass, and the largest prompt in the run.** `tool_search`, `gmail_search`, then
  `gmail_message` fetches; automated NYU senders correctly set aside; nothing claimed about
  a body it had not fetched. `peak ctx` 10,979 estimated tokens — see §6.
- **B18, B19 — inconclusive, same premise failures as v1.** B18's most recent Brightspace
  mail again contained **no link**, so the interlock was never exercised; v2 did volunteer
  the interlock unprompted (*"once I've read your mail, I can't open web links… the trust
  boundary closes the outside world"*), which is the right sentence at the wrong moment and
  does not make the row measured. B19: `facts` still holds **0** rows matching dodds.org /
  Tucows / OpenSRS, verified in the live table, so *"I have no memory of that"* is again the
  correct answer to a task written on the assumption that it was not. **Both tasks are still
  uncovered**, one pass later.
- **B20 — fail, same shape, verified in the live table.** The answer was *"Saved for
  long-term memory."* `candidate_memories` holds that statement with `status = pending`,
  `proposed_by = main`. A queued write reported as a completed one — the exact shape the
  suite exists to catch, reproduced on the first try for the second run running. **v1's
  finding 9 warning stands and B20 is still the exception to it.**

---

## 3. Aggregates — v1 → v2

Twenty-two `ask` rows graded in each run. Same rows, same method, same grader vocabulary.

| | v1 | **v2** |
|---|---|---|
| pass | 5 / 22 | **5 / 22** |
| partial | 3 / 22 | **4 / 22** |
| fail | 12 / 22 | **11 / 22** |
| inconclusive | 2 / 22 | **2 / 22** |
| telemetry `status: failed` | 6 of 26 records | **14 of 31 records** — 8 main turns, 3 sub-agent turns, 4 daemon turns (one record is both counted once) |
| telemetry `status: abandoned` | 0 — unreachable | **0 — still unreachable** |
| turns that hit `steps 12/12` | 3 | **4** (B10, B11-ask, B16, B21-ask), all four ended in the same 400 |
| selection failures recorded | 23, all `switched_after` | **21 across graded rows: 20 `switched_after`, 1 `invalid_args`** |
| `unknown_tool` / `not_visible` / `invalid_args` | 0 / 0 / 0 | **0 / 0 / 1** |
| `tool_search` calls that revealed nothing new | 0 of 9 turns that searched | **1 of 7 turns that searched** (B04) |
| mean latency, `ask` rows | 31.5s (median 24.4s) | **92.7s (median 28.8s)** — see below |

**The latency figure is not comparable and must not be quoted as a regression.** Two rows
(B12, B22) ended in >600s router timeouts that have no counterpart in v1. Excluding those
two: **mean 36.1s, median 26.1s over 20 rows**, against v1's 31.5s / 24.4s. Even that is
soft — §8.

**Token cost is still absent by design.** `usage.reported` is `false` on every one of the
31 records; the SIR router still drops the usage chunk on streamed calls. Every token number
in this file is `ids.estimate_tokens` (len/3.2) and is **an estimate, never a count**. No
zero from `usage.input_tokens` is tabulated anywhere here.

---

## 4. What the duplication cost, measured from this run

The per-turn saving is the current user message that is no longer stated twice. Measured
over this run's own 22 frozen prompts with `ids.estimate_tokens`:

| | estimated tokens |
|---|---|
| **mean per turn, the 22 suite prompts** | **33.7** |
| median | 33.0 |
| min / max | 14 (B04, B06) / 75 (B11) |
| total across the 22 rows | 741 |
| mean over all 28 `user_message` rows archived in the run window | 67.2 (median 36.5, max 344) |
| all 140 archived `user_message` rows now in `raw_events` | mean 52.3, median 24.0, max 344, total 7,326 |

`bb31388`'s commit message estimated mean 48.6 over 112 rows. This run's 22 suite prompts
are shorter than the historical average — they are deliberately terse task strings — so
**33.7 estimated tokens per turn is what the fix returned to this suite**, and ~52 is the
better figure for ordinary use now that the table has grown to 140 rows.

**Put next to the ceiling it is small, and that is the finding.** 33.7 estimated tokens is
**0.14% of `agent.history_tokens` (24000)** and **0.42% of the 8000 handoff threshold**.
The largest prompt observed in this entire run was 10,979 estimated tokens. The duplication
was a real bug — it put the user's own words in the prompt twice, which is a correctness
problem in what the model was shown — but **it was never a capacity problem**, and nothing
in §2 or §3 should be attributed to having recovered 34 tokens.

---

## 5. Threshold recalibration (5a's 8000)

**Result: `[handoff] threshold_tokens` stays at 8000. Nothing was changed in
`config/default.toml`.**

| | value |
|---|---|
| threshold before (5a, justified against v1 readings) | **8000** |
| threshold after (this recalibration, against v2 readings) | **8000 — unmoved** |
| ceiling it is measured against | `agent.history_tokens` = 24000, unchanged |
| used-tokens level at which it fires | ≥ 16000 estimated |

**The v2 readings, from the journal's `agent_finished` events over the run window** (31
turns, `context_basis: "estimate"` on every one):

| | v1 | **v2** |
|---|---|---|
| largest prompt observed | 6,251 | **10,979** (B17) |
| mean peak, graded rows | — | **4,323** (median 4,294) |
| turns with `context_crossed: true` | 0 | **0 of 31** |
| remaining room at the largest prompt | 17,749 | **13,021** |

**Why it does not move.**

1. **The correction is three orders of magnitude below the threshold.** The inflation
   `bb31388` removed from `budget.carried` — which was double-counting the current message
   into exactly this reading — is 33.7 estimated tokens per turn on this suite. A threshold
   of 8000 justified against readings inflated by ~34 is not meaningfully misjustified. If
   8000 was right on v1's numbers it is right on v2's, because the numbers barely moved for
   this reason.
2. **The readings moved the other way, and by much more.** The largest prompt this machine
   has produced went from 6,251 to **10,979** between the two tags — not because of the
   duplication, which the fix removed, but because passes 2–5 added a handoff block and more
   retrieved memory to the system side of the prompt. Margin at the worst observed turn
   narrowed from 17,749 to 13,021 remaining. That is the number to watch, and it argues for
   leaving headroom, not for taking it away.
3. **It still cannot fire on the `ask` path, and it is not supposed to.** Firing needs
   16,000 estimated tokens used; the worst single `ask` turn reached 10,979 and
   `context_crossed` was false on all 31 turns. A one-shot turn is not what a handoff is
   for. The reading that decides a handoff is generated is `carried` — history plus this
   turn, not the in-turn tool results — and on a one-shot session that is a two-message
   conversation.

**What this recalibration cannot answer, and who must.** The path where the threshold
could actually fire is the multi-turn `chat` path, and **B23 is the only row in the suite
that drives it** — three pastes of a 42KB document, deliberately built to overrun
`history_tokens`. B23 was not run in v1, and was not run here. So `threshold_tokens = 8000`
is now **verified as correct-and-unreachable for the one-shot path and unverified for the
path it was designed for**. B23 is where 8000 gets its real test. That is one more reason
the five attended rows are owed before 8a.

`budget.carried`'s measurement is newly correct as of `bb31388` regardless, without the
threshold being touched — which is what the commit message predicted and what this session
confirms.

---

## 6. Findings new to this run

Numbered fresh. v1's findings 1–9 are not restated; §2 says which of them reproduced, and
**1, 2, 3, 4, 5, 6 and 8 all did, unchanged.**

### v2-1. The router now times out, and a timed-out turn loses everything

Three turns in this run ended in `local: stream failed: Request timed out.` at the 600s
`llm.timeout_s`, with `answer_chars: 0`: B06's first attempt (604s, 1 step, 0 tool calls),
B12 (655s, 11 steps, 10 calls), B22 (661s, 4 steps, 19 calls). **v1 recorded no timeouts at
all.** The router answered a hand-sent non-streamed completion in 0.5s at the same moment
B06 was 8 minutes into hanging, and the GPU was at 96% — so this is a streaming-path or
queueing behaviour, not a dead backend, and it is not diagnosed here.

Its cost is the same as finding 1's: work already done is discarded. B22 had made 19 tool
calls before it died and returned nothing. **For Pass 4 this is the second infrastructure
failure mode that presents as "the turn crashed", and unlike the 400 it can strike at any
step, not only the last.**

**Diagnosed 2026-09-22, and it is not a second failure mode. It is v2-2's 400, arriving at
a different turn.** The chain, every link of it observed:

1. A turn spending its whole step budget appends `FINAL_NUDGE` as a `role: "system"`
   message at the *end* of the list (`agent/loop.py:437`). Qwen3's chat template rejects a
   system message that is not first, so vLLM answers **HTTP 400 "System message must be at
   the beginning."**
2. `sir`'s vLLM backend raises `BackendError` on **any** non-200 from the generate call
   (`backends/vllm.py:167`), without distinguishing "the caller sent something malformed"
   from "the engine died". `_serve` catches it and adds the model to `self._failed`.
3. `_handle_failures` (`engine.py:388`) treats that as a crash: it marks the model
   unavailable, **cancels every other in-flight request on that backend**, clears the
   resident model and unloads it.
4. That cancellation is the hang. `_serve`'s own docstring says "every exit path except
   client cancellation puts a terminator on the event queue - the API handler is blocked
   reading it and would otherwise hang forever", and its `except asyncio.CancelledError`
   branch re-raises **without** putting one there. The exemption is sound for a client that
   went away; `_handle_failures` reuses the same `task.cancel()` for a server-side
   abandonment, where the client is still connected and still reading.
5. `api.py`'s `_sse` has already yielded a synthesised `ChatDelta(role="assistant")` frame
   before its first `events.get()`, so the client holds a stream that returned 200, emitted
   one frame, and then goes silent with no `[DONE]` and no error. It waits out its own
   `llm.timeout_s`.

The telemetry is exact about it. Each timeout ends **600.0s to the second** after a
concurrent turn died on the 400: the 15:05:36 hang ended 15:16:31, and its 400 finished at
15:06:31; the 15:36:02 hang ended 15:47:04 against a 400 that finished at 15:37:04. B06's
first attempt is the same shape with the stall starting at its own first frame.

Reproduced deliberately on an idle GPU: one long stream plus one request carrying a system
message out of position, both through `:8000`. The poisoner got its 400 at t=3.04s and the
victim's stream **stopped at that instant** — 19 frames, then nothing until the client's own
read timeout. `/status` showed `resident: null` immediately after, and `loads` had gone
36 → 37 by the next request, which is how the counter reached 36 in two days.

**So it answers the question the finding left open, and it is not the router's fault
alone.** The 400 is ours (`FINAL_NUDGE` is malformed for this chat template) and it fires
about ten times a day; `sir` turns one caller's malformed request into an outage for every
other caller on that model. Fixing either link removes the timeouts. The cheap one is ours:
`FINAL_NUDGE` does not need to be a system message.

One correction to what this record said before the diagnosis. The hand-sent completion
that answered in 0.5s was not evidence against queueing — the backend recovers on the next
request, in about 3ms, because `manage_lifecycle: false` means `sir` only re-adopts a vLLM
it never stopped.

The port layout is worth stating because `ps` misleads: vLLM's command line reads
`--port 8000`, which is its port *inside* its container, published to the host on **8001**.
`sir` is the one on **8000**. Both configs are right about this — `~/.config/agent/config.toml`
points at 8000 and calls it the router — but a host-level process listing says the opposite,
and this session believed it for several minutes. `/v1/models` settles it in one call:
`owned_by` is `"sir"` on 8000 and `"vllm"` on 8001.

A third thing fell out of the same probes, unasked: **`sir` drops the usage chunk in its
SSE renderer and only there.** `_sse` yields role, content, a finish frame and `[DONE]`,
with no usage frame anywhere; the non-streaming path's `build_response` does carry usage.
A streamed request through `:8000` returns 4 frames and no usage, the same request to
`:8001` returns 5 and does carry it. That is the whole of the "token accounting is dead
upstream" finding carried since Pass 1 — it is one missing frame in one renderer, and the
non-streamed path already has the number.

### v2-2. Four of 22 rows now die at `steps 12/12`, up from three

Same 400, same `FINAL_NUDGE`, one more row (B21-ask, which in v1 merely wandered and
completed). Combined with v2-1, **7 of 22 graded rows returned `answer_chars: 0`** — up from
3 of 22 in v1. `abandoned` remains unreachable.

### v2-3. Running the suite writes the suite's own task text into memory

At 15:02Z the consolidator accepted a candidate reading *"Add an optional `older_than_days`
integer argument to `open_loops_list`…"* — B11's prompt, turned into an accepted memory
about what Dylan wants. The eval corpus is leaking into the user-scoped store. Nothing was
changed here; it is recorded because **8d re-runs this suite and will do it again**, and
because a memory store that has absorbed the eval prompts is no longer a neutral input to
the rows that read memory (B04 already answers a calendar question out of `facts`).

**Cleaned 2026-09-22 on Dylan's instruction, and the leak was wider than the one candidate.**
The 47 eval sessions were identified by matching every archived `user_message` against the
frozen prompts in `evals/baseline-tasks.md` — 24 from v1, 23 from v2 — and everything
traceable to them was removed through the product's own commands, after `agent backup`:

| What | How many | Disposition |
|---|---|---|
| facts | 4 | `agent memory retract`, reason recorded |
| open loops | 4 open (2 already closed) | `agent loops close` |
| goals | 1 | `agent goals drop` |
| markdown | 2 files | `regenerate_markdown()`, 2 commits in the memory repo |

The four facts were B20's calendar-freshness preference (twice, once per run, plus a merged
restatement) and a duplicate of the genuine "building a personal agent runtime" fact that
B08 caused. **One had reached `profile/preferences.md`** — `promoted_to_md = true` — so the
eval prompt had become a bullet in the file the agent reads as Dylan's stated preferences.
That block now reads `_Nothing established yet._`, which is the true state: no preference in
this store was ever stated outside an eval.

Two notes for 8d. `candidate_memories` was left intact as the proposal log — the beliefs are
retracted, and rewriting what the gate decided would be a second falsification. And
`retract` is a database write only: it does not touch the markdown, so a cleanup that skips
`regenerate_markdown()` leaves the retracted fact sitting in `preferences.md`.

Found while doing it: **`agent goals drop` prints `dropped` whether or not it matched.** It
takes a slug and runs `UPDATE goals SET status=… WHERE slug=%s` (`repo_agenda.py:62`)
without checking rowcount, so an id — which is what `agent goals list` shows — reports
success and changes nothing. Same signature as the rest: wrong, plausible, never an error.

### v2-4. `invalid_args` fired for the first time

B06's graded turn recorded one `invalid_args` selection failure — a kind that was 0 in v1.
Not diagnosed; the turn passed.

---

## 7. Deviations from v1's method

Four. Each is recorded rather than silently substituted.

1. **The run took two windows ~12 hours apart, not one 29-minute window.** B01–B10 ran
   02:06Z–02:55Z and B11–B22 ran 14:58Z–15:57Z on 2026-09-22; the gap was tooling stalls on
   this session's side, not the system's. Model, daemon pid, feed credentials and commit
   were re-verified at 15:03Z and were unchanged across the gap. **What it costs:** the live
   data moved between the two halves. It does not cross-contaminate rows — every `ask` is a
   fresh one-shot session — but the five live-data rows most sensitive to it (B04–B08) all
   ran inside the first window, within two minutes of each other, as in v1.
2. **B06's fault window was 43 minutes, not ≈4.** The run condition asks for past
   `2 × poll_interval_s`. v1 came in 20s short and said so; v2 ran long for the same
   tooling reason as deviation 1. It does not affect the signal, for v1's reason: the
   connector was disabled by hand, so `calendar_upcoming` surfaces `disabled` directly
   rather than inferring staleness from age.
3. **The run tree was `main` at `bb31388`, not a detached HEAD at the tag.** `main`,
   `HEAD` and `eval-baseline-v2` are all `bb31388`, so the tree content is identical to
   what a `git switch --detach eval-baseline-v2` would produce. Not detached, deliberately:
   this repo is edited in parallel and moving HEAD out from under a working session is not
   worth a cosmetic match.
4. **The daemon was restarted; v1's was not.** v1 ran with one daemon, up since before the
   run, throughout. v2 required a restart because pid 4814 predated `bb31388` and was
   running the unfixed code. This is required by the method rather than a departure from
   it, but it means the daemon's own turns in this run have a cold cache and a fresh process
   where v1's had 31 hours of uptime.

**Not deviations, checked and identical:** model and quantization, host, the four live
feeds and the two uncredentialed ones, autonomy, per-task method, prompt text, grader
vocabulary, telemetry rotation, live accounts and live stores (no scratch DB).

**Reset performed after the run:** `gcal-nyu` re-enabled (verified `enabled`, polling); the
B12 mutation patch reversed and `git status` verified clean; no `git checkout -- src tests`
was run, because the tree was already clean and this repo is edited in parallel. **Not
reset, deliberately, because it is evidence:** B09's two registrar loops and B20's pending
candidate are left in place for whoever reads §2.

---

## 8. Why a v1 → v2 delta is not attributable to the fix — read this before quoting one

`eval-baseline` is `c97d3ab`; `eval-baseline-v2` is `bb31388`. **They are 23 commits apart,
and the duplication fix is one of them.** Between the two tags landed all of pass 02 (the
run journal as the only event path), pass 03 (mandatory `effect_class`, the effect ledger,
idempotency key derivation, four sessions of tool classification), pass 04 (checkpoint
record, resume and reconciliation, `uncertain` as a first-class observation, fork and
disclosure) and pass 05 (context budget watching, the handoff object and successor) —
**42 source files, +7,905 / −220 lines.**

The brief for this session described `bb31388` as a bug fix that "changes every prompt this
runtime sends", which is true, and asked for the delta on that basis. But the prompt this
runtime sends changed in more ways than one between the two measurements: the system block
grew a handoff section, memory retrieval changed, and the largest observed prompt went
**up** by 75% (6,251 → 10,979) across a change whose only prompt-size effect was to remove
~34 tokens.

**Therefore:**

- The two grade moves (B02, B05) are single samples from a temperature-0.7 model on rows
  whose mechanism did not change, and §2 says so for each. **Neither is evidence about the
  fix.**
- The aggregate is 5/4/11/2 against 5/3/12/2 — one row of movement, inside sampling noise
  for a 22-row suite.
- **The only number in this file that is attributable to the fix is §4's**, because it is
  measured directly from the archived messages rather than inferred from a difference
  between two runs of different code.
- A clean measurement of the fix alone would have been `c97d3ab` vs `c97d3ab + the loop.py
  hunk`, which is not what was run and is not what v1 pinned. **It is not available now and
  cannot be recovered from these two files.**

This does not make the re-run worthless — it re-establishes the comparison point for passes
8, 9 and 10 against the code that actually ships, which is what 8d needs. It does mean
"the fix moved row X" is a claim this document does not support for any X.

---

## 9. What this run did not measure

- **Token cost** — router-side, unchanged since v1.
- **B11, B12, B13, B21 under `chat`, and B23** — not run, in either baseline. **Owed before
  8a.** §1.
- **The handoff threshold on the path it is for** — B23. §5.
- **The private-data interlock** (B18) — still no link in the mail. Uncovered in both runs.
- **Memory recall with provenance** (B19) — the belief is still not in memory. Uncovered in
  both runs.
- **Near-limit context behaviour** — v1 stopped early, v2 timed out. Uncovered in both runs.
- **Trace correlation** — `trace_id` and `span_id` are `null` on all 31 records, as in v1
  and as in Pass 1a.

## 10. Reproducing this

```
git switch --detach eval-baseline-v2              # bb31388
systemctl --user restart agent-daemon.service     # the daemon must run the fixed code
mv ~/.local/share/agent/logs/telemetry.jsonl \
   ~/.local/share/agent/logs/telemetry-$(date +%Y%m%d)-passNN.jsonl
# then §2 of evals/baseline-tasks.md, per-task Method, and §6's reset afterwards
```

Check what SIR is actually serving before starting — a live completion, not just
`/v1/models`, which lists both residents. If it is not `Qwen/Qwen3.8-27B-FP8`, neither this
file nor `baseline.md` is a valid comparison.
