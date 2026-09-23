# Session ledger

Single source of truth for orchestration state. Survives a `/clear`. Update after every
state change, before reporting.

Status: `pending` | `awaiting-human` | `dispatched` | `complete` | `blocked`.

| session | status | dispatched | outcome record | notes |
|---|---|---|---|---|
| bootstrap | complete | human | — | CLAUDE.md commands filled in; still untracked in git |
| 1a | complete | — | pass-01-outcome.md | telemetry emission, flat JSONL |
| 1b | complete | human | pass-01-outcome.md | task suite frozen, 23 rows |
| 1c | complete | human | pass-01-outcome.md + baseline.md | 22 of 23 rows graded; 5 human-at-terminal rows not run |
| 2a | complete | — | pass-02-outcome.md | journal store + writer. Committed as fe8a8e0. |
| 2b | complete | 2026-09-20 | pass-02-outcome.md | 17-type vocabulary, 9 of them emitted. No Must not crossed. |
| 2c | complete | 2026-09-20 | pass-02-outcome.md | committed as b34f153. In-memory UIEvent bus deleted; journal is the only event path. 8 deviations, none crossing a Must not. Kill test passed at 3 points. |
| 3a | complete | 2026-09-20 | pass-03-outcome.md | committed as cae2f75. effect_class mandatory, no default; 26/26 builtins declare UNAUDITED (= unsafe_write), none classified. 5 deviations, no Must not crossed. |
| 3b | complete | 2026-09-20 | pass-03-outcome.md | committed as 0eda557. effect table (schema v2) + idempotency keys + announce-before-dispatch in the executor. 7 deviations, no Must not crossed. |
| 3c | complete | 2026-09-20 | pass-03-outcome.md + effect-classification.md | committed as 100e0ff. 19 of 26 ruled (11 read, 2 idempotent_write, 6 unsafe_write); UNAUDITED 26 → 7. 5 deviations, no Must not crossed. **Ran autonomously under the standing policy in place of its supervised hard stop — the table still needs Dylan's review before Pass 4.** |
| 3d | complete | 2026-09-20 | pass-03-outcome.md + effect-classification.md | committed as a5ba017. Last 6 ruled: fs_write/shell_exec/web_fetch unsafe_write, gmail_search/gmail_message/web_search read. UNAUDITED 7 → 1. 5 deviations, no Must not crossed. No live external call was made. **Ran autonomously in place of its supervised hard stop — needs Dylan's review before Pass 4.** |
| 4a | complete | 2026-09-20 | pass-04-outcome.md | checkpoint record + schema v3 + 5 triggers (3 wired live), behind `[checkpoints] enabled=false`. 682 tests green. 8 deviations, no Must not crossed. ~1.3 ms / 573 B per checkpoint, ~90% of it fsync. |
| 4b | complete | 2026-09-20 | pass-04-outcome.md | resume.py: fold → rehydrate → reconcile → announce. `agent journal resume` reports by default. 711 tests green. 11 deviations, no Must not crossed. Two decisions handed to 4c. |
| 4c | complete | 2026-09-20 | pass-04-outcome.md | **hard stop — human runs this** (user-facing wording). Ran autonomously under the standing policy in place of that stop: the wording is proposed in the record with 7 rejected variants and **still needs Dylan's review at the pass boundary**. 729 tests green. 8 deviations, no Must not crossed. Ruled both of 4b's handed-up decisions. |
| 4d | complete | 2026-09-20 | pass-04-outcome.md | fork.py + `agent journal fork`. Parent bit-for-bit untouched; disclosure has three lists, not one. 751 tests green. 10 deviations, no Must not crossed. Pass-level exit criteria met. |
| 5a | complete | 2026-09-21 | pass-05-outcome.md | `agent/budget.py` + `[handoff]`; ceiling is `agent.history_tokens` 24000, **not** `max_context_tokens` 262144. Threshold 8000 kept. 767 tests green. 6 deviations, no Must not crossed. Gives the `handoff` checkpoint trigger its first producer. |
| 5b | complete | 2026-09-21 | pass-05-outcome.md | `agent/handoff.py`: object, validator, generator, successor block; stored in `handoff_object` on the `turn_end` checkpoint. 806 tests green. 6 deviations, no Must not crossed. **Exit criterion partly met — 2 of the required 3 suite tasks.** |
| 5c | complete | 2026-09-22 | pass-05-outcome.md | committed as 6113734. The manifest (with tool results in it), cold resume (`agent/rehydrate.py` + `agent journal continue`), requirement B's rerun guard, `assistant_step` archiving, `handoff_schema` 2 with a real v1 upgrade. 846 tests green. 16 mutations, all caught. A failing test found a third resume reason nobody designed - `already_handed_off`. |
| 5d | complete | 2026-09-22 | pass-05-outcome.md | committed as ae2d6ea. `handoff_lookup`: one ref, a bounded excerpt, model-invoked only, journaled like any other tool call so Dylan's per-turn lookup count is a query and not a mechanism. 856 tests green. 8 further mutations, all caught. |
| 6a | complete | 2026-09-22 | pass-06-outcome.md | `agent/delegation.py`: `TaskSpec` (normalized in `__post_init__`, sha256 `digest`) + `delegate(...)`; `run_subagent` takes a spec and raises on a string; `worker_created` carries `task_digest`. 882 tests green. 9 deviations, no Must not crossed. **Wrote 7 events into the LIVE journal by accident** (run id `run-tool`, no effects, no checkpoints — verified); left in place, `conftest.py` monkeypatch list fixed. |
| 6b | complete | 2026-09-22 | pass-06-outcome.md | `agent/results.py`: `WorkerReport` (pydantic, extracted) + `WorkerResult` (frozen, nothing decodes into it), validated in `run_subagent`; prose instead of the schema is `uncertain` with `report_valid=False`, never a degraded success. `[delegation] debug_transcripts` is the explicit debug mode. 900 tests green. 9 deviations, no Must not crossed. **Found and closed unasked:** `repo_archive.recent_messages` rebuilt a worker's final prose into the orchestrator's next-turn history — the pass's own rule broken with no tool call involved. |
| 6c | complete | 2026-09-22 | pass-06-outcome.md | `result_key` = `TaskSpec.digest`; the run-scoped cache is folded from two new journal events (`worker_result_cached`, `worker_result_reused`) rather than from the checkpoint, because the live journal holds 0 checkpoint rows across 50 runs. `checkpoint.worker_results[]` filled from the same fold. 920 tests green. 9 deviations. **Vocabulary 17 → 19**, both named in the drift guard. Reuse rate 3/3. No Must not crossed; cross-run reuse pinned by two tests. |
| 7a | complete | 2026-09-23 | pass-07-outcome.md | **hard stop — human runs this** (harness inspection). Ran autonomously under the standing policy ("Harness inspection (7a): dispatch it. It writes a finding and changes no code."); the marker stands and **the finding is still Dylan's to read at the pass boundary.** No code touched, 920 tests green (baseline, unchanged), ruff clean. 1 deviation, no Must not crossed. One physical store, four record types, **no scope dimension — no task-local memory exists today**. Live-data proof: 1140 events / 52 run ids before and after, run-id lists diff empty. |
| 7b | complete | 2026-09-23 | pass-07-outcome.md | three declared buckets (`memory/scopes.py`) + a run+agent-scoped working bucket folded out of the journal (`journal/working_memory.py`, `agent/working_memory.py`, `tools/builtin_working.py`), isolated on `(run_id, scope)` in the query — **not** on the session id, which a worker shares with its caller. Discarded at worker finish and at run end. 937 tests green. 5 deviations, no Must not crossed: no migration in either store, no new column, no checkpoint schema change. **Vocabulary 19 → 21** (`working_memory_noted`, `working_memory_discarded`), both named in the drift guard with their pass. 5 mutations, all caught — the first found a real hole, the two-worker test passed with the scope filter deleted until each worker used a distinct note key. |
| 7c | complete | 2026-09-23 | pass-07-outcome.md | promotion as journaled effects (`promotion_classified`/`promotion_committed`/`promotion_batch`), keyed on **the boundary, not the step**, batched at task (`subagents.py`, after `worker_finished`) and run (`loop.py::_promote`) boundaries; `pending_promotions[]` + `memory_watermark` filled from the same fold (`journal/promotions.py`). Both crash orderings fault-injected and green. 966 tests green. 7 deviations. Must not not crossed, **but one needs Dylan's confirmation**: there IS a migration, `migrations/0010_promotion.sql`. **Mutation finding: deleting the `worker_id` guard in `_promote` broke zero tests** — a worker's turn would have classified and written its *caller's* scope mid-turn; now killed by a named test. **Fixed en route: `classify` used the process-global `get_provider()`, so the test suite was making real calls to the live model endpoint.** |
| 8a | pending | — | — | approval-wall question from 1c is a prerequisite here |
| 8b | pending | — | — | |
| 8c | pending | — | — | |
| 8d | pending | — | — | |
| 9a | pending | — | — | |
| 9b | pending | — | — | |
| 9c | pending | — | — | |
| 9d | pending | — | — | |
| 10a | pending | — | — | |
| 10b | pending | — | — | |
| 10c | pending | — | — | **hard stop — human runs this**, recurring; one question per session |

