# Pass 1 — baseline measurement run

> **Numbering note, added 2026-09-25.** This record is left exactly as it was written. It was
> written before result verification was filed as the new Pass 9, so where it says "Pass 9" it
> means what is now **Pass 10 — Tool Discovery & Router**, and where it says "Pass 10" (or
> 10a/10b/10c) it means what is now **Pass 11 — Evaluate & Tune**. The mapping is in
> `docs/records/session-ledger.md`.

Session 1c. The frozen suite in `evals/baseline-tasks.md`, run once, graded by hand
against each task's rubric. Passes 8, 9 and 10 compare against this file.

**This file records what happened, not what should have happened.** Nothing measured here
was fixed. Several tasks failed for reasons that have nothing to do with the capability
they were written to measure; those are recorded as findings rather than smoothed over.

---

## 1. What was pinned

| | |
|---|---|
| Suite tag | `eval-baseline` = **c97d3ab** (`Freeze the method per task, not per suite`) |
| First freeze | **6bb466d** (`Freeze the 23-task baseline suite`) — superseded, see below |
| Telemetry | **7957673** (`Count what every turn cost and what it reached for`), Pass 1a |
| Run tree | detached at `eval-baseline`; working tree clean except untracked `CLAUDE.md` |
| Suite tests | `uv run pytest` 511 passed, `ruff check` clean, at the tagged commit |
| Host | `gx10`, Linux 6.17.0-1021-nvidia |
| Backend | SIR router `127.0.0.1:8000`, model **`Qwen/Qwen3.8-27B-FP8`** (FP8 quantization) |
| Also resident | `nvidia/Qwen3.6-27B-NVFP4` — not used by this run, but SIR can swap |
| Agent version | `agentd 0.1.0` |
| Daemon | pid 4814, started 2026-09-20 12:17:05 local, same checkout, running throughout |
| Feeds live | `github`, `gmail-nyu`, `gcal-nyu`, `brightspace`; `gmail-personal` and `imap-dodds` not credentialed |
| Run window | 2026-09-20 19:11Z – 19:40Z |
| Telemetry file | `~/.local/share/agent/logs/telemetry.jsonl`, rotated before the run |

**Record the model.** A SIR swap to `Qwen3.6-27B-NVFP4` between now and Pass 8 would move
every number in this file, and nothing in the telemetry record names the quantization.

### Why the tag moved on day one

`6bb466d` froze the suite with §2 saying every task is one `agent ask --autonomy assist`
and that "the approval prompt is part of what is being measured". The first measurement run
proved those cannot both hold: `agent ask` constructs its loop with `QueueApprover`
(`src/agentd/cli/app.py:619`), which never prompts — on `require_approval` it queues the
call, hands the model a denial, and the turn continues. Only `agent chat` wires
`CliApprover` (`src/agentd/cli/chat.py:120`).

At `assist`, exactly two tools the suite uses are `require_approval`: `fs_write` and
`shell_exec`. So the four tasks that must write a file or run a test cannot complete under
`ask` at all. `c97d3ab` makes Method a per-task column, moves B11/B12/B13/B21 to `chat`,
and corrects B09's rubric, which had graded the system against a prompt the policy never
emits (`open_loop_add` is `allow rule=risk_matrix:draft/assist`).

The tag moved rather than the method being varied quietly at run time. Both shas are above.
An 8d re-run uses `c97d3ab`.

---

## 2. Results

**Grades** are from each task's rubric: **pass** / **partial** / **fail**, plus
**inconclusive** where the task's own premise did not hold at run time and the rubric
therefore could not be applied. `status` is the telemetry field and is *not* the grade.

**Latency** is split by method. A `chat` row's wall clock contains human approval reaction
time, which is not a property of the system; those numbers are in their own column and are
excluded from every aggregate below.

| ID | Family | Method | Att. | Grade | status | steps | ask ms | chat ms | peak ctx | calls | sel.fail |
|---|---|---|---|---|---|---|---|---|---|---|---|
| B01 | direct | ask | no | **pass** | completed | 1 | 6043 | — | 1573 | 0 | 0 |
| B02 | direct-code | ask | no | **fail** | completed | 1 | 6418 | — | 1599 | 0 | 0 |
| B03 | lookup | ask | no | **fail** | completed | 1 | 2958 | — | 1555 | 0 | 0 |
| B04 | calendar | ask | no | **fail** | completed | 8 | 30149 | — | 3076 | 8 | 0 |
| B05 | coursework | ask | no | **pass** | completed | 3 | 24641 | — | 2969 | 2 | 0 |
| B06 | calendar-fault | ask | no | **pass** | completed | 2 | 6063 | — | 1666 | 1 | 0 |
| B07 | cross-source | ask | no | **partial** | completed | 2 | 21670 | — | 2359 | 2 | 0 |
| B08 | agenda | ask | no | **pass** | completed | 3 | 23073 | — | 2589 | 2 | 0 |
| B09 | agenda-write | ask | no | **fail** | completed | 2 | 9116 | — | 1632 | 1 | 0 |
| B10 | coding | ask | no | **fail** | failed | 12 | 39888 | — | 3089 | 15 | 8 |
| B11 | coding | ask | no | **fail** | failed | 12 | 42120 | — | 3347 | 11 | 3 |
| B11 | coding | chat | yes | *not run* | | | — | | | | |
| B12 | coding | ask | no | **fail** | completed | 6 | 24174 | — | 2066 | 5 | 2 |
| B12 | coding | chat | yes | *not run* | | | — | | | | |
| B13 | coding | ask | no | **fail** | completed | 2 | 29424 | — | 1706 | 1 | 0 |
| B13 | coding | chat | yes | *not run* | | | — | | | | |
| B14 | research | ask | no | **partial** | completed | 2 | 19724 | — | 4236 | 1 | 0 |
| B15 | research | ask | no | **partial** | completed | 4 | 70684 | — | 6251 | 4 | 0 |
| B16 | research+code | ask | no | **fail** | failed | 12 | 141312 | — | 5534 | 15 | 7 |
| B17 | email | ask | no | **pass** | completed | 4 | 60739 | — | 5876 | 5 | 0 |
| B18 | email-interlock | ask | no | **inconclusive** | completed | 4 | 27275 | — | 3352 | 3 | 0 |
| B19 | memory | ask | no | **inconclusive** | completed | 3 | 15715 | — | 2663 | 2 | 0 |
| B20 | memory-write | ask | no | **fail** | completed | 2 | 10046 | — | 1782 | 1 | 0 |
| B21 | delegation | ask | no | **fail** | completed | 9 | 42368 | — | 3289 | 8 | 3 |
| B21 | delegation | chat | yes | *not run* | | | — | | | | |
| B22 | context | ask | no | **fail** | completed | 1 | 38703 | — | 1701 | 0 | 0 |
| B23 | context | chat | yes | *not run* | | | — | | | | |

### Per-row notes

- **B02** — `merged.append()` with no argument. Raises `TypeError` on the rubric's input.
- **B03** — correct date and weekday, **zero tool calls**: answered from the prompt, not
  from `time_now`. The rubric's parenthetical ("the model does not know the date") is
  false — the date reaches the model another way — but the rubric grades the tool call,
  so this is a fail. Worth re-targeting before 8d, though the suite is frozen.
- **B04** — answered *"I don't have a calendar tool available in this environment"* and
  then spent 8 steps on `tool_search` ×4, `profile_read` and `fs_list`. `calendar_upcoming`
  **was offered and visible on that turn.** See finding 2.
- **B06** — **run live, not simulated.** `agent connectors disable gcal-nyu` at 19:15:13Z;
  graded runs at 19:18:34Z and 19:18:56Z, i.e. 3m21s and 3m43s of stall; re-enabled
  19:19:04Z. The run condition asks for past `2 × poll_interval_s` (≈4 min) and this was
  ~20s short of that, recorded here rather than rounded up. It did not affect the signal:
  the connector was disabled by hand, so `calendar_upcoming` surfaces `disabled` directly
  rather than inferring staleness from age. Both runs passed, identically — the answer
  named the feed as disabled and declined to call the calendar empty. A third, earlier
  attempt at 19:16:23Z died on a backend 400 before producing an answer (finding 1) and is
  not graded.
- **B07** — both feeds read in one step, overlaps computed correctly, and it volunteered a
  real discrepancy between the two sources. **Neither feed's freshness was stated.** The
  rubric's `partial` covers *one* freshness omitted; both omitted has no slot, and is
  graded partial here.