## Carried forward

- **Pass 1 open question, unresolved into Pass 2:** token accounting is dead — the SIR
  router drops the usage chunk, so `usage.reported` is `false` on every turn.
- **Pass 1 open question, unresolved:** the approval wall blocks 5 baseline rows and is a
  prerequisite question for 8a, not just a suite defect.
- **RESOLVED by 2b:** the budget-exhaustion crash at `agent/loop.py:213` is unchanged (a fix
  is a behaviour change, barred by Must not), but the turn now journals
  `agent_finished status=failed` carrying the HTTP 400, so the sequence is readable. The
  underlying bug is still open and still fails 3 of 22 baseline rows.
- **2b → 2c, before building a feed:** no live turn has ever been journaled;
  `~/.local/share/agent/journal.db` does not exist. Everything in the 2b record is from the
  suite. `tool_finished.trust` and `ToolCall.id` are the two paths most likely to surprise.
- **2b → Pass 3:** `policy/replay.execute_approved` runs a queued approval with
  `ctx.run_id = None` — a tool call journaled by nobody, the same shape as baseline finding 3.
  Tool events are emitted from `loop.py`, not the executor; making `effect_intended`
  unbypassable means moving emission into the executor and taking these with it.
- **RESOLVED by 2c:** a live turn has now been journaled. `tool_finished.trust` and
  `ToolCall.id` were both real on the wire, so the 2b → 2c warning is closed.
- **2c → Pass 4/5, to decide rather than inherit:** whether a run id is ever derived from a
  turn id — the REPL and Telegram mint the run id before the turn, so `run_id != str(turn_id)`
  on those two channels.
- **2c → Pass 4/5:** a retrieval-degraded turn is still invisible to a fold. `Notice` survived
  with one use (memory-retrieval failure) because no event type covers it, and inventing an
  18th was out of 2c's scope.

- **3a → 3b, two decisions 3b must make rather than inherit:** the idempotency key for a
  call with `ctx.run_id = None` (`policy/replay.execute_approved`, the same hole flagged
  2b → Pass 3), and whether the effect ledger lands in the journal SQLite file, which
  would trigger 2a's "`_migrate` cannot migrate".
- **3a → Pass 4, ordering constraint:** Pass 4 must not ship before 3c/3d. Every tool
  currently reads `unsafe_write` via the `UNAUDITED` placeholder, so reconciliation would
  surface `time_now` and `fs_list` as uncertain effects.
- **3a → 3d, pre-empted corner:** MCP tools are pinned to `unsafe_write` at wrap time
  because `wrap()` must pass the field. 3d inherits that as a decision already made.
- **RESOLVED by 3b:** the `ctx.run_id = None` call (`policy/replay.execute_approved`, MCP)
  is keyed `detached:<action_id>`, unique per call, never a shared bucket. The effect
  ledger lives in journal.db under a real migration ladder, so 2a's "`_migrate` cannot
  migrate" is closed rather than triggered.
- **3b → Pass 4, conservative reading to inherit:** `started` is a ledger-only transition.
  Pass 02 fixed the vocabulary at 17 types and a drift-guard test asserts it, so there is
  no `effect_started` event and the ledger holds one bit a cold fold cannot reproduce.
- **3b → 3c, a test that must die:** `tests/test_journal_events.py::test_a_complete_turn_is_readable_from_the_journal_alone`
  asserts an `effect_intended`/`effect_committed` pair around `fs_read`. Classifying
  `fs_read` as `read` must delete that assertion, not work around it.
- **3b → 3c/3d, the cost that makes the audit urgent:** every effecting call now costs four
  synchronous fsyncs, and with all 26 builtins `UNAUDITED` that applies to `time_now`.
- **RESOLVED by Dylan at the Pass 3/4 boundary (both owed rulings):** `memory_search` is
  `read`, as a deliberate exception — non-idempotent counter, harmless drift — ruled after
  confirming `access_count`/`last_accessed_at` feed no ranking or scoring anywhere in
  `src/`. `web_fetch` stays `unsafe_write`: confirmed `GET`-only, no POST, no auth header,
  no cookie jar, and the class rests on argument provenance rather than method.
  `UNAUDITED_TOOLS` is now empty. Full reasoning in `docs/records/pass-03-outcome.md`
  under "Pass 3/4 boundary".
- **Pass 3/4 boundary → Pass 10, a standing collection:** `effect_class` is a property of
  the tool; effect risk is a property of the call. `memory_search` and `web_fetch` are both
  forced into a per-tool class that cannot express the distinction. Do not decide now —
  append every tool that breaks this way and decide against real traces at Pass 10. This
  supersedes 3d's open question 4, which had proposed Pass 8; Pass 8 is tool-surface
  reduction and this is not about where a tool lives.
- **Pass 3/4 boundary → 4b and 4c, binding requirements not suggestions:** the
  reconciliation prompt for an orphaned `web_fetch` must show the URL — "Confirm this
  fetch?" with no URL trains blind confirmation, which is the exact outcome the
  `unsafe_write` ruling exists to prevent. And several orphaned fetches on one resume group
  into a single prompt rather than one each.
- **3d → whoever adds a send path:** the pass file's `gmail send` and `calendar create` do
  not exist in this registry. Gmail send accepts no client-supplied id; Calendar
  `events.insert` does. Nothing is `idempotent_write` today, so the id check gated nothing.

## Pass 3 complete

3a–3d all committed, tests 661 green, ruff clean, no Must not crossed in any session.
The two owed rulings were put to Dylan at the pass boundary and settled before 4a was
dispatched, so the 3a → Pass 4 ordering constraint is satisfied: every tool carries a
recorded ruling and reconciliation will not surface `time_now`, `fs_list` or
`memory_search` as uncertain effects. **Pass 4 is clear to start.**

- **4a → 4b, two handoffs:** no live turn has written a checkpoint (the flag is off), so run
  one with `[checkpoints] enabled = true` and count the columns before building resume on
  it. And an orphaned effect's *arguments* are not in the ledger — `result_ref` is NULL
  until a terminal state — so 4c's binding requirement that a `web_fetch` prompt show its
  URL must read the `tool_requested` event, not the ledger.
- **4b → 4c, two decisions 4b would not make alone:** whether `never_dispatched` is phrased
  as `blocked` rather than `uncertain` (the pass file defines blocked as "the work did not
  happen", and 4b's evidence string can say that — but the ledger row is written *after*
  the event, which is the crash window that made orphans a fold rather than a scan); and
  whether a resumed turn's message list carries a tool message for the orphaned call.
  Resume closes the effect but deliberately writes no `tool_failed`, so a fold still shows
  a `tool_started` with no partner.
- **4b → Pass 5, the gap it refused to paper over:** there is no `model_messages()`. The
  journal holds 200-char previews and the archive has no mid-turn assistant prose, so a
  function returning previews as message content would be a laundering channel. Tool-call
  arguments are exact; the rest is a spine. Pass 5 owns the gap.