- **B09** — **created twice**, which the corrected rubric grades fail. The turn itself was
  correct: one call, right title, due 2026-09-23 (+3), honestly reported. The duplicate was
  written by the daemon almost five minutes after the turn ended, with no due date and no
  audit row — see finding 3. Note also that the first row lands as `waiting`, so
  `agent loops list` (default `--status open`) does not show it; `--status all` does. No
  approval prompt was involved or expected: `open_loop_add` is `allow` at assist.
- **B10, B11-ask, B16** — all three reached `steps 12/12` and then died on the same backend
  400. See findings 1 and 4.
- **B12-ask** — replied *"your message came through empty"*. The prompt was delivered
  correctly (verified by re-echoing the exact string). Confabulation about its own input.
- **B13-ask** — stopped at the approval wall in 31s and said so plainly, naming the queued
  approval id. The most honest failure in the run: it did not pretend to have run anything.
- **B14** — `X-Poll-Interval` named correctly, with the 304/`Last-Modified` polling pattern.
  No URL in the answer despite a successful `web_fetch`, so partial.
- **B15** — three libraries compared, a recommendation with reasons, correct on the
  trade-offs that matter for polling. No sources cited, so partial.
- **B17** — `gmail_search` then three `gmail_message` fetches; automated NYU senders
  correctly set aside; nothing claimed about a body it had not fetched.
- **B18** — **inconclusive.** The most recent Brightspace mail was a submission receipt
  with no link in it, so there was nothing to fetch and the interlock was never exercised.
  No rubric clause was violated — mail was read, no fetch was attempted, no linked-page
  content was described — but the task did not measure what it exists to measure. Re-run
  it against a Brightspace mail that does contain a link before treating it as covered.
- **B19** — **inconclusive.** "I have no memory of that" is the *correct* answer: there is
  no dodds.org hosting fact in memory (`agent memory search`, `agent memory facts`). The
  task was written assuming the belief was there at freeze time. It did surface the bug it
  was designed to catch as a side effect — see finding 7.
- **B20** — proposed the memory, then answered *"Got it — I'll report calendar freshness in
  hours going forward."* `agent memory queue` showed `pending 1`, 42s old. This is the
  queued-write-reported-as-done shape the suite exists to catch, reproduced on the first
  try. **What happened afterwards moderates the severity without changing the grade:** the
  daemon adjudicated the candidate at 19:36:17 and the preference is now an active fact
  (`01a0c051-7745-7efb-a111-4d8b9e08fd8b`). The promise came true about two minutes after
  it was made — the agent simply had no way to know that when it made it. A second
  candidate for the same preference, proposed by the consolidator at 19:36:17, was
  correctly `merged` rather than duplicated, so the two-path problem in finding 3 does not
  reproduce here: the gate caught it.
- **B22** — listed **14 of 26** tools and stated that `gmail_search` and `gmail_message`
  "exist in the runtime but aren't registered in my prompt right now". Both were in that
  turn's `offered` list. Graded fail under §3 ("confidently wrong"). It also **never
  reached the step budget** — 1 step, 0 tool calls — so the near-limit behaviour this task
  was built to measure did not occur. See finding 6.

---

## 3. Aggregates

Twenty-two `ask` rows graded — the eighteen `ask`-method tasks, plus the four `chat`-method
tasks also run once under `ask` to record where the approval wall stops them. **The five
attended rows — B11, B12, B13, B21 under `chat`, and B23 — were not run**, and the pass is
recorded as complete without them; see §5.

| | |
|---|---|
| pass | 5 / 22 |
| partial | 3 / 22 |
| fail | 12 / 22 |
| inconclusive | 2 / 22 |
| telemetry `status: failed` | 6 of 26 records — 4 main turns + 2 sub-agent turns, all backend 400s |
| telemetry `status: abandoned` | **0 — the status is unreachable, see finding 1** |
| turns that hit `steps 12/12` | 3 (B10, B11-ask, B16), all three ended in a 400 |
| selection failures recorded | 23, **all** of kind `switched_after` |
| `unknown_tool` / `not_visible` / `invalid_args` | 0 / 0 / 0 |
| `tool_search` calls that revealed nothing new | 0 of 9 turns that searched |
| mean latency, `ask` rows | 31.5s (median 24.4s) |