- **RESOLVED by 4c (4b's two handed-up decisions):** `never_dispatched` stays `uncertain`,
  not `blocked` — `intend()` resets the shared ledger row to `intended` on a second
  attempt, so `blocked` (which permits retry) would authorise re-sending a call that
  already went out. And a resumed message list does carry one synthetic, runtime-labelled
  tool message per interrupted call, so no `tool_started` is left without a partner.
- **4c → Dylan, at the pass boundary:** the user-facing wording is proposed and unreviewed,
  verbatim in `pass-04-outcome.md` with seven rejected variants. It is the one thing in
  Pass 4 the standing policy deferred rather than decided.
- **4c → Pass 5:** `notice()` and `closing_messages()` have no runtime consumer yet
  (reachable only via `agent journal resume --notice`), and which path was taken for an
  uncertain call is journaled nowhere.

## Pass 4 complete

4a–4d all committed, tests 751 green, ruff clean, no Must not crossed in any session. The
pass-level exit criteria are met and the evidence is in `pass-04-outcome.md` under
"Pass 4 — closing": kill and resume at all five boundary types, an orphaned `unsafe_write`
surfacing as `uncertain` and never silently retried, and a fork that works with an honest
disclosure. Checked against reality rather than only the suite — nine mutations all caught,
a copy of the live journal plans a fork on all six of its runs, and a scratch run with six
real ledgered effects was forked twice end to end through the CLI.

**Owed to Dylan before Pass 5 — one thing, and it is the thing the standing policy
deferred rather than decided:** every user-facing string in 4c and 4d is proposed and
unreviewed. All of it is in `src/agentd/agent/observations.py`; 4c's wording is in the
outcome record with seven rejected variants, and 4d's disclosure is there verbatim as
rendered from a real forked run. This is the honesty surface of the whole pass — what the
runtime says when it does not know whether something happened, and what it says when a
conversation is rewound past work that was really done.

## Pass 4/5 boundary — put to Dylan 2026-09-20, two settled and one open

**Ruled: mid-turn assistant prose gets archived, so 5c's lossless branch really is
lossless.** (4b open question 1, which the Pass 4 closing named as the one decision Pass 5
could not avoid.) Rejected: accepting the loss and annotating it, and treating any run with
non-empty mid-turn prose as handoff-only. **The cost he accepted is binding on 5c and must
not be quietly papered over:** archiving fixes nothing for runs already in the journal, so
every run recorded before this lands still falls through to the lossy path. 5c states which
branch a given run took and why; it does not present the fallback as the lossless one.

**Ruled: `result_lost` is renamed before anything consumes it.** (4c open question 2.)
Rejected: keeping the name, and collapsing it back into the two-word vocabulary — 4c's
argument against the collapse stands (`uncertain` asks a question with a known answer,
`blocked` invites the duplicate the pass exists to prevent). **The word he chose is
`unreported`** — an adjective about the call's standing, so all three statuses are the same
part of speech, and it reuses the journal's own phrase (`may_have_run` already reads "never
reported back"). The collision that costs: an uncertain call is unreported too. What
separates them is not in the status word but in the evidence field, which is `committed` for
this one — so anything that renders the status without the evidence beside it is wrong, and
5b must check that nothing does. Rejected: `completed` (collides with the effect-ledger word
for a settled effect and with `STATEMENTS[COMMITTED]`, and reads as "nothing to see here" in
a list of things that went wrong) and `unwitnessed` (draws the line in the right place, too
literary for a block a model reads). The rename is `observations.py`, `resume.py` and the
tests, and it happens in 5b before the first consumer. The model-facing heading changes with
it — "N happened, with the result lost rather than the call" becomes "N unreported - the call
is recorded as having happened and its result did not survive" — and the user-facing
`notify_user` block is unchanged, because it never used the status word.

**Ruled: 4d's disclosure stands, with four edits — applied.** The opening sentence drops
its two mechanism clauses; both list headings become "I made:"; a failed call is stated and
never interpreted; the empty case attributes its claim to the journal. The closing sentence
is unchanged at his instruction. Full reasoning, the rendered output and the four edits
verbatim are in `pass-04-outcome.md` under "Pass 4/5 boundary — the wording review". 751
tests green, ruff clean, no behaviour change.

**His reasoning on edit 3 is the part that outlives the edit:** a failure response does not
prove the effect did not land — a fetch can fail after the server acted, which is why
`web_fetch` is `unsafe_write`. Appended to the Pass 10 per-call-risk collection in
`pass-03-outcome.md` beside `memory_search` and `web_fetch`. **3b's ledger rule and 4c's
`effect_failed → blocked` mapping are deliberately NOT changed** — this is a collection
entry to decide at Pass 10 against real traces, not a ruling to apply now. A later session
that "fixes" either of them has misread this.

**Open, and unresolved by edit 3: a failed call is now the only call in a disclosure that is
not named.** The old line claimed it changed nothing outside, so withholding its arguments
cost nothing; edit 3 withdraws that claim and the line still withholds them. Put to him;
nothing in Pass 5 depends on the answer.

**Open: 4c's wording review.** He has not read it in full and explicitly held it. **Nothing
in Pass 5 may give `notice()`, `prompt()` or `closing_messages()` a consumer until he has** —
which is 5b and 5c, not 5a. 5a consumes none of it.

**5a is clear to dispatch.** It is runtime-side token accounting and a threshold; it touches
no wording, no status word and no rehydration path.


## 5a → 5b and 5c, and one thing for Dylan

- **The two hazards 5a was handed both held, and both are now settled facts rather than
  warnings.** Provider token accounting is dead — 12 of 5,087 `actions` rows have non-zero
  tokens, all from 2026-09-17, and `usage_reported` is false on all 5 live `agent_finished`
  rows. Everything 5a records is `ids.estimate_tokens` (len/3.2) and is labelled
  `context_basis = "estimate"`. **No later pass may put these numbers in a table as measured
  values.** And the ceiling is `agent.history_tokens` (24000): `llm.max_context_tokens`
  (262144) is used nowhere in `src`, and a crossing against it would need a 254k prompt
  against a ~56k structural maximum.
- **5b inherits an unresolved question, not a finished one: `context_crossed` answers "is
  this prompt near the ceiling", not "is this conversation long".** The numerator is the
  whole assembled prompt; the denominator is the budget on what carries *forward*. So
  `max_steps` (12) × `tool_result_max_chars` (8000) ≈ 30k estimated tokens of tool output
  can cross a 24k ceiling with a two-message conversation. It has never happened here, and
  it is reachable by construction. 5a recorded the alternative rather than deciding it.
- **The crossing has never fired outside the suite.** 0 of 33 recorded turns come within
  17,749 tokens of the threshold; the longest real conversation on this machine is 1,993
  tokens, 8% of the ceiling. The forced-long test builds its own history. **5b must not read
  "it fired in a test" as "it fires when it should."**
- **`estimate_tokens` has never been calibrated against this model's tokenizer.** len/3.2 is
  a guess. Every number in Pass 5 inherits its error.
- **For Dylan, a precedent rather than a blocker:** 5a added five *required* fields to the
  existing `agent_finished` type, so the 5 pre-5a rows in the live journal no longer pass
  `validate_payload`. Checked rather than assumed: `validate_payload` is called from exactly
  one place, `journal/runtime.py:123`, on the write path, so nothing re-validates on read and
  all 9 live runs still fold. The question is whether a later pass may do this again — Pass 6
  (`worker_results`) and Pass 7 (`memory_watermark`) both fill slots on existing records and
  will face the same choice. 4a's answer twice was required-and-nullable, which does not help
  when the key is absent entirely.
- **5b is held** pending the 4c wording review, per the fence recorded above.

## Pass 4/5 boundary — the 4c review, and two requirements that bind later passes

Dylan reviewed 4c's recovery wording on 2026-09-21: four changes and one confirmation. The
two below are **requirements, not suggestions**, in his own filing language. They are
recorded here because neither is code anybody has written yet, and both are about a failure
that only becomes reachable once Pass 5 gives this wording a consumer.

**REQUIREMENT on whichever Pass 5 session first consumes `notice()` — a runtime guard, not
a prompt.**

> A runtime guard that refuses an `unsafe_write` matching an unresolved uncertain call's
> tool and `canonical_args` in the same run, unless I've said to run it again.

His reasoning, which is the part to carry: **the prompt is currently the only thing standing
between an uncertain call and a duplicate.** A re-issued call gets a new idempotency key —
`hash(run_id, step_id, tool_name, canonical_args)` includes `step_id`, so the same logical
call made again in a later step does not collide with the row that is already open (3b #5,
narrowed by 4b and widened again by 4d across a fork). And the model being asked to obey
"you must not re-run an uncertain call" is a local 27B. A sentence in a prompt is not an
enforcement mechanism; this is the enforcement mechanism.

Note the interaction with 4b, which is why this belongs to the session that consumes
`notice()` rather than to the ledger: an `uncertain` effect is *terminal* to the fold, so
"unresolved" cannot mean "still open in the ledger". The guard needs the set of uncertain
observations the resumed turn was handed, and it must be released per call once the user
says to run it again.

**REQUIREMENT on Pass 5 (handoff generation) and Pass 7 (promotion).**

> Messages with `synthetic=True` are never summarized or promoted as fact.

`ClosingMessage.synthetic` exists precisely so this is checkable rather than a matter of
convention, and the text carries `RUNTIME_PREFIX` as a second, independent signal. The
failure it guards is the one 4b refused to open when it declined to hand the journal's
200-character previews to a model as message bodies: **text the runtime wrote about a call,
folded into a summary or a memory candidate, becomes a claim the user never made and no tool
ever returned.** Pass 5's handoff generator reads a message list; Pass 7's promotion reads
one too. Both must exclude these, and both must say in their outcome record how they did it.


## 5b → 5c, and three things for Dylan

**The exit criterion is two-thirds met, and that is stated rather than rounded up.** The pass
file asks for continuity across the boundary on **at least three** Pass 1 tasks including the
two near-context-limit ones. 5b did the two near-limit tasks against the real local model —
B23 crossed at the *real* configured threshold with no forcing at all, the first crossing
outside the suite, and B22 was forced by lowering the ceiling to 1000. The third task was not
run. That is an open item on the pass, not on 5c.

**RESOLVED by 5b — 5a's handed-forward question.** There are two readings now, answering two
questions. `budget.read_messages` sizes the whole assembled prompt and still marks 5a's
`handoff` checkpoint; `budget.carried` sizes only `user`/`assistant` text — what
`history_messages` actually replays — and that is the reading that decides a handoff is
generated. Measured on the live runs: B22's prompt reached 13,484 estimated tokens while what
it carried was 910. So "prompt full" and "conversation long" are now separate facts and the
journal distinguishes them.

**Requirement A is enforced and the enforcement is tested rather than demonstrated.**
`handoff.source()` refuses three classes and counts each into the stored object: the
`synthetic` flag (not the prefix string), every `system` message — `FINAL_NUDGE` and
`STUCK_NUDGE` are unflagged runtime text — and any residual `RUNTIME_PREFIX`, which lands in
`source.unflagged_runtime_text`. Both live runs had nothing synthetic to exclude, so the
counters were 0 and the guard was never exercised on real material. Worth re-reading the first
time a real resume feeds a handoff.

**Requirement B falls to 5c**, and the reason is recorded rather than assumed: 5b consumes
neither `notice()` nor `closing_messages()` — a threshold handoff contains no interrupted call
— and its only import from `observations.py` is `RUNTIME_PREFIX`. **5c consumes `notice()`, so
5c builds the guard.**

**THE FINDING OF THE PASS, and it is a quality failure rather than a bug.** After a handoff
this runtime **does not reliably refuse to answer from material the handoff dropped.** Same
code, same task, B23 turn 4, three runs, three behaviours: a confident wrong answer with no
hedge; a correct refusal naming what it no longer had; and a confident wrong answer again.
Turn 5 — the row the baseline actually grades — passes in both runs. Adding `DROPPED_LINE`
moved the behaviour and did not fix it. Three directions are named in the record and **none of
them is carrying more of the conversation forward, which is the pass file's second Must not**;
the first live run of 5b is what it looks like when the carry window quietly grows. This is
5c's and Pass 7's, and it is the thing to grade the pass on.

**Found incidentally, pre-existing, and not fixed: every turn's prompt contains the user's
current message twice.** `run_turn` archives the user message (`loop.py:318`) *before* reading
the history window back (`:376`), so `history_messages` returns it and `build_messages`
appends it again (`context.py:111`). Verified independently at the boundary. It has always
done this, it inflates every context reading 5a and 5b take by the size of the current
message, and fixing it is a behaviour change to every turn that would move the Pass 1
baseline. Same shape as the `loop.py:213` budget-exhaustion crash carried since Pass 2:
recorded, not fixed, and **whoever fixes it must re-measure the threshold afterwards.**


## The 5b boundary — Dylan's three rulings, 2026-09-21

### 1. Confabulation: the manifest and the lookup, as one mechanism

Both directions, not one. **His reading of the pass's second Must not, which is what makes
the lookup permissible, recorded verbatim at his instruction** (also in
`pass-05-outcome.md`):

> it forbids the runtime restoring the old context wholesale as a fallback. It does not
> forbid the successor requesting a specific named item. The line is who chooses what comes
> back and how much.

5c ships the manifest — each dropped item with a kind, a one-line description and a **ref**,
not just a kind, so a lookup can be targeted; and a successor context that says it must look
one up or say it does not have it, never answer from memory of it. **5d is new and is in the
pass file**: a `read`-class tool taking one ref and returning a bounded excerpt, no free-text
query, no "everything", model-invoked only, journaled, reading the journal/archive.

**The guard he attached, binding:** record lookups per successor turn. If successors
routinely fetch every manifest item as their first action, that is the wholesale restore by
another route — **flag it for Pass 10 rather than tuning it**.

**The eval he specified:** ask a successor about dropped material; a lookup or "I don't have
that" passes, a confident answer fails. Run it before, **after 5c alone**, and after 5d, so
the manifest's share of the gain is measured rather than assumed.

### 2. The duplicated user message: fixed now, as its own commit

Before the 5a threshold is treated as final. His five steps are in the dispatch brief; the
ones that outlive this session:

- **`baseline.md` and the `eval-baseline` tag stay v1. They are not overwritten.** The fix
  commit is tagged `eval-baseline-v2` and `baseline-v2.md` is a new file.
- **The attended rows — B11, B12, B13, B21, B23 — are Dylan's to run at the next sitting,
  and they must be done before 8a**, because Pass 8 compares the coder family against them.
- **From here, Passes 8, 9 and 10 compare against v2.** Recorded here so a later session
  does not pick up v1 by default.
- The v1→v2 delta per row, and tokens saved per turn, is itself a finding: what the
  duplication was costing.
- The 5a threshold is recalibrated against v2 readings, with both values recorded and why it
  moved.

**Answered at the boundary:** this is **not** the same bug as the carried "token accounting
broken upstream" item. That is the SIR router dropping the streaming `usage` chunk, so
provider counts arrive as zero. This is prompt assembly sending the same text twice. Different
layers, neither causes the other; they interact only in that one makes counts unavailable and
the other makes estimates too large.

### 3. The third task: run it, and the 5b exit is **not met**

Not "met at two-thirds". **Recorded as not met**, because the criterion asks for preserved
continuity and the confabulation finding says the two tasks that ran did not fully preserve
it. **The pass exit is judged after 5d** — re-run all three tasks, same grading, compare.

The third task is chosen rather than convenient: an ordinary suite task, **not B22 or B23**,
not one anyone tuned the handoff against, preferring one where substantial material enters
mid-run, with the handoff forced after it so there is something real to drop. It answers one
question in one line: **is the unreliability general, or specific to the near-limit tasks?**
And it is the "before" point of the confabulation eval.


## The re-baseline, and the thing it invalidates

`docs/records/baseline-v2.md` is written. `baseline.md` and the `eval-baseline` tag are
untouched. `eval-baseline-v2` = `bb31388`. All 22 automated rows re-run live against
`Qwen/Qwen3.8-27B-FP8` — the same model as v1, verified by live completion rather than by
`/v1/models`, which lists both residents.

**The v1→v2 delta does not measure the duplicate-message fix, and no later pass may cite it
as though it does.** The two tags are **23 commits apart** — all of passes 02, 03, 04 and 05;
`git diff --shortstat` reports 73 files, +20,007/−341. The orchestrator's brief framed the
re-run as measuring the fix; that framing was wrong and the session running it said so rather
than producing the number that had been asked for. **A clean measurement of that fix alone is
no longer recoverable**, because the baseline had not been re-run since Pass 1.

What *is* attributable to the fix is the figure measured directly from archived messages
rather than differenced between runs: **mean 33.7 estimated tokens saved per suite prompt**
(median 33, max 75), 52.3 over all 140 archived rows. `ids.estimate_tokens`, an estimate,
never a count.

**v2's real value is undiminished for the purpose the ledger already recorded**: it is the
reference point Passes 8, 9 and 10 compare against. It is a fresh measurement of the current
tree, which is what those passes need; it is only the *difference* from v1 that cannot carry
an explanation.

**Grades moved on two of 22.** B02 fail → pass (v1's `merged.append()` bug is gone, verified
against the rubric's own input) and B05 pass → partial (correct deadlines, no freshness
stated, which the rubric requires). Both are single samples from a 0.7-temperature model on
rows whose mechanism did not change, and B05 carries an explicit grading note because v1's
answer text cannot be re-read to rule out a grader-caused delta. Aggregate: pass 5→5,
partial 3→4, fail 12→11, inconclusive 2→2. Turns hitting 12/12 rose 3→4; **zero-answer turns
rose 3→7**.

**The threshold did not move, and the reasoning is recorded either way**: `[handoff]
threshold_tokens` stays 8000. The inflation removed is ~34 estimated tokens, 0.42% of the
threshold. Meanwhile the largest observed prompt rose 6,251 → 10,979 and `context_crossed`
was false on all 31 turns. **It is verified unreachable on the `ask` path and unverified on
the multi-turn path it exists for — which is B23, one of Dylan's attended rows.**

**New in v2 and not present in v1: three rows died on 600-second router streaming timeouts**
(B06 attempt 1, B12, B22) with zero answer characters. This contaminates latency for the
whole run and changes B12's and B22's failure modes. It is an environment finding, not a code
one, and nobody has looked at it.

**Running the suite writes eval text into the live memory store.** The consolidator accepted
B11's prompt as a preference. The suite was always going to do this — v1 did too — but it is
recorded here because it is the user's real memory being written by a test.

**Reproduced unchanged at `bb31388`:** B09's unaudited duplicate loop (no `actions` row, no
`due_at`), B20's queued write reported as saved, delegation dead (three sub-agent turns, all
under 110 ms, `llm_ms: 0`), and the `FINAL_NUDGE` mid-list HTTP 400.

**Method deviations from v1, four:** the run was split into two windows about 12 hours apart
(tooling stalls, with model, daemon, feeds and commit re-verified across the gap, and B04–B08
all inside window one); B06's fault window was 43 minutes rather than ≈4; the tree was on
`main` at `bb31388` rather than detached, because this repo is edited in parallel; and the
daemon was restarted mid-history where v1's ran throughout.

**Still owed before 8a, in both baselines:** B11, B12, B13 and B21 under `chat`, and B23.

---

## Boundary — the 600s timeout diagnosed, the eval cleaned out of memory, and a closing sentence that lied

Three things Dylan asked for directly, none of them a pass session. Recorded here because
the first closes an open finding in `baseline-v2.md`, the second changes his live data, and
the third changes recovery wording Pass 4 ruled on.

**1. The 600s timeout is the `FINAL_NUDGE` 400, arriving at somebody else's turn.** Full
chain in `baseline-v2.md` finding v2-1, which is amended from "not diagnosed here" to
diagnosed and reproduced. In short: the nudge is appended as a `system` message at the end
of the list, Qwen3's template rejects that with a 400, `sir` treats *any* non-200 from the
backend as a crash, and its crash handler cancels every other in-flight request without
putting a terminator on their event queues — which its own docstring warns is a hang. The
clients had already been sent a synthesised opening frame, so they sit on a live stream
that never produces another byte until `llm.timeout_s`. The telemetry matches to the
second, and it reproduces on demand.