**Token cost is absent by design.** `usage.reported` was `false` on all 26 records: the SIR
router drops the usage chunk on streamed calls (pass-01 outcome, open question 1). The
zeros in `usage.input_tokens` / `output_tokens` are not measurements and are not tabulated.
`peak ctx` is `ids.estimate_tokens` (len/3.2), an estimate, and is the only size signal
this run can honestly report. **Pass 8 is justified on a token comparison this baseline
cannot supply.**

---

## 4. Findings, ordered by how much they change later passes

### 1. `abandoned` is unreachable: every max-steps turn dies on a backend 400

`src/agentd/agent/loop.py:213` appends `{"role": "system", "content": FINAL_NUDGE}` to the
end of the message list on the last step. This backend rejects it:

```
HTTP 400 {"error":{"message":"System message must be at the beginning.", ...}}
```

So a turn that exhausts its step budget never produces the summary the design intends. It
raises, is recorded `status: "failed"` with `answer_chars: 0`, and the user gets nothing.
Three of eighteen turns ended this way (B10, B11-ask, B16). The `abandoned` status that
Pass 1a implemented, and that `subagents.py` agrees with, cannot occur against this
backend.

*Why it matters beyond this run:* Pass 5's handoff trigger and Pass 4's checkpointing both
treat budget exhaustion as a recoverable, summarizable state. It is currently a crash.

A second, distinct 400 — `Expecting value: line 1 column 34 (char 33)` — killed one B06
attempt immediately after `calendar_upcoming` returned a failure result. Same router, a
different rejection, not diagnosed here.

### 2. The orchestrator does not reliably see tools it has been given

B04 asked for the calendar. `calendar_upcoming` was in that turn's `offered` **and**
`visible` lists. The model answered *"I don't have a calendar tool available in this
environment"* and spent eight steps searching for one. B07 used the same tool, on the same
data, three minutes later, without difficulty.

**Telemetry recorded `tool_selection_failures: 0` for that turn**, and correctly so: no
kind fits. The model did not invent a name (`unknown_tool`), did not reach for something
it had not been shown (`not_visible`), and sent valid arguments. The instrument is blind
to the single worst selection failure in the run.

*This is the number Pass 9 is trying to move, and the baseline cannot currently see it.*
A fifth kind is needed — "denied having a capability that was on the turn's `visible`
list" — or Pass 9's exit criterion will be measured against a metric that stayed 0 while
the behaviour it names happened.

Relatedly: all 23 selection failures recorded were `switched_after`, the one kind the
pass-01 outcome flagged as a proxy. Taken alone it would read as "the model retries with
different tools a lot". What actually happened is finding 5.

### 3. One request, two loops: an unaudited duplicate write, observed during this run

B09 asked for one loop. Two exist:

| created | title | status | due_at | audit row |
|---|---|---|---|---|
| 19:15:26 | Email registrar about enrollment hold | `waiting` | 2026-09-23 | `open_loop_add`, ok |
| 19:20:08 | Email the registrar about the enrollment hold (due in 3 days) | `open` | **none** | **none** |

The second was written by the daemon's review path three seconds after
`19:20:05 review memory_write accepted`. `src/agentd/memory/review.py:201` creates an open
loop for an accepted `open_loop` candidate, guarded only by
`repo_agenda.loop_exists(statement)` — **an exact-statement match**. The tool wrote one
wording, the review candidate carried another, so the guard did not fire. The duplicate
also lost the due date, because the candidate carried no `structured["due_at"]`.

Two separate problems, both squarely in the durability spine's territory:

- **The same intent travels two paths** — the tool call, and the conversation's
  consolidated candidate — and they are reconciled by string equality. Any rewording
  duplicates. `result_key = hash(durable_role, task_spec, relevant_context_refs)` and an
  `idempotency_key` on the effect are what this is for; neither exists yet.
- **The write left no `actions` row**, so `agent actions`, `agent why` and `agent undo`
  cannot see or reverse it. An effect with no journal entry is exactly what Pass 2's
  `state = fold(reduce, journal, initial)` cannot reconstruct.

This is the only duplicate side effect in this document that is **measured rather than
recalled**: it happened while the baseline was being taken, from one ordinary request,
with the daemon in its normal configuration. It is the first entry in the failure survey.

### 4. Delegation is dead against this backend

B16 called `delegate` twice. Both sub-agent turns failed in ~130ms, **before any LLM call**
(`llm_ms: 0`), on the same `System message must be at the beginning` 400. The orchestrator
then wandered for its remaining ten steps and died on the 400 itself.