So **v2-1 and v2-2 are one finding, not two**, and `sir` is the amplifier rather than the
cause. Two fixes, either sufficient, neither done: make `FINAL_NUDGE` not a system message
(ours, one line, removes ~10 400s a day), or make `sir` not treat a caller's malformed
request as a backend crash (not this repo). **Recommended: do ours before the 5c eval**,
which runs against the real model and will otherwise inherit the same failure.

One thing the record had backwards and now does not: the 0.5s hand-sent probe was not
evidence against queueing, because the backend re-adopts in ~3ms on the next request. And
one trap worth writing down: `ps` shows vLLM with `--port 8000`, which is its *in-container*
port, published to the host on 8001. `sir` holds 8000. `/v1/models` settles it — `owned_by`
is `"sir"` there and `"vllm"` on 8001.

**Unasked, from the same probes: the dead token accounting is one missing frame.** `sir`'s
SSE renderer emits no usage chunk at all, while its non-streaming path does carry usage.
That is the whole of the "`usage_reported` false on every streamed turn" finding carried
since Pass 1. It does not make `budget.py`'s estimates wrong, but it means `BASIS_PROVIDER`
has a reachable implementation whenever somebody wants it.

**2. The eval is out of the memory store.** 47 eval sessions identified by matching every
archived `user_message` against the frozen prompts; 4 facts retracted, 4 open loops closed,
1 goal dropped, both markdown files regenerated, `agent backup` taken first. The table and
the reasoning are in `baseline-v2.md` v2-3. The one that mattered: B20's prompt had been
promoted into `profile/preferences.md` as a stated preference of Dylan's. `preferences.md`
now reads `_Nothing established yet._`, which is true — no preference in that store was
ever stated outside an eval.

`candidate_memories` was deliberately left as the proposal log. **8d will re-contaminate
the store**, and the §6 reset in `evals/baseline-tasks.md` does not cover any of this —
it resets the connector, the loop, the working tree and the pending fact, and says nothing
about what the consolidator promotes. That gap is worth closing before 8d, not after.

Found doing it: `agent goals drop` prints `dropped` whether or not it matched anything.

**3. `CLOSING_TEXT` was keyed by status, and told a failed call it had been interrupted.**
Commit `f0be3ee`. This is the wording question left open twice at the Pass 4/5 boundary,
now answered the way the rest of the module already works: keyed by evidence, total over
the vocabulary. A failed call now says it ran, reported a failure, and was *not*
interrupted — while still refusing the inference Dylan struck, that a failure proves the
effect did not land.

Two further defects fixed in passing, both found by rendering the output rather than by the
suite: the re-run directive is now derived from the observation's own `paths`, so it can no
longer contradict `paths_for` (an uncertain `idempotent_write` carries `retry` and was being
told "Do not re-run it yourself"); and the `never_dispatched` text claimed an earlier
attempt may have run when there had been no earlier attempt. Three tests added, each
mutation-checked against its own defect. 811 passing, ruff clean.

**Pass 5 state is unchanged by all of this.** 5c and 5d are still ahead, and the third suite
task Dylan ruled "run it now" is still not run — it is the next thing.

---

## Pass 5 complete — 5c and 5d shipped, and the exit is **not** met

5a–5d are all committed and 861 tests are green, ruff clean. The pass exit is recorded as
**not met**, following Dylan's own rule from the 5b boundary that a partially met criterion
is never rounded up, and **exactly one thing is missing**.

**What shipped.** The nudge fix first (`3015aa0`), because the eval runs against the real
model and would otherwise have inherited the `FINAL_NUDGE` 400 that hangs every concurrent
caller. Then 5c (`6113734`): the manifest, cold resume in `agent/rehydrate.py`, requirement
B's rerun guard, `assistant_step` archiving, `handoff_schema` 2 with a real v1 upgrade, and
`agent journal continue`. Then 5d (`ae2d6ea`): `handoff_lookup`. Twenty-seven mutations, one
survivor, and that survivor named a real bug that had already shipped.