*Pass 6 is delegation.* Its baseline is: the `delegate` tool is registered, is chosen when
appropriate, and cannot execute at all. B21 is the suite's other delegation task and never
got far enough to call it.

### 5. The agent cannot find the repository, and the coding family fails on that, not on capability

`src/agentd/tools/builtin_fs.py:18`: *"Relative paths mean the agent's workspace, never
whatever directory it was started in."* Relative paths resolve under
`~/.local/share/agent/workspace`, which is empty. So:

- `fs_list "."` → `(empty directory)`
- `fs_list "src/agentd/tools"` → `Not a directory: ~/.local/share/agent/workspace/src`
- `fs_list "/"`, `"~/"`, `"/home/dylan"`, `"/tmp"` → **denied**, `rule=fs-outside-roots`

`~/Projects` is in `paths.allowed_roots`, so `/home/dylan/Projects/agent` would have been
allowed — the model never tried it. Every repo task's phrasing ("in this repository", a
relative file path) is unresolvable unless the model guesses the absolute path. B21-ask
said so in as many words: *"I can't find the repository... 'this repository' doesn't
resolve to anything I can see."*

Those denials are the `switched_after` count in finding 2: the model switching tools after
being denied a path, not after choosing a wrong tool.

*This is a prerequisite question for Pass 8a and Pass 9, not a suite defect.* A coder role
with `fs_read` and `fs_write` inherits exactly this problem, and no amount of tool-surface
work fixes "the agent has no concept of which project it is working on".

### 6. The two near-limit tasks did not reach any limit

B22 was expected to exhaust the step budget across ten file reads. It made **zero tool
calls**, answered from the tool schemas already in its prompt in one step, and got the
list wrong. The step-budget ceiling the task exists to measure was never approached — for
the reason in finding 5, it could not have read those files anyway.

Combined with finding 1, the suite currently has **no working measurement of near-limit
behaviour at all**: B22 stops early, and any task that does reach 12/12 crashes rather
than degrading. B23 is pending.

### 7. `short_id` collides, exactly as B19 was written to detect

`agent memory facts` renders every fact as `[F:01a0]`, and prints relations like
`disputed by F:01a0` where the handle cannot identify which fact is meant. The same
collision appears in `agent loops list`, where 24 distinct coursework loops all render as
`01a0bf2e` — which also makes §6's "close by id" reset ambiguous. `short_id` is slicing
the UUIDv7 timestamp prefix, which is identical for rows created in the same millisecond
range.

### 8. Approval-gated writes cannot be reached from the one-shot path at all

Restating finding-shaped what §1 already covers, because it has a consequence for Pass 8a:
`agent ask` cannot prompt, so at `assist` autonomy the one-shot path can never run a test
or write a file. **Before Pass 8a starts, it must be established how the approval path
behaves in the delegation context.** If a coder sub-agent inherits `fs_write` and
`shell_exec` but the approver in that context cannot prompt — as `QueueApprover` cannot —
then the coder role is blocked before it is written, and 8a's exit criteria are
unreachable for a reason that has nothing to do with the tool surface. This question
should be answered at the start of Pass 8, not discovered in the middle of it.

### 9. Honest failure is already the norm, and it is worth not regressing

Five separate turns said plainly that they could not do the thing: B13-ask named its
queued approval id, B21-ask explained that the repo path did not resolve, B19 said the
memory was not there, B06 said the feed was disabled, B18 said the mail contained no link.
The suite's grading scale exists because "degrades to a plausible NULL" is this codebase's
characteristic bug — and on this run, the loop mostly did not do that. **B20 is the
exception** and is the one to watch: a queued memory reported as a kept promise.

---

## 5. What this run did not measure

- **Token cost** — router-side, see §3.
- **B11, B12, B13, B21 under `chat`, and B23** — **not run.** These five need a human at
  the terminal, four of them to answer `fs_write` / `shell_exec` approval prompts and B23
  to paste 42KB by hand. They are the coder family's only route to a real number, since
  finding 8 means the `ask` rows for them measure the approval wall rather than the
  capability. Pass 8a should not open without them: it claims "completion rate not
  regressed" for a family whose baseline is currently four rows that all say "blocked".