**Four defects were found by reading rather than by the suite**, and the method matters more
than the count: the watermark cut the manifest above the turn's own tool output; the
`assistant_step` rows were archived and then unreachable (found by counting a real generated
handoff's five manifest items against its seven archive rows); a detached run's resume reason
named a budget nothing measured; and `journal continue --apply` would have replayed the whole
conversation when handoff generation failed, which is the pass file's second *Must not*
arrived at by omission.

**The confabulation eval ran at all three points Dylan specified.** Rate: **0 of 3 at every
point** - nothing invented a quotation anywhere. The manifest changed the *content* of the
refusals rather than the rate, and the lookup converted one refusal into a correct answer
with exactly one fetch. **The substantive result is B23 turn 4**, the row that produced 5b's
finding: 5b got a confident wrong answer, then a refusal, then a confident wrong answer from
the same code; with a manifest in force it refuses correctly and distinguishes what the
summary contains from what it is about.

**The one thing missing is B22**, one of the two near-limit tasks the 5b exit names. It has
now failed to produce a usable measurement three times running, each time for a different
reason and **never the handoff machinery**: v1 answered from its prompt in one step, v2 died
on a 661 s router timeout, and this pass's run spent 301 s calling six tool names that do not
exist plus one literally named `tool`, while `fs_read`/`fs_search`/`fs_list` sat unused. The
handoff fired on that run and produced an 11-item manifest in 57.6 s. **Re-running it a
fourth time unchanged is not the answer**; the option to put to Dylan is whether B22 may name
its tool, at the cost of no longer measuring tool selection.

## Carried forward from Pass 5

- **A new and live second source of the v2-1 timeout chain, and it is not ours to fix.** The
  `FINAL_NUDGE` 400 is fixed. The model now supplies its own: malformed tool-call JSON,
  which vLLM answers with `HTTP 400 Unterminated string`, which `sir` treats as a backend
  crash and which therefore cancels every other in-flight request. The router's `loads`
  counter climbed 45 → 51 over eight consecutive eval attempts. **Any future suite run on
  this box should expect it**, and `baseline-v2.md` v2-1's chain is only half closed.
- **The local 27B calls tool names from pretraining instead of the schemas it is sent** -
  `Read`, `Bash`, `Glob`, `Grep`, `read_file`, `list_files`, and once a tool named `tool`.
  Roughly one repo-comprehension run in three is usable because of it. This is the single
  biggest obstacle to running any tool-driven eval here and it will bite 8a.
- **→ Pass 7, the first thing to fix with this material:** a successor disclaims material it
  is still holding. `DROPPED_LINE` says the replaced messages are gone and the model
  generalises it to the carried window too. Saying what is *carried* as well as what is
  dropped is the cheap fix; a longer warning is not, because quantity of warning has now
  moved behaviour unpredictably in both directions twice.
- **→ Pass 7/10, the rubric's blind spot:** *a lookup or "I don't have that" passes* cannot
  see the failure above, because the false disclaimer is phrased as a pass. Any re-run needs
  a second axis - was what the successor said about its own context true - and the harness
  already records everything that check needs.
- **→ Pass 10, Dylan's guard on the lookup, unchanged and now answerable:** lookups per
  successor turn are journaled as ordinary `tool_requested` rows; the query is in
  `pass-05-outcome.md`. Observed so far: one lookup per successor, on the one item where
  fetching was the only route. Also for that collection: the successor twice *asked
  permission* to fetch rather than fetching.
- **→ whoever runs B23 again:** whether it crosses at all depends on how verbose the model
  is that day. 5b's run reached `carried = 17,542`; this one peaked at 15,448 on identical
  input against a crossing point of 16,000. Check `context_crossed` before reading a run as
  evidence.
- **Still owed before 8a, unchanged by this pass:** B11, B12, B13 and B21 under `chat`, and
  B23. Passes 8, 9 and 10 compare against `baseline-v2`, not v1.
- **`[checkpoints] enabled` is still false by default**, so `Session.resume` has nothing to
  read back and a handoff survives a process only when it is switched on. 5b's open question
  5, unchanged; turning it on is a Pass 4 decision.

---

## Pass 6 complete in code — and one exit criterion is recorded **not met**

6a–6c are committed (`b12599f`, `abba052`, `84fc671`), 920 tests green, ruff clean, no
*Must not* crossed in any session. The **pass-level** exit criteria are met: one delegation
interface, a result schema validated on return, and resume reusing completed worker results
at 3/3.

**The 6b session criterion is not met, and it is not rounded up.** The pass file asks that
*every durable role returns valid results across the Pass 1 task suite*. No session in this
pass ran the suite, or any live model at all — every result is unit-tested and
mutation-checked against scripted providers. `baseline-v2.md` records delegation as dead on
this machine (three sub-agent turns, all under 110 ms, `llm_ms: 0`), and **nothing in Pass 6
checked whether that is still true**, so the schema has never decoded a report a real 27B
wrote. That is the first thing to do with this material, and it is cheap: the digest makes a
repeated delegation observable for the first time.

**The vocabulary grew 17 → 19** (`worker_result_cached`, `worker_result_reused`), the first
addition since Pass 2 fixed it. Both are named in the drift guard with the pass that added
them, so it is a decision rather than drift — but 3b treated the 17 as a line it would not
cross (it declined to add `effect_started` and accepted a ledger-only transition instead),
and this is the precedent moving.

**The cache is in the journal, not the checkpoint.** 6c found the live journal holds **zero
checkpoint rows across all 50 runs**, so the `worker_results[]` slot Pass 4 left inert would
have stayed inert. `checkpoint.worker_results[]` is filled from the same fold, so the
checkpoint still agrees with the journal — the governing invariant holds — but the working
path does not depend on a feature that has never been switched on.

**Session 6a wrote seven events into the live journal** (`run-tool`: `worker_created`,
`agent_started`, three `message_appended`, `agent_finished`, `worker_finished`; no effect
rows, no checkpoints, nothing in Postgres — verified directly, not taken on report).
`run_subagent` resolves config at call time via `cfg or get_config()` and the `delegate` tool
is exactly the caller that has none. Left in place, because the journal is append-only and a
synthetic run sitting in it is better than a deletion from the source of truth.
`tests/conftest.py` now monkeypatches `agentd.agent.subagents`; the underlying shape is
unchanged and is the same shape as every other name on that list.

**`uv run pytest` had not collected since `ae2d6ea`.** `tests/test_handoff_lookup.py` (5d)
imports `tests.test_handoff_manifest`, which resolves under `python -m pytest` and not under
the command `CLAUDE.md` names as the gate. Fixed in `817b7bf` as its own commit before any
Pass 6 work was dispatched. **The gate command and the green suite were two different things
for the whole of 5d**, which is worth knowing when reading 5d's test counts.

## Carried forward from Pass 6

- **For Dylan, the one thing in this pass that is a privacy question rather than a design
  one:** a worker is shown its caller's conversation while `TaskSpec.brief` tells it it
  cannot see it. Probed rather than inferred — `run_subagent` builds
  `Session(id=parent_session_id, ...)`, so `history_messages` returns the user's earlier
  messages, and a scratch probe found a planted secret in the worker's first prompt. 6b closed
  the leak in the *other* direction (worker prose rebuilt into orchestrator history, via
  `repo_archive.recent_messages`, which no delegation-path check would have caught). This one
  is the private-data interlock's business as much as delegation's, and fixing it means giving
  a worker its own session id — which changes what `events_for_session` groups and what
  consolidation reads. Nobody should fix it inside a session that is doing something else.
- **→ Pass 7, and it is now answered twice the same way:** 5a's precedent question — may a
  pass add a *required* field to an existing event type? 6a added `task_digest` to
  `worker_created` and 6c added two event types; all 50 live runs still fold, because
  `validate_payload` is called on the write path only. Pass 7 faces it a third time with
  `memory_watermark`. Three sessions have now taken the same option without anybody ruling on
  it.
- **→ Pass 8, measure before normalizing:** the cache is keyed on prose the local 27B
  composes, so two delegations that mean the same thing miss unless it re-types them
  identically. The measurement is `worker_result_reused` against `worker_created` on real
  runs. If it is near zero while the same work is visibly repeated, the fix is refs (urls,
  paths, fact ids), not fuzzier matching. Historical live data: 6 delegations, 6 distinct
  task strings — nothing has ever been repeated here, so there is no rate to read yet.
- **→ Pass 7/8, deliberately not crossed:** a crash *inside* a worker leaves `worker_created`
  with no result, and the run's own record of what was asked is a 200-character preview plus a
  digest. The full brief exists in Postgres (`subagent_message`, `actions.input.task_spec`),
  but reading it crosses a store boundary the resume path does not cross.
- **Not fixed, and left deliberately:** `checkpoints.WorkerRef.role` is `"subagent"` for every
  worker ever checkpointed — it reads the LLM role, while the durable role is
  `payload["name"]`. 6c does not depend on it (only a *finished* worker's result is restored;
  an open one is re-delegated from the caller's own `TaskSpec`), and reading a key pre-6a rows
  do not have would crash a fold.
- **Nothing expires, and that is the definition of a run-scoped cache** — a run lasting hours
  serves a result earned in its first minute. It is the staleness question the *Must not*
  deferred, one scope smaller.
- **`FINAL_INSTRUCTION` and the `delegate` tool description both changed in 6b**, so
  `baseline-v2` is not comparable across this pass for anything that measures delegation.
  Passes 8, 9 and 10 still compare against v2 for everything else.
- **Unchanged by this pass:** effect events carry no `worker_id` (3b #4), so a reconciliation
  cannot say whether the orchestrator or a worker made a call; `open_workers[]` has never been
  non-empty; `[checkpoints] enabled` is still false; token accounting is still broken upstream
  (`sir`'s SSE renderer sends no usage frame).
- **Still owed before 8a, unchanged:** B11, B12, B13 and B21 under `chat`, and B23. And the
  Pass 5 exit is still **not met** — B22 is the missing task, with the open question of
  whether it may name its tool at the cost of no longer measuring tool selection.

---

## Pass 7 complete — the exit criteria are met, and nothing live has ever exercised them

7a–7c are committed, 966 tests green, ruff clean, no *Must not* crossed in any session.
The three pass-level exit criteria are met: three logical memory types, worker working
memory that is isolated, and crash-safe promotion verified by fault injection in both
orderings rather than by inspection.

**Stated rather than folded in, under the rule that a partial result is never rounded up:**
the criteria are met *in code and under fault injection*. The live journal holds **0
`working_memory%` events and 0 `promotion%` events** across all 53 runs. No live model has
ever classified a promotion here, and the working bucket only fills when the model calls
the tool. This is the same gap Pass 6 recorded against its own schema — the machinery is
mutation-checked against scripted providers, not against a 27B that has to decide to call
something.

**The storage stayed shared, as the third *Must not* requires.** Three buckets are a logical
distinction (`memory/scopes.py`), the working bucket is folded out of the journal like 6c's
result cache one scope further out, and no store was migrated for bucket separation.

**Two mutation findings, and both name a bug rather than a missing test.** 7b: the
two-worker isolation test passed with the scope filter deleted, until each worker used a
distinct note key. 7c: deleting the `worker_id` guard in `_promote` broke **zero** tests — a
worker's turn would have classified and written its *caller's* working scope, mid-turn,
which is two of this pass's three *Must not* lines at once. Both now have a test that dies
when the guard goes.

**The whole test suite has been calling the live model endpoint.** 7c found `classify`
reaching for the process-global `get_provider()`, fixed it by threading the provider from
`AgentLoop.provider`, and added `agentd.memory.promotion` to the `conftest.py` monkeypatch
list. Worth knowing when reading any earlier pass's timings.

## Owed to Dylan at the Pass 7 boundary

1. **7a's finding itself.** It is the pass's `**hard stop — human runs this**` session, run
   autonomously under the standing policy. The marker stands and the reading is still his.

2. **The migration, and whether it crosses the third *Must not*.** `migrations/0010_promotion.sql`
   adds one partial unique index on `candidate_memories.structured->>'promotion_key'`. 7c's
   argument, which reads as sound and is his to confirm: the *Must not* forbids migrating the
   physical store when a **logical** distinction achieves the same thing, and no logical
   distinction can deduplicate a write across a dead process — if postgres commits and the
   process dies before the journal hears about it, the resume writes the row again, and that
   duplicate is exactly the silent retrieval decay the pass exists to prevent. Same shape and
   the same partial-index trick as `raw_events_connector_dedup_idx` in `0007_connectors.sql`.
   The 116 existing candidates carry no promotion key and are unconstrained.

3. **The required-field precedent, now at its third instance and still unruled.** 5a added
   required fields to `agent_finished`, 6a added `task_digest` to `worker_created`, 7c adds
   `memory_watermark`. It works only because `validate_payload` runs on the write path alone
   (one caller, `journal/runtime.py:123`) — 7a re-verified that and also found the live journal
   **already** holds rows violating today's spec: 4/5 `worker_created` without `task_digest`,
   5/52 `agent_finished` without the 5a context fields, and 5/5 `worker_finished` carrying
   `status="ok"`, outside the current enum. Three sessions have now taken the same option
   without anybody ruling on it. Nothing re-validates on read, by design.

4. **`agent db migrate` has not been run; the live DB is at 0009.** Deliberately left to him
   rather than run autonomously, because it changes his real database. Until it is run, a
   semantic promotion on the live system **fails and stays pending** — recoverable via
   `agent journal resume --apply`, not lost. This is the one action owed before the new path
   works live.

## Carried forward from Pass 7

- **Nothing sweeps pending promotions across runs.** `complete_pending` is called only by the
  next boundary of the *same* run and by the resume CLI. A run that dies at its last boundary
  leaves a promotion pending until someone resumes that specific run.
- **`episodes` is empty and has always been** — 0 rows after 111 ok `post_session` runs,
  because `ExtractedEpisode` has no required fields and the stats dict never counts the
  omission. So the three buckets are one live, one dead and one new. 7a found it, 7b was told
  not to fix it, and it is a consolidation behaviour change that belongs to whoever owns that
  path. **The episodic third of this pass is untested against real data because there is no
  real data.**
- **The `synthetic=True` rule still holds structurally rather than by a check.** The promotion
  path reads `raw_events`, which has no `synthetic` column, so nothing can promote a runtime
  message today — and nothing stops a future session archiving a message list and breaking it
  silently. The guard Dylan asked for is an invariant of the current data flow, not an
  assertion.
- **`memory_promote` is an effect, not a registered tool**, so it is deliberately absent from
  `effect-classification.md`, whose test asserts the table equals the registry.
- **Vocabulary 21 → 24** across this pass (`working_memory_noted`, `working_memory_discarded`,
  `promotion_classified`, `promotion_committed`, `promotion_batch` — 19 → 24 counting 7b's
  two). Pass 2 fixed it at 17 and 3b declined to add an 18th; that line has now moved three
  times, each time named in the drift guard with its pass.
- **`[checkpoints] enabled` is still false**, so `pending_promotions[]` and `memory_watermark`
  are filled from the fold and the checkpoint agrees with the journal by construction — but on
  this machine the checkpoint copy has still never been written. Unchanged since Pass 4.
- **Still owed before 8a, unchanged by this pass:** B11, B12, B13 and B21 under `chat`, and
  B23. Passes 8, 9 and 10 compare against `baseline-v2`, not v1. **The Pass 5 exit is still
  not met** — B22 is the missing task, with the open question of whether it may name its tool
  at the cost of no longer measuring tool selection.
- **Pass 8 reverts to supervised mode** by the note at the foot of `orchestrator-prompt-auto.md`:
  each 8x session moves a capability family, and a regression is easier to catch at the session
  boundary than four sessions later.