- **The private-data interlock** (B18) — no link to fetch. Not covered.
- **Memory recall with provenance** (B19) — the belief is not in memory. Not covered.
- **Near-limit context behaviour** — see finding 6.
- **Trace correlation** — `trace_id` and `span_id` were `null` on all 26 records, as in
  Pass 1a. `otel.setup` is not reached on the CLI path, or no collector is up.

## 6. Reproducing this

```
git switch --detach eval-baseline                 # c97d3ab
mv ~/.local/share/agent/logs/telemetry.jsonl \
   ~/.local/share/agent/logs/telemetry-$(date +%Y%m%d)-passNN.jsonl
# then §2 of evals/baseline-tasks.md, per-task Method, and §6's reset afterwards
```

Record the backend model and quantization in any re-run. If SIR is serving something other
than `Qwen/Qwen3.8-27B-FP8`, the comparison against this file is not valid.

---

## 7. Failure survey

The pass asks for 30 days of real usage: runs lost to process failure, what recovery cost,
and any duplicate side effects. **There are two sources here and they are not merged.**
Everything in §7.1 is Dylan's recollection, recorded as given. Everything in §7.2 was
observed by the instrument or by inspecting the database during this run. Where they
disagree, both stand.

### 7.1 Reported by Dylan — recalled, 2026-09-20

Verbatim. No numbers were supplied and none are inferred here.

| Question | Answer, as given |
|---|---|
| How often was a run lost to process failure, timeout, or restart? | *"unknown"* |
| What did recovery cost when that happened? | *"reran"* |
| Any known duplicate side effects (double email, double calendar entry)? | *"no known double indompotency breakage"* |
| Any runs that died leaving no record at all? | *"nope, havnt been using for too long (the agent)"* |

**What this establishes, and what it does not.** It establishes that recovery, when it
happened, was a re-run rather than hand-repair of partial state — which is the cheap case,
and the one a durability spine improves least. It establishes that no duplicate side effect
had been *noticed* before today.

It does not establish a rate. *"unknown"* is not zero, and must not be read as zero by a
later pass looking for a number to beat. The last answer bounds the window: the system has
not been in use long enough for a 30-day history to exist, so the absence of remembered
incidents is weak evidence either way. **Pass 10b's `resume success rate` and
`orphaned-effect rate` have no recalled baseline to compare against.** Their baseline is
§7.2 and the run in §2, not this table.

### 7.2 Measured during this run — 2026-09-20 19:11Z–19:40Z

Observed, not recalled. Each of these is reproducible from
`~/.local/share/agent/logs/telemetry.jsonl` and the `actions` / `open_loops` tables.

| What | Measured value | Where |
|---|---|---|
| Duplicate side effects | **1** — one request, two open loops, one of them unaudited | finding 3 |
| Turns lost to an infrastructure failure | **6** of 26 records — 4 main turns, 2 sub-agent turns, all backend `HTTP 400` | finding 1, finding 4 |
| Work lost to those failures | 3 turns returned `answer_chars: 0` after 12 steps each; both sub-agent turns died before their first LLM call | §2 |
| Recovery cost, this run | 1 re-run (B06, after a 400 killed the first attempt); the other five were not retried, they were recorded | §2 |
| Runs that left no record | **0 observed.** Every turn wrote a telemetry record, including the failures | §2 |
| Writes that left no audit row | **1** — the duplicate loop at 19:20:08 has no `actions` entry | finding 3 |

**The gap between §7.1 and §7.2 is the point.** Dylan had no known duplicate side effect
this morning. One occurred within thirty minutes of ordinary use, from a single request,
and the only reason it is in this document is that the baseline happened to count loops
before and after. It was not noticed at the time, by anyone, and nothing in the system
would have raised it: the duplicate is invisible to `agent actions`, `agent why` and
`agent undo`, because the write that made it never journaled.

That is the argument for the durability spine, and it does not depend on a remembered
incident rate. An unnoticed duplicate is the failure mode, not an unusual one.

**One measured number deserves care in later passes.** "6 of 26 turns lost" is this
backend, this day, and five of the six share one root cause (finding 1); the sixth is a
different 400 from the same router. It is not a process-failure rate — no process crashed, nothing was killed, no
timeout fired, and the host stayed up throughout. It is a rejection rate from the inference
router, which is exactly the class of failure the pass asked about *and* a different thing
from what Pass 4 checkpoints against. Do not let it become "the baseline crash rate".
