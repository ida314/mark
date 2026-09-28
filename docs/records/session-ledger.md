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
| 8a | complete | 2026-09-24 | pass-08-outcome.md | committed as `e6a3b83` (+ `70f8394`, the measured comparison). Both 2026-09-23 gates cleared first: the pre-8a B11/B12/B13 rows were run attended and recorded as the `baseline-v2.md` addendum, and the approval-wall question was answered by the code. `fs_*` + `shell_exec` → `coder`. 6 rows 3p/1partial/2f → **6 pass**. Permitted 29 → 24; offered 20 → 18.7, i.e. unchanged. Two measurement incidents and a memory-store pollution, all in the record. |
| 8b | complete | 2026-09-24 | pass-08-outcome.md | committed as `b012016`. `web_search` + `web_fetch` → `researcher`; no ephemeral worker defined, on 8a's criterion (all six researcher tools are `allow` at every autonomy). Permitted 24 → 22, `always_on` 13. At 22 `select` does **not** short-circuit, so offered is capped rather than reduced. B14–B16 run pre/post against clones at 8a's and 8b's heads — `baseline-v2` is not a usable comparand, its B14/B16 died on the pre-8a fix-1 400. **partial/partial/fail → 3 pass.** Orchestrator web calls 11 → **0**; B16's orchestrator context peak 19 640 → 3 945 (−80%) while the run made 3.5× more web calls. Method improved on 8a's: per-process env overrides, so nothing shared was edited and no restore is owed. |
| 8c | complete | 2026-09-24 | pass-08-outcome.md | `memory_search` + `memory_history` → new `memory` durable role (the in-process `delegate(agent="memory")` branch is gone); `gmail_search` + `gmail_message` → new `mail` role, **not** `researcher`, which would have put the mailbox beside the only holder of the web tools. Permitted 22 → **18**, `always_on ∧ surface` 13 → **10**, `select` short-circuits — verified independently. Suite 1028, `ruff check .` clean. Two interlock repairs and a new `mail-delegation-never-unattended` rule. **B17 pass holds; B18/B19 uncovered (premise failures, not regressions); B20 not comparable — the model sent `memory_remember` with empty args twice.** **offered = 17 on every row**, deterministic for the first time. Interlock probed against the engine: 8c's `memory`/`mail` carve-out holds and the daemon→mail hole was real and is closed — but **`daemon + delegate(researcher)` is still `allow`**, and a worker may `web_fetch` at `observe`, so the same gap is open for the role that holds egress. Filed for 8d. |
| 8d | complete | 2026-09-25 | pass-08-outcome.md | full frozen suite (B01–B22) against `~/Projects/agent-8d` at `21977b4`, daemon stopped, promotion off, per-row setup in `~/Projects/agent-evals/run8d.sh` — B12's fixture applied then reversed, the clone reset after every row that edits it. **B23 is out of scope**: it is the multi-turn session row and has no single blockquote prompt, so the one-row harness cannot run it. **22 rows in 73.9 min: 11 pass, 3 partial, 7 fail, 1 uncovered. `offered = 17` on every row.** Of the 7 failures, 5 are tool-call fidelity and 2 are worker report fabrication; **none is a policy, surface or routing failure**. Headline: `delegate` is now the only door to five families and the 27B cannot reliably produce its two-required-argument schema — 9 rejected calls, and on B13 the withdraw-after-two guard left the turn with no route to anything. Four rows emitted tool-call markup as prose and the runtime did not notice (`status=completed`, `steps=1`). B12's worker fabricated a green test run — disproved directly here, the tests really do fail. `always_on` is **inert for the orchestrator** below the short-circuit threshold, so the pass file's borderline-tool question is moot; leave the flags alone. Still open: the `daemon + delegate(researcher)` gap, and eval pollution — 8c's recorded fix was implemented in a trap and **did not work**, because the consolidator re-derives candidates from episodes. |
| 9a | complete | 2026-09-25 | pass-09-outcome.md | `agent/verification.py`: the ledger folded out of the worker's own journal + six deterministic checks, no model call. `WorkerResult.validation`/`flags`; **vocabulary 24 → 25** (`worker_verified`), and `worker_finished` gained two required fields. Run in one sitting with 9b and 9c, on Dylan's instruction. |
| 9b | complete | 2026-09-25 | pass-09-outcome.md | tool success and validation status are separate axes (Dylan's ruling): `delegate` keeps `ok = completed` and carries `validation` beside it. Invalidated = not an answer, not cached, no candidate memories. **Deviation:** `remember` refuses `invalidated` only, not everything short of `valid` — an `uncertain` result is cached *with its doubt*, `entry_version` 1 → 2. |
| 9c | complete | 2026-09-25 | pass-09-outcome.md | one escape step (`delegate` only) before the tool-free summary step, on for every caller; `abandoned` is now `not ended_by_choice` rather than `steps >= max_steps`; a verbatim-repeated denial withdraws the tool with `DENIED_NUDGE`. 1067 tests green, ruff clean, **6 mutations all caught**. |
| 9d | complete | 2026-09-27 | pass-09-outcome.md | full frozen suite against `~/Projects/agent-9d` at `c187a80`, 8d method, 124.7 min, 22 rows / 32 workers / 30 verified. **Nine flag firings across two axes, nine false positives, zero true positives.** `file_not_read` **8/8 FP** — four mechanisms, three of them structural (prose containing a slash; a path that arrives in a tool *result*; the `/workspace` mount vs the host path a worker correctly reports; a role with no filesystem tools). `write_not_performed` **1/1 FP** — `CLAIMS_WROTE` matched `modified` inside *"no files were modified"*. `tests_not_run` and `status_conflict` **never fired**, so both are unmeasured, and `tests_not_run` has a *measured false negative*: `| tail -N` makes a failing pytest run `exit=0`, verified directly with B12's fixture applied. `completed_without_tools` cannot fire on the shape it was built from — all seven `steps=1` zero-call turns in `journal-8d.db` are **MAIN** turns, which are never verified. **The uncovered class is a report that understates its work** (B10 w1 "every tool call I made failed" / 10 of 15 ok; B12 w1 "did not run the suite or edit any files" / 13 calls incl. a patch and a pytest run) — both `valid`, because five of six checks are gated on `completed` and only 21–43% of real workers are. Escape step: granted **16×**, all to workers, all offering **no tools**; 16 extra model calls, never taken. `ended_by_choice` changed no recorded status. **All three hard flags now fall under the pass file's `Must not`** — one measured at 100% FP, two unmeasured. Nothing in `verification.py` changed; the five candidate fixes are Dylan's call. |
| interlude 2026-09-28 | complete | 2026-09-28 | interlude-2026-09-28-telegram-approvals.md | pass-file rule waived by Dylan. Session `01a0e9f6` diagnosed: tool calls never replayed into the next turn's history (two fabrications from one gap), Telegram turns built with a `QueueApprover` ("nobody is at the keyboard" to a person on a phone). Fixed: `history_messages` replays calls+results as one budget unit; `TelegramApprover` with Approve/Deny buttons and a 180 s wait; `answer_flagged` (vocabulary 25 → 26), `args_empty_stream`, `queued_approvals` on the worker door, `fs_read` wording. 1104 tests green, ruff clean, daemon restarted. |
| 10a | pending | — | — | discovery: registry and retrieval (was 9a) |
| 10b | pending | — | — | discovery: recall evaluation (was 9b) |
| 10c | pending | — | — | discovery: router (was 9c) |
| 10d | pending | — | — | discovery: precision evaluation (was 9d) |
| 11a | pending | — | — | evaluate: replay harness (was 10a) |
| 11b | pending | — | — | evaluate: full metric run (was 10b) |
| 11c | pending | — | — | **hard stop — human runs this**, recurring; one question per session (was 10c) |

## Carried forward

- **Renumbering, 2026-09-25.** Result verification was filed ahead of discovery, on Dylan's
  ranking, and took the number 9. The mapping, which every record written before this date
  needs:

  | says | means |
  |---|---|
  | Pass 9, 9a–9d | discovery & router — now Pass 10, `docs/plans/pass-10-discovery.md` |
  | Pass 10, 10a–10c | evaluate & tune — now Pass 11, `docs/plans/pass-11-evaluate.md` |

  The eight existing outcome records were **not** rewritten. A record describes what was true
  when it was written, and editing its prose to match a later numbering would make it a worse
  record, not a better one. Each moved plan file carries the same note at its top.

- **No longer owed as a manual step.** The daemon is stopped for the duration of a suite run,
  because its consolidation loop — not the harness — drains eval `candidate_memories` into
  `facts`. As of 2026-09-25 `~/Projects/agent-evals/run8d.sh` **owns that window itself**: it
  stops the daemon at the top and restarts it from a `trap ... EXIT INT TERM`, clearing the
  candidate queue first. The 2026-09-24 version left the restart as a note here, the session
  died, and the daemon came back only because someone started it 80 seconds later
  (`NRestarts=0`, `Restart=on-failure` — systemd would not have).

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

3. **The required-field precedent — CORRECTED 2026-09-23 by Dylan: two instances (5a, 6a) plus the 6b reshape, not three. 7c added nothing.** Ruled at the Pass 7/8 boundary; see below. 5a added
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

---

## Pass 8 opens — supervised. 8a held at step 5, two gates, 2026-09-23

Prerequisites for 8a all exist: `pass-08-tool-surface.md`, `pass-01-outcome.md`,
`pass-06-outcome.md`. Working tree carries no tracked modifications; five untracked paths
(`CLAUDE.md`, the three `docs/plans` prompt files, `repo-drop(1).zip`) are pre-existing and
were not written by a session.

**Gate 1 — the comparison 8a ends with is not meaningful today, and the reason is not the
count of missing rows but which rows they are.** The coding family is B02, B10, B11, B12,
B13. 8a moves `fs_write` and `shell_exec`. The only rows that exercise those two tools are
B11, B12 and B13, and the only variant that can exercise them is `chat`, because under `ask`
the `QueueApprover` denies both — which is the same defect gate 2 is about. Those three
`chat` rows are three of the five still owed. What is left with a recorded v2 value is B02
(needs no repository access, cannot see a coder-role regression) and B10 (read-only
comprehension, already `fail` at 12/12 steps). The v2 `ask` rows for B10/B11/B12/B13 are all
`fail`, and three of the four failures are environmental — the 400 chain at 12/12 steps, a
655 s router timeout on B12, and B13 stopping at the approval wall in words. **A comparison
against an all-fail floor cannot show a regression at all**, and an improvement in it is not
distinguishable from the router having a better day.

Minimum before 8a: **B11, B12, B13 under `chat`.** Strongly advisable: **B21 under `chat`** —
it is not a coding row, but it is the only baseline evidence of whether this model chooses to
delegate, and after 8a every coding row becomes a delegation row. **B23 is not needed for
8a.**

**Gate 2 — the 1c approval-wall question, and the code now answers it the bad way.**
`pass-01-outcome.md:346` asks which approver is in force inside a delegated role and whether
it can prompt. Pass 6 threaded an `approver` parameter into `run_subagent`
(`agent/subagents.py:134,183`), and `tools/builtin_delegate.py:83` reads it from
`ctx.extra["approver"]` — but **nothing in `src/` ever writes that key**, so the fallback at
`builtin_delegate.py:85-87` always fires and every delegated worker is built with
`QueueApprover(origin=ctx.origin)`, including under `agent chat` where the caller holds a
`CliApprover` that can prompt. A coder role given `fs_write` and `shell_exec` therefore gets
a queued denial on every write and every test run. 8a's exit criteria are unreachable until
this is decided.

Put to Dylan, undecided: inherit the caller's approver (one line, but it lets a worker prompt
the terminal mid-delegation); an autonomy contract making the coder's writes `allow` under a
scoped root; or queue-and-resume on Pass 3/4 machinery. And whether the fix is inside 8a or
its own commit before it — it touches Pass 6's files, which is a stop-and-ask condition.

**Carried into Pass 8 unanswered from the Pass 7 boundary, none of them blocking 8a:**
`migrations/0010_promotion.sql` unconfirmed; the required-field precedent at its third
instance, still unruled; `agent db migrate` not run, live DB at 0009, so a semantic promotion
on the live system fails and stays pending.

## Boundary actions taken 2026-09-23, on Dylan's instruction

**1. Gate 2 ruled: a delegated worker inherits the caller's approver.** Option one of three.
The cost accepted with it, stated in the option and not re-raised: **a worker can prompt the
terminal mid-delegation.** Not yet implemented — `ctx.extra["approver"]` is still written by
nothing, so `tools/builtin_delegate.py:85-87` still falls through to
`QueueApprover(origin=ctx.origin)` on every delegation. Open: whether the fix is its own
commit before 8a or inside 8a. It touches Pass 6 files, which is a stop-and-ask condition,
so the recommendation on the table is its own commit first — the shape of the `FINAL_NUDGE`
fix before the 5c eval.

**2. `agent db migrate` run. The live DB is at 0010.** Precondition verified before applying
rather than taken from 7c's record: `candidate_memories` held **116 rows, 0 with a
`promotion_key`, 0 duplicate keys** — so the partial unique index could not fail and
constrains nothing that already existed. `agent backup` taken first:
`~/.local/share/agent/backups/20260923T204418Z` (db 1.4 MB, repo 7 kB). After: `applied: 10,
pending: none`, `candidate_promotion_key_idx` present with the definition `0010` specifies,
116 rows unchanged.

**This resolves Pass 7 boundary item 4, and it resolves item 2 by action rather than by
ruling** — `agent db migrate` *is* the application of `0010_promotion.sql`, the migration
that was awaiting confirmation. Nothing has written a `promotion_key` yet, so nothing depends
on the index; it is one `DROP INDEX candidate_promotion_key_idx;` from gone if the answer
would have been no.

**Still owed, and 8a is still held on the first of them:**
- **Gate 1 is unanswered.** B11, B12, B13 under `chat` before 8a, or dispatch 8a knowing its
  closing comparison reads an all-fail floor and cannot show a regression.
- **The required-field precedent, third instance, still unruled** (Pass 7 boundary item 3).
- 7a's finding is still Dylan's to read (Pass 7 boundary item 1).

## Pass 7/8 boundary — Dylan's three rulings, 2026-09-23

### 1. The required-field precedent: ratified, with four conditions, and Pass 9 pre-decided

**The premise the orchestrator put was wrong and the correction is the substance.** There are
**two** instances, 5a (`agent_finished`) and 6a (`worker_created`) — **7c added nothing.**
And there is a larger one nobody recorded: **6b renamed and replaced fields on
`worker_finished`, and every live row now fails ten clauses.**

**The invariant does not hold because the reducer is forgiving. It holds because no fold
reads any of the drifted fields.** That is a different and much weaker guarantee than the one
5a, 6a and 7c each relied on, and it is the reason this was rulable at all. The only visible
symptom today is `journal/render.py::_worker_finished`, which prints `payload['status']`
verbatim — `researcher: ok`, an off-vocabulary value, with nothing marking it as a legacy row.

**Ruled: required means required-going-forward, as a written rule rather than an accident.**
Rejected: demoting to `opt()`, which would give up the absent-versus-null distinction 5a
exists to enforce on every *future* write merely to make old rows legal; and versioning
everything, because nothing reads these fields. **Four conditions, all binding:**

1. **Reading a drifted field requires a version guard.** The first time a fold or reader needs
   a field that pre-existing rows lack, that type gets a payload version and the reader
   follows the `handoff_schema` pattern: known versions listed, unknown ones refused, absence
   **stated rather than defaulted**. Adding a field is cheap only while nothing reads it.
2. **Renames and removals on a type with live rows are a stop-and-ask.** 6b set that precedent
   without anyone noticing; it is recorded here as the precedent it actually is.
3. **Land the 14 real drifted rows as a fixture test** that folds, summarises and renders them.
   All 966 tests pass whichever way this is ruled — *which is why it was never ruled.*
4. **Legacy rows get marked on render.** `render.py` shows an off-vocabulary status as a legacy
   value instead of printing it verbatim.

**Pre-decided for Pass 9, and it decides what 9a builds:** the routing record is a **new event
type, not a field on `worker_created`.** Additive, no existing row can fail it, and it avoids a
third instance. **Rule stands before 9a is planned.**

### 2. Gate 1: do not dispatch against the current floor — and running the rows today would not fix it

**The all-fail floor carries no capability signal**: three of the four v2 coding results were
harness or environment failures and the fourth (B13) was by design. **The daemon is still
running pre-fix code from the v2 run**, 400-ing on every heartbeat and cancelling concurrent
requests, so running B11–B13 under `chat` today would mostly re-measure that.

**The approved order, and 8a does not move until it is done:**

1. Land the two fix commits (§3).
2. `systemctl --user restart agent-daemon.service`, and confirm the next heartbeat completes.
3. Run **B11, B12, B13 under `chat`**, attended, on the tree 8a will branch from. ~1 hour.
4. Record them as an **addendum to `baseline-v2.md` §2**, and **that addendum is 8a's
   comparand** — the coder family has never had a measured baseline and the tree has moved 13
   `src` commits since v2.
5. Dispatch 8a.

**Caveat to carry into 8a:** B21, and every coder row after 8a, is a **delegation** row, and
delegation has never executed against this model. **8a's comparison is only meaningful if fix
commit 1 actually makes a worker turn complete — verify with a live probe before dispatching,
not with tests.**

### 3. Packaging: two standalone commits before 8a, in the `3015aa0` shape

**Commit 1 — prompt shape. First, because without it no worker runs at all.** Fold
`extra_system` into the single leading system message at `loop.py:472`, the way
`build_messages` already handles `handoff_block`. Test asserting **exactly one leading system
message**, mutation-checked by restoring `insert(1, …)` and confirming the test fails. Verify
against vLLM on **:8001** both ways.

*Verified at the boundary before dispatch:* `loop.py:471-472` is the `insert(1, …)`, and
`subagents.py:244` passes `extra_system=spec.prompt` — so **every worker has always been sent
a second system message at index 1**, the same shape Qwen3's template rejects with the 400
that `baseline-v2.md` v2-1 traced through `sir`. This is the mechanism behind "delegation is
dead on this machine" (three sub-agent turns, all under 110 ms, `llm_ms: 0`), carried since
v2 and never explained.

**Commit 2 — approver.** `tctx.extra["approver"] = self.approver` in `loop.py`, keeping the
fallback in `builtin_delegate.py`. Loop-level test asserting the context `run_turn` builds
carries the caller's approver, mutation-checked by deleting the line. **The existing tests in
`test_delegation.py` set the key by hand, which is why this survived.**

**Neither commit touches Pass 6 files, so the stop-and-ask does not trigger** — that is the
point of putting the assignment in `loop.py` rather than in `builtin_delegate.py`. Both are
logged here as pre-8a fixes rather than as pass sessions.

### 7a

Non-blocking; Dylan reads it at the pass boundary as planned. **Its violation table is right
but understates `worker_finished`** — see ruling 1.

## Pre-8a fix commit 1 — landed in the working tree, uncommitted, 2026-09-23

`agent/context.py` (`build_messages` gains `extra_system`, folded into the leading system
message straight after `handoff_block`) and `agent/loop.py` (the `insert(1, ...)` deleted,
call site passes it through). No new module, so no `conftest.py` change; `builtin_delegate.py`
untouched as instructed.

**The mechanism is confirmed against the real model, in its own words.** Probe against vLLM on
8001 (`owned_by` re-checked: `"sir"` on 8000, `"vllm"` on 8001), prompt assembled by the real
`build_messages`:

```
BEFORE  roles=['system','system','user']  → HTTP 400
        {"error":{"message":"System message must be at the beginning.", ...}}
AFTER   roles=['system','user']           → HTTP 200  "The capital of France is Paris."
```

**So "delegation is dead on this machine" — carried since `baseline-v2.md` and never
explained — has a cause.** Every delegated turn 400-ed before the model was asked anything.
The three sub-agent turns under 110 ms with `llm_ms: 0` are that 400.

**Mutation, re-run independently by the orchestrator rather than taken on report:** restoring
`messages.insert(1, ...)` fails **exactly the two new tests, 2 failed / 966 passed — zero
pre-existing tests bit.** Nothing in 968 tests defended this. The subagent also reports a
second mutation (deleting the fold in `context.py`) killing the same two. Gates verified
directly: `uv run pytest` 968 passed, `ruff check .` clean, `ruff format` not run.

**Live data untouched, verified both sides:** journal 1227 rows / 55 run ids / 24 effect / 0
checkpoint, Postgres `candidate_memories` 116, `facts` 47 — identical before and after. The
journal copy took the `-wal`.

**Found and deliberately not fixed — a latent third site, for Dylan to rule on.**
`context.history_messages` does `picked.insert(0, {"role": "system", "content": "Summary of
earlier conversation..."})` when a session has a `summary`, which lands at **index 1** of the
assembled list and would 400 identically. It is unreachable today — no caller of
`repo_archive.end_session` passes a summary and `sessions WHERE summary IS NOT NULL` is 0 —
so it was left as out of scope, and the new test would catch it the moment a summary is
written. **Whoever gives that path a producer inherits a live 400.**

Also confirmed in passing: `FINAL_NUDGE`/`STUCK_NUDGE` are already `{"role": "user"}` since
the 2026-09-22 fix, so `extra_system` was the **last live** mis-positioned system message;
every other `{"role": "system"}` in `src/agentd` is at index 0.

## Pre-8a fix commit 2 — landed in the working tree, uncommitted, 2026-09-23

`agent/loop.py` only: `tctx.extra["approver"] = self.approver` at line 508, in the existing
`extra`-key block after the `ToolContext` constructor, with a comment naming the failure.
`builtin_delegate.py` untouched and its fallback left in place, so the diff stays out of Pass 6
files and the stop-and-ask does not trigger.

Brief confirmed on every point: `self.approver` is set at `loop.py:302`; before this change
`grep -rn '"approver"' src/` returned **exactly one hit** — the read at `builtin_delegate.py:83`
— so nothing ever wrote the key; `subagents.run_subagent(approver=...)` already threaded it into
the worker's `AgentLoop`.

**Mutation, re-run independently by the orchestrator:** deleting the line kills **1 test, the new
one. 968 passed. No pre-existing test died.** The subagent also mutated it to a different
`AutoApprover` instance (right type, wrong object) and the same single test died on identity.
Gates verified directly: **969 passed**, `ruff check .` clean, `ruff format` not run.

**That zero-pre-existing-kills result is itself the answer to why this survived Pass 6.**
`test_delegation.py:287,328` and `test_worker_results.py:252` each build a `ToolContext` by hand
with `extra={"approver": AutoApprover(True)}` and call `builtin_delegate.delegate.handler`
directly, bypassing `run_turn` entirely. **The hand-set key still fully masks the bug**; all three
tests left untouched, as instructed.

**Live data identical both sides:** journal 1227 rows / 55 run ids, `candidate_memories` 116. No
new module, so nothing owed to the `conftest.py` monkeypatch list.

**OWED TO DYLAN BEFORE THE ATTENDED RUNS — a worker's approval prompt is indistinguishable from
the orchestrator's.** He accepted mid-delegation prompting as the cost of ruling "inherit the
caller's approver"; he has not seen what the prompt says. Reported, not changed:

- `ApprovalRequest` (`policy/approvals.py:14`) has **no `actor` and no `role` field**, so a
  worker's `actor` (`subagent:<name>`) cannot reach the prompt even in principle.
- `origin` **does** reach it — `run_turn(origin=f"subagent:{spec.name}")` (`subagents.py:241`) →
  `ApprovalRequest(origin=ctx.origin)` (`executor.py:143`) — but `CliApprover.request`
  (`cli/chat.py:59`) never renders it. The attribution is on the object and discarded by the
  renderer. **One line to add to the panel body.**
- `session_id` cannot substitute: a worker's `Session` is built with `id=parent_session_id`
  (`subagents.py:189`), identical to the caller's. Same shape as the Pass 6 privacy item.

This matters for step 3 of the approved order specifically: during B11-B13 attended, Dylan is the
one answering these prompts, and a worker's `fs_write` will look like the orchestrator's.

## Daemon restarted, and the live journal names both 400 producers — 2026-09-23 20:01 EDT

Dylan committed fix 1 and ran `systemctl --user restart agent-daemon.service`. Service active,
running `/home/dylan/Projects/agent/.venv/bin/agent`, an editable install pointed at the working
tree — so the restarted daemon is carrying **fix 1 and the uncommitted fix 2** as well.

**His premise was right and the journal proves it, but not through the agenda.** A provider 400
does not raise out of `run_turn` — it is journaled as `agent_finished status=failed` — so
`repo_agenda.notify(title="Heartbeat failed")` has **never once fired** (no `source='daemon'` row
has ever been written; 64 notifications total, one `heartbeat` row ever, from 2026-09-20).
**Anyone looking for daemon health in the agenda would conclude the daemon was fine.**

The journal says otherwise: **27 failed turns, and 33 rows carrying the string `System message
must be at the beginning`.** Broken down, and this is the useful part — **it is one error string
with two independent producers:**

| producer | count | step count | cause |
|---|---|---|---|
| `daemon:heartbeat` | 14 | 4 = its `max_steps` | `FINAL_NUDGE` at the step budget |
| `main` (cli 7, telegram 3) | 10 | 12 = `max_steps` | `FINAL_NUDGE` at the step budget |
| `subagent:researcher` | 6 | dies at step 1 | `extra_system`, i.e. **fix 1** |
| `subagent:coder` | 3 | dies at step 1 | `extra_system`, i.e. **fix 1** |

**The nudge half was already fixed in code on 2026-09-22** — `loop.py:538` appends `FINAL_NUDGE`
as `{"role": "user"}` — but the daemon process predated that commit and Python does not reload on
an editable install, so **the running process kept 400-ing on code that had been fixed in the tree
for a day.** The most recent was 2026-09-23T18:11:05Z, 45.3 s wasted after four completed steps
and two real tool calls, answer_chars 0. Restarting is the whole fix for that half.

**The subagent half is independent live confirmation of fix 1, from Dylan's own journal rather
than from a probe.** Nine delegated turns, all dead at step 1 before the model answered anything
— which is exactly `baseline-v2`'s "delegation dead: three sub-agent turns, all under 110 ms,
`llm_ms: 0`", now with a cause.

**Watching for the next heartbeat** (interval 1800 s, and it sleeps *before* its first iteration,
so ~00:31Z; it also makes no model call at all unless the situation report is actionable and its
digest changed, so a quiet tick is not evidence of a fix).

**Method note worth keeping: daemon health is not visible where it looks like it should be.** The
`except Exception` handler around the heartbeat catches everything except the failure mode that
actually happens. Worth a Pass 10 entry.

## Step 2 confirmed, and the live delegation probe — 2026-09-24 00:31Z and 00:38Z

**The heartbeat completed a real model turn for the first time.** 00:31:53Z, 30 min after the
restart: 4 steps, five real tool calls (`open_loops_list`, `memory_search`, `coursework_due`,
`calendar_upcoming`), `FINAL_NUDGE` appended as a **user** message (168 chars, "[runtime, not
from the user]...") and **no 400**, a 1234-char answer, and the suggestion reached the agenda
(`notifications`, `source=heartbeat`, 00:31:53 — only the second such row ever). Against
2026-09-23T18:11:05Z: same 4 steps, `answer_chars` 0, HTTP 400. **Step 2 of the approved order
is met.**

Status reads `abandoned`, not `completed`, and that is not a failure: `loop.py:829` sets
`abandoned` whenever `steps >= max_steps`, and the heartbeat's `max_steps` is **4**. **So a
working heartbeat can never report `completed` unless the model finishes in three steps** — it
used all four both times. Its answer is by construction "a summary of unfinished work, not an
answer" (`loop.py:825`), and that is the text `repo_agenda.notify` files as a Suggestion.
**Pass 10 tuning item, not a bug.**

### The probe: fix 1 works, and it uncovers the blocker for 8a

One live delegation to the **coder** role, read-only task, `AutoApprover(approve=False)` so a
write could not be approved by accident.

**Dylan's caveat is satisfied: a worker turn now completes.** 15 steps, 14 tool calls, 52.1 s of
model time, 64.7 s wall, ending in a 714-char answer. Before fix 1, `subagent:coder` died at
step 1 with `answer_chars` 0. **The 400 is gone in the live path, not only in a probe harness.**

**And Pass 6's open gap closes with it.** Pass 6 recorded that "the schema has never decoded a
report a real 27B wrote". It has now: `worker_finished` carries `report_valid: true`,
`status: uncertain`, `evidence: 2`, `followups: 2`, `actions_taken: 0`, and the summary is
honest — *"Could not list Python files in src/agentd/agent because that path does not exist in
the workspace."* A truthful `uncertain` with a valid report is exactly 6b's design working.

**THE FINDING, and it should stop B11-B13 from being run yet: the coder cannot see the
repository.** Tool tally for the run — `fs_list` ×10, `fs_read` ×1, `shell_exec` ×3 — and every
one failed:

- `fs_list path="src/agentd/agent"` → `Not a directory:
  /home/dylan/.local/share/agent/workspace/src/agentd/agent`. Relative paths resolve under the
  empty workspace, as `builtin_fs.py:18` has always done.
- `fs_list path="/home/dylan"` → denied, `rule: fs-outside-roots`.
- `fs_list path="."` and the workspace root → `(empty directory)`.
- `shell_exec` ×3, all against `/workspace` (the sandbox's container path) → denied,
  `rule: risk_matrix:write/assist` (the probe's own approver, by design).

**This is `pass-01-outcome.md:346`'s second item, the one filed as "Related, and cheaper to
answer" beside the approval-wall question — and unlike the approval wall, nobody ever answered
it.** Its own words: *"'in this repository' resolves to nothing. Every repo task in the suite
failed on this before it reached the capability it was written to measure. A coder role inherits
the problem unchanged."* Confirmed live, against the actual coder role, 24 hours before 8a would
have created one.

**Consequence for the approved order:** 8a gives the coder role `fs_*` and `shell_exec`. If a
path into Dylan's repo cannot be resolved, B11/B12/B13 will fail after 8a for the same reason
they failed in v2, and **8a's comparison will measure path resolution a second time instead of
tool surface.** Running the attended rows *before* this is settled bakes that into the
comparand. **Put to Dylan: this needs an answer before step 3, not after.**

Noted in passing, a correction: **tool events now carry `worker_id`** (`tool_requested`,
`tool_started`, `tool_failed`, `tool_finished`, `message_appended` all have it) and `step_id` is
namespaced by the worker's turn id. The carried item "effect events carry no `worker_id`
(3b #4)" is still true of *effect* events only.

**Live data written by the probe, stated rather than glossed:** journal 1248 → 1313 rows (64 in
the probe run `8002fd81-...`, plus one stray single-row run `885a06a9`), one new `sessions` row
(`01a0d0d9-6d9d-7207-aade-1c9d5e3b3a12`), no effects, no promotions, no memory writes.

## Pass 7/8 boundary — three more rulings, 2026-09-24

Dylan took the recommended option on all three, bare, so the cost written into each option is
the cost accepted.

### Ruling 1 — the coder cannot find the repo: option (c), both halves, one commit

**Not a permissions problem, and two problems rather than one.** `allowed_roots` already
contains `~/Projects`, so an absolute path into the repo works today; the probe failed because
the model used *relative* paths, which `builtin_fs._p()` sends to the empty workspace, and its
one absolute guess (`/home/dylan`) was outside the roots while the correct parent was inside
them. **There is no `project` key in `config.py` — nothing tells a worker where it is.**
Separately, `builtin_shell.py:26` mounts `{cfg.paths.workspace}:/workspace:rw`, the *empty*
directory, so `shell_exec` cannot see the repo by construction.

**That second half is load-bearing for the owed rows.** B12 (diagnose a seeded test failure) and
B13 (run the suite and interpret a large output) both require running tests against the repo.
**Neither could pass today whatever 8a did to the tool surface.**

Accepted with the option, and binding on the implementation: the project root is **named
explicitly in config, never auto-detected** (inference is a recorded failure mode here), and
**`fs_*` stays absolute-only — `_p()`'s semantics do not move.** The fix is that the worker
learns the absolute path. The sandbox mount is **rw** on the real repo, accepted knowingly,
while Dylan edits the same tree in parallel. Rejected: (a) alone (B12/B13 still cannot run a
test), (b) alone, and (d) scoping the coder to the workspace and accepting an uninformative
comparand.

### Ruling 2 — the anonymous worker approval prompt: option (a), render it

One line in `CliApprover.request` (`cli/chat.py:59`). `origin` already reaches
`ApprovalRequest` (`executor.py:143`) carrying `subagent:coder` and the renderer discards it;
`ApprovalRequest` has no `actor`/`role` field and `session_id` is the caller's, so `origin` is
the only signal that exists. Rejected: accepting it for the attended runs, on the grounds that
Dylan answers these prompts personally during B11-B13 and a worker asking to write is the case
the gate-2 ruling exists for.

### Ruling 3 — the latent third 400: option (b), leave it armed

`context.history_messages` inserts a `"Summary of earlier conversation..."` **system** message at
`picked[0]`, landing at index 1 and 400-ing identically. Genuinely dead: nothing passes a summary
to `repo_archive.end_session` and `sessions WHERE summary IS NOT NULL` is 0. **No code this
pass.** Fix 1's test is the guard and catches it the first time a summary is written. **Whoever
gives that path a producer inherits a live 400** — that is the accepted cost, and the reason this
entry exists.

### Also ruled at the same time: from here, remaining tasks are dispatched to subagents.

**Outstanding action, not a ruling: fix 2 is still uncommitted.** `tctx.extra["approver"] =
self.approver` and its test are live in the running daemon (editable install) and absent from
git. A restart before committing silently reverts it.

## Ruling 1 implemented — and it uncovers why B12 and B13 still cannot pass

In the working tree, uncommitted: `config.py` (`PathsConfig.project`, `project_root`, and a
validator refusing a project outside `allowed_roots`), `config/default.toml` (documented,
**commented out**), `agent/subagents.py` (`project_block(cfg, *, has_shell)` appended to the role
prompt, **empty when no project is configured — no guess**), `tools/builtin_shell.py`
(`mount_source()` = `project_root or workspace`, mounted rw at `/workspace`, preview and
description naming what is really mounted, and a refusal to start when the mount source does not
exist — `docker run -v` would otherwise create it as root and every command would then lie), plus
`tests/test_project_root.py` (11 tests). **`_p()` untouched, as the ruling required.**

**980 passed** (969 before), ruff clean, verified directly. **7 mutations, all killed, no
pre-existing test died for any of them.** One finding inside that:
`test_sandbox_never_mounts_the_docker_socket` did **not** die under the mount mutation — it
asserts the workspace path appears in the command line and passes only because `conftest` leaves
`project` unset. **It stops biting on the mount source the moment a project is configured.**

**Not live.** `~/.config/agent/config.toml` has no `project` key and the subagent correctly did
not edit user config outside the repo. Until `project = "~/Projects/agent"` is added under
`[paths]`, behaviour is identical to today. **That is the one manual step, and it is what arms
the rw mount.**

**What `shell_exec` can do once it is set, in one sentence:** any command the model writes runs as
uid 1000 with `/home/dylan/Projects/agent` mounted read-write as its working directory, so it can
overwrite or delete Dylan's uncommitted in-editor work — `rm -rf`, a stray `git checkout`/`clean`,
or `ruff format .` rewriting 75 of 126 files — with no undo, and the container being throwaway
protects nothing.

### THE SANDBOX CANNOT RUN THIS PROJECT'S SUITE — three blockers, all verified directly

1. **No toolchain.** `agent-sandbox:local` has `python3` = **3.12.14** and `git`, and **no `uv`, no
   `pytest`, no gcc/make/node.** This project runs on **3.14**.
2. **The venv dangles.** `.venv/bin/python` targets
   `~/.local/share/uv/python/cpython-3.14.*/python3.14`, outside the mount, so `readlink -f`
   resolves to nothing inside the container.
3. **Postgres is unreachable.** It is published `127.0.0.1:55432->5432` — host loopback only.
   From the sandbox, `127.0.0.1`, `172.17.0.1` and `host.docker.internal` are all
   closed/unreachable, so `conftest`'s `pg_dsn` would `pytest.skip` the whole suite and **the best
   output a worker could honestly report is "skipped".**

**So B12 (diagnose a seeded test failure) and B13 (run the suite and interpret a large output)
cannot pass, and the mount alone does not buy them.** This is not a tool-surface property and 8a
cannot fix it.

**Separately, and it contradicts `CLAUDE.md`:** `uv run pytest -m docker` reports **980 deselected,
0 selected — the docker-marked suite is empty.** `CLAUDE.md` names `-m docker` as a real gate.
Nothing has ever been marked. The subagent's container checks were run by hand with a read-only
mount.

**Live data unchanged:** journal 1313 rows / 58 run ids before and after; `candidate_memories` 116;
`facts` 47. No conftest change owed — `builtin_shell` and `subagents` are already on the
monkeypatch list.

**Reported, not acted on:** `agent init` / `agent doctor` print the workspace and never the
project, so a misconfigured `project` is invisible there.


---

## Pass 8 opens for real — three pre-8a commits landed, 2026-09-24

The working tree that had been carrying pre-8a fix 2 and ruling 1's implementation is now
committed. Each was verified alone in a detached worktree (`git worktree add --detach`), not
merely green beside the others: **980 passed at `91fa9c6` with nothing of Dylan's in the
tree**.

| sha | what it is | ruled |
|---|---|---|
| `c9aa84f` | fix 2 — a delegated worker inherits the caller's approver | gate 2, 2026-09-23 |
| `91fa9c6` | `[paths] project`, the role-prompt block, the sandbox mount | ruling 1, 2026-09-24 |
| `a4b32e4` | the approval prompt names the worker that is asking | ruling 2, 2026-09-24 |

Ruling 2 had been ruled and **not implemented** — `cli/chat.py` still discarded `req.origin`.
It is implemented now, and the mutation was re-run by the orchestrator rather than taken on
report: reverting the render kills **2 of the 3 new tests and 0 pre-existing**, and making the
orchestrator branch claim a worker kills the negative test alone, so it is not vacuous. Three
signals fire together for a worker (title, magenta border, a bold first body line) because the
person answering these prompts during an attended eval run is answering a stream of them.

**`[paths] project` is now live.** `~/.config/agent/config.toml` gained
`project = "~/Projects/agent"` under `[paths]` (backup of the pre-edit file in this session's
scratchpad). Verified through the real config, not by reading the file back:
`cfg.paths.project_root` and `builtin_shell.mount_source()` both resolve to
`/home/dylan/Projects/agent`, and `project_block` renders the path plus the absolute-paths
sentence, with the /workspace sentence for the coder and without it for the researcher.

**That arms the read-write mount, which is the accepted cost of ruling 1 and is now real
rather than prospective.** Any command a model writes runs as uid 1000 with
`/home/dylan/Projects/agent` mounted read-write as its working directory. Also now true, and
noted at ruling 1: `test_sandbox_never_mounts_the_docker_socket` passes only because
`conftest` leaves `project` unset, so it stops biting on the mount source in any context where
a project is configured.

## Gate 1 put to Dylan again, and ruled — the sandbox gets fixed first

**The blocker is larger than the ledger recorded.** The 2026-09-24 entry said B12 and B13
could not pass; in fact **all three gate-1 rows** are unrunnable, because B11's rubric requires
`uv run pytest` green as well. So the approved order's step 3 — "run B11, B12, B13 under
`chat`, attended" — could not have been executed as written, and running it would have
produced three environmental failures indistinguishable from a capability floor.

Confirmed rather than assumed: `tests/conftest.py::pg_dsn` is **session-scoped** and calls
`pytest.skip` on an unreachable Postgres, and essentially every test reaches it through the
`cfg` fixture — so the whole suite skips, it is not a partial loss. And
`migrations/0001_extensions.sql` needs **`vector` and `pg_trgm`**, so any Postgres the sandbox
gets has to be a pgvector build, not stock.

**Ruled: fix the sandbox, self-contained.** The image carries its own Postgres on container
loopback and its own prebuilt environment; **`--network none` stays the default posture and
the sandbox never reaches the host DB or the internet to run the suite.** Rejected, and the
rejection is the substance: giving the container a route to `127.0.0.1:55432` is cheaper and
less exotic, and it would hand a sandbox running model-written commands network access to the
agent's own memory database — 47 facts, 116 candidates, the whole Postgres side of the journal.
The isolation that made `shell_exec` acceptable is not spendable on a convenience. Also
rejected: amending gate 1 to the rows that can execute (B02, B10, B21), on the grounds that the
write-and-run-tests half of the coder role is exactly the half 8a moves, and Pass 10 would
inherit a coder role nobody had ever seen finish a change.

**Costs accepted with the option, stated when it was offered:** a session of environment work
that no pass file owns, delaying 8a by that session; a much larger image whose baked venv goes
stale whenever `uv.lock` moves, with nothing that detects the drift; and the verbatim command
problem — **B12's task text instructs the agent to run `uv run pytest tests/test_telemetry.py`**,
so `uv run pytest` has to work offline in the container rather than be replaced by a bespoke
command the worker would have to be told.

**Where this sits in the approved order.** Steps 1 and 2 are met. Step 3 is now preceded by the
sandbox work. Steps 4 (the `baseline-v2.md` §2 addendum) and 5 (dispatch 8a) are unchanged.

### A finding that decides what 8a has to build: "not offered" does not mean "cannot call"

Found while reading the surface 8a is supposed to reduce, before any 8a code was written.
There are **four** doors by which a tool reaches a turn, and the pass file's "move it out of
the orchestrator surface" only closes two of them:

1. `Registry.select` — `always_on`, plus what the session already used, plus the top-k
   embedding matches above `SIMILARITY_FLOOR` (`tools/registry.py:84-110`).
2. `AgentLoop._with_lookup` — the handoff manifest's special case, both directions.
3. `tool_search` — the model asks for more, and `ctx.extra["added_tools"]` makes them visible
   on the next step (`agent/loop.py:805-811`).
4. **`agent/loop.py:699-700` — the model names a tool it was never shown, and the loop adds
   it to `exposed` and runs it.** `tools/executor.py` performs no visibility check of any
   kind; the only consequence is that `tool_requested` is journaled with `visible: false`
   first, which is Pass 1a's `not_visible` selection-failure kind. **The call still executes.**

**Probed, not inferred.** A scratch test (run and deleted, nothing left in `tests/`) built an
`AgentLoop` over a registry holding one tool, replaced `select` with one that returns `[]`, and
had the provider call that tool by name: `offered=[] registered=yes ran=True`. A first attempt
was inconclusive and is worth recording — with one tool registered `select` returns everything,
because `len(enabled) <= ALWAYS_EXPOSE_LIMIT` short-circuits it, so the tool *was* offered and
the probe proved nothing. The second version forces the empty offer.

So `fs_write` and `shell_exec` are reachable from the orchestrator today by *name alone*,
whatever `select` does, and removing them from `always_on` — which they are already not on —
would change nothing that matters. A flag that only `select` and `tool_search` honour would
leave door 4 open and 8a would report a surface reduction it had not made.

**The mechanism that closes all four at once already exists and is the one workers use.** A
worker's loop is built with `Registry(tools=registry.subset(spec.tool_names, spec.tool_tags))`
(`agent/subagents.py`), so a name outside the subset is simply not in `self.registry.tools`
and door 4 cannot open. The orchestrator, by contrast, is built with the full
`get_registry()` (`agent/loop.py:300`). **The symmetric fix is to give the orchestrator a
subset too**, rather than to add per-tool visibility flags to `select`.

Consequence for 8a's brief, and for 8d's measurement: the exit criterion "every moved tool
reachable through a durable role" has a matching negative that nothing currently tests —
*not reachable any other way* — and it should be a test, not a description.

### Checked before arming the mount: what fix 2 actually propagates on the unattended paths

The rw mount plus "a worker inherits the caller's approver" is only safe if the unattended
callers hold an approver that cannot approve. Verified by reading every construction site
rather than assuming: **every unattended path builds `QueueApprover`** —
`daemon/heartbeat.py:123` and `daemon/scheduler.py:55` (`origin="daemon"`),
`daemon/telegram.py:331`, and `cli/app.py:627,1626` for `agent ask`. `CliApprover` is
constructed in exactly one place, `cli/chat.py`, which is attended by definition.

So fix 2 hands a daemon-initiated worker the daemon's `QueueApprover`, which queues the call
and returns a denial — the coder's `shell_exec` stays denied on the heartbeat path, and the
only route by which a worker can actually write to the mounted repo is a human sitting at
`agent chat`. That is the shape the gate-2 ruling wanted, and it now holds by construction on
both sides rather than by the accident that nothing wrote `ctx.extra["approver"]`.

**A worry raised here and then disproved — recorded because the disproof is the useful
part.** This entry first said that under `agent chat --autonomy act` a delegated coder's
`shell_exec` might resolve to `allow` and write with no prompt, since the role's
`autonomy_cap` is `"act"` and `cap_autonomy` caps rather than raises. **Wrong.** Evaluated
against the real policy engine and the shipped `config/policy.default.yaml`:

| tool | observe | assist | act |
|---|---|---|---|
| `shell_exec` | **deny** | require_approval | **require_approval** |
| `fs_write` | **deny** | require_approval | **require_approval** |
| `delegate` | allow | allow | allow |

`risk_matrix:write/act` is `require_approval`, not `allow`. There is no autonomy level at
which a write to the mounted repo skips the approver. The claim is struck rather than
softened.

**And the heartbeat is two layers further from it than the approver argument suggested.**
`daemon/heartbeat.py:118,130` runs at `autonomy="observe"`, not `assist`, so
`cap_autonomy(min(...))` caps a heartbeat-delegated coder at `observe`, where `shell_exec`
and `fs_write` are **`deny`** — refused by the policy engine before any approver is consulted.
So the `QueueApprover` is the *second* thing stopping an unattended write, not the first.

**The running daemon does not have the new config.** It started 2026-09-23 20:01 and
`get_config()` resolves at process start, so `[paths] project` is invisible to it until
`systemctl --user restart agent-daemon.service`. Not restarted here: it is not on the
critical path (the attended rows run under `agent chat`, a fresh process), and a restart is
what arms the mount for the heartbeat's own delegations. Dylan's call.

### The before-number 8d compares against, measured rather than assumed

Read off the live telemetry (`~/.local/share/agent/logs/telemetry.jsonl`, 52 records, 46 with
`role == "main"`) and the live journal (copied with its `-wal`, 1313 rows / 58 runs).

**The orchestrator is offered 20 tools on 39 of 46 real main turns** (19 on six, 18 on one),
out of a registry that is now 29 enabled tools — it was 26 when the Pass 1 baseline was taken.
Thirteen of those are `always_on`: `calendar_upcoming`, `coursework_due`, `delegate`,
`gmail_message`, `gmail_search`, `goals_list`, `handoff_lookup`, `memory_remember`,
`memory_search`, `notify_user`, `profile_read`, `time_now`, `tool_search`. The rest arrive by
embedding similarity.

So **the number Pass 8 has to move from is 20, not 13.** "Permanent surface" in the pass file
reads naturally as the `always_on` set, but what a turn actually pays for in prompt tokens and
in selection error is the offered set, and the two differ by seven. Both numbers belong in the
8d table. Note also that the pass file's target surface names `reminder_set`, `watcher_add`
and `open_loops_list` as permanent, and none of the three is `always_on` today — so 8a–8c are
not purely subtractive, and the borderline cases it asks to decide empirically
(`goal_upsert`, `open_loop_add`, `open_loop_close`, `profile_read`) start from a mixed state.

**And the honest limit on the door-4 finding above: it has never fired here.** All **258**
`tool_requested` events in the live journal are `visible=1, known=1` — not one call to a tool
the turn had not been shown. So door 4 is a latent hole in what 8a can *guarantee*, not an
observed behaviour, and the 8a record must not write it up as if the model were already
reaching around the surface. What it means is narrower and still decisive: after 8a, "the
orchestrator cannot call `shell_exec`" would be a claim the code does not support, and one
`FakeProvider` test naming the tool directly would expose it.

## The sandbox now runs the suite — `a8639a8`, 2026-09-24

Ruling implemented. `docker/sandbox.Dockerfile` rebased on `pgvector/pgvector:0.8.1-pg17`
with `uv`, a 3.14 interpreter and `uv sync --frozen` into `UV_PROJECT_ENVIRONMENT=/opt/venv`
at build time; a cluster initdb'd at build as uid 1000 and copied into the tmpfs per
container; `docker/sandbox-profile.sh` at `/etc/profile.d/10-agent-sandbox.sh` to start it
and export `AGENT_DB__DSN`; `.dockerignore`; and the tmpfs raised 256m → 512m in
`builtin_shell.docker_command`.

**Verified by the orchestrator, not taken on report**, against a detached worktree mounted at
the real `docker_command` flags:

| check | result |
|---|---|
| `uv run pytest` in-container, `--network none` | **983 passed**, 0 failed, 0 skipped — host parity |
| three consecutive runs at 512m | 983 / 983 / 983, tmpfs peak **352MB of 512** |
| the same at 256m | run 1 green, **run 2 `18 failed, 664 passed, 301 errors`**, tmpfs 100% |
| `uv run pytest tests/test_telemetry.py` (B12 verbatim) | 19 passed |
| `sys.prefix` under `uv run` | `/opt/venv` |
| planted `.venv` sentinel in the mount, after a full run | byte-identical, `find -printf` diff empty |
| bare `python3`, `git`, `git apply` of the B12 fixture | all work on a real clone |
| tmpfs constant reverted to 256m | exactly 1 test dies, 986 pass, none pre-existing |

Host gates: **987 passed**, `ruff check .` clean. `ruff format` not run.

**The 256m double-run result is the justification for the bump and it is the house bug
class.** One run fits; the second fills the tmpfs and returns a large, confident,
entirely wrong test result. A worker iterating on the suite would read it as real.

**Three things to know that the brief did not anticipate:**

1. **The Postgres base image has no system Python at all**, so `python`/`python3`/`pip`
   disappeared — a silent capability regression against `python:3.12-slim` that would have
   broken any model-written `python3 -c ...`. Closed by putting `/opt/venv/bin` on PATH in
   the profile script. It has to be the profile script and not the Dockerfile, because
   `/etc/profile` *resets* PATH for non-root logins and discards the image `ENV`.
2. **`agent-sandbox:local` was rebuilt and re-tagged in place**, so the live image the daemon
   would use is the new one already. Recoverable — the old Dockerfile is in git history — but
   it is a change to the running system that happened as part of an implementation task, and
   it is recorded here rather than glossed.
3. **A git *worktree* is dead inside the container**: its `.git` is a file pointing at a
   gitdir outside the mount. A real clone or the real repo works fine. Only affects how the
   image is verified, not how a worker uses it.

**Corrections to earlier ledger entries, both from this work:** the container's Python is
3.14.7 against the host's 3.14.5 (same minor; uv took the latest patch, pin it if they must
match), and `patch(1)` is absent from the image — immaterial, since the B12 fixture is
applied with `git apply`, which works.

**`-m docker` still selects 0 of 987.** `CLAUDE.md` names it as a gate and nothing has ever
been marked. The four new tests in `tests/test_sandbox_image.py` are static assertions on the
Dockerfile and the profile script, deliberately not docker-marked: a real one would need the
image built and would have to skip cleanly when run *inside* the sandbox, where there is no
docker. Flagged, not built.

**Where the approved order stands now.** Steps 1 and 2 met; the sandbox prerequisite met.
**Step 3 — B11, B12, B13 under `chat`, attended, ~1 hour — is next and it needs Dylan at the
terminal.** Step 4 is the `baseline-v2.md` §2 addendum, step 5 is dispatching 8a.

## The live probe — the whole chain works, and it was interrupted before it finished

Run against a **throwaway clone**, not the working tree: `~/Projects/agent-probe`, with
`AGENT_PATHS__PROJECT` pointed at it, so nothing a worker did could reach Dylan's repo. Real
config, real SIR endpoint, real journal, real sandbox image. `AutoApprover(True)`, because the
point was to exercise `shell_exec` rather than the approval wall. One delegation to `coder`:
*"Run this project's full test suite and report exactly how many tests passed, how many
failed, and the command you used. Do not change any files."*

**Dylan interrupted it partway, and it is not being re-run.** What had already landed is the
part that mattered, read back out of the journal (run `01a0d3ca-c8ca-7172-81b6-67fb31ce19f5`,
23 rows, 14:21:17–14:22:33Z):

| seq | call | result |
|---|---|---|
| 6→8 | `fs_list "/home/dylan/Projects/agent-probe"` | **ok**, 240 chars, 4 ms |
| 10→12 | `fs_read ".../pyproject.toml"` | **ok**, 1913 chars, 2 ms |
| 14→18 | `shell_exec "uv run pytest 2>&1 \| tail -40"` | **ok, `exit=0`**, 3297 chars, **28.3 s** |
| 20 | `shell_exec "uv run pytest ... \| grep -E 'passed\|failed\|error'"` | requested; interrupted here |

**Zero `tool_failed` events in the run.** Against the 2026-09-24 00:38 probe — fourteen tool
calls, every one of them failed — that is the whole pre-8a chain working at once, in the live
path rather than in a harness:

- **fix 1** (`d02a559`): the worker turn runs at all; before it, `subagent:coder` died at step 1
  on the `extra_system` 400 with `answer_chars: 0`.
- **ruling 1** (`91fa9c6`): the model wrote an **absolute path into the project** on its first
  call and it resolved. The previous probe's fourteen failures were relative paths landing in
  the empty workspace plus one out-of-roots guess.
- **fix 2** (`c9aa84f`): the probe's approver reached the worker, so `shell_exec` was approved
  rather than queue-denied. Under the old fallback this row would read `denied`.
- **the sandbox** (`a8639a8`): `uv run pytest` **exited 0 inside the container** on a live
  delegation — the command B12's task text names, run by the model, not by a verification
  script.

**What this probe does not establish**, and it should not be written up as if it did: there is
no `worker_finished`, so nothing exercised the report schema, the result cache or the
`worker_finished` boundary on this run; and "exit=0" is the shell's status, not a graded
answer — the worker never got to state a test count, which is exactly the B13 failure mode
(reasoning about a truncated result) that the real eval row exists to measure.

**Live data written, stated rather than glossed:** journal 1313 → 1336 rows, 58 → 59 runs; one
new `sessions` row; two `effect_intended`/`effect_committed` pairs for the two `shell_exec`
calls. No memory writes, no promotions, no `worker_finished`. The clone at
`~/Projects/agent-probe` has been removed.

## Pass 8 boundary — Dylan's rulings on the four doors, 2026-09-24

### The enforcement fix: a ceiling, not the visible set — its own commit, before the baseline

The orchestrator put the wrong design and **the correction is the substance.** The proposal
was "refuse a registered-but-*unoffered* tool at the executor." Dylan's reading, recorded
close to verbatim:

> "If only door 1 respects the subset, 8a's tool move is still advisory: the orchestrator can
> `tool_search` for `fs_write` (door 3), pick it up from a handoff manifest written by an
> earlier session that used it (door 2), or just name it (door 4). The boundary has to be the
> subset itself, and every door has to respect it."

And the half that inverts the original proposal: **naming a tool that is in the subset but was
not shown this turn is legitimate recovery from a retrieval miss.** `Registry.select` is an
embedding lookup and it misses. Enforcing *offered* would have broken a working behaviour
while leaving three doors open — the worst of both. The boundary is **subset membership**.

Ruled, four parts, all binding:

1. **Doors 1–3 filter against the subset.** `select`, `_with_lookup` and `tool_search`'s
   `added_tools` may only return tools inside it. Door 2 is named specifically: a handoff
   manifest written by an earlier session that *had* a tool must not reintroduce it.
2. **Door 4 stays, but only inside the subset.** Kept and journaled exactly as today
   (`tool_requested` with `visible: false, known: true`). Out-of-subset names are refused.
3. **The executor checks subset membership as the last line**, so a fifth door added later
   **fails closed rather than open**. Defence in depth, not the primary gate.
4. **Nothing is added to `tool_requested`.** *"Don't add a field to `tool_requested` to mark
   'outside surface.' That would be the precedent question again on a type with 258 live
   rows. Put the reason in `tool_failed`'s existing error text."* If a structured field turns
   out to be needed it is `opt()` or a new event type, **and it comes back to him first.**
   This is the required-field ruling of 2026-09-23 being applied by its author, one day later,
   to the first case that tested it.

**Eight tests: one per door, for both the orchestrator and a worker**, each reaching an
out-of-subset tool through that door and asserting refusal, each mutation-checked
individually. His reason for the worker half, so it is not mistaken for redundancy:
*"Workers already have `Registry.subset`, but I don't know that doors 2–4 respect it there
either, and the test settles that."*

**Why it lands before the baseline and does not contaminate it:** all 258 real
`tool_requested` events are `visible=1, known=1`, so no past run could have been affected, and
8a branches from a tree where "offered" is actually enforced. The shipped orchestrator subset
is *everything* — the mechanism and its tests land now, and 8a is what narrows it.

### The other three

**Fix 1 goes in the outcome table.** `d02a559`, so the record shows both pre-8a fixes landed
before the measurement rather than only the one this session made. Done.

**The `act` correction is accepted, and the table is the right place for it.** Dylan will stop
the heartbeat timer for the hour — **for the endpoint-contention reason, not because the
daemon could write.** *"After the restart the 400 goes away, but the heartbeat will still
spend its step budget retrying a denied `open_loop_close` ten times. Log it for after Pass 8."*
Logged in `docs/plans/pass-10-evaluate.md` under 10c, with two neighbours found in the same
evidence: a working heartbeat can never report `completed` (budget 4, `abandoned` at
`steps >= max_steps`), and `repo_agenda.notify(title="Heartbeat failed")` has never fired
because a provider 400 does not raise out of `run_turn`.

**Clone, agreed — and 8d must match.** His sequence: stop the timer; `git clone` at HEAD and
record the sha; point `[paths] project` at the clone; restart the daemon; run the delegation
probe to completion; run B11–B13 under `chat`, resetting within the clone; record as the
`baseline-v2.md` §2 addendum **with the sha and "run against a clone"**; then restore
`project`, restart the daemon, restart the timer. **"8a's closing comparison should run the
same way, against a clone at 8a's head, so both sides of the comparison are measured
identically"** — now in the pass file under 8d, with the verified warning that it must be a
clone and not a `git worktree` (a worktree's `.git` is a file pointing outside the mount, so
`git` is dead inside the container).

### Also done at this boundary

`agent-sandbox:pre-8` now tags `b65b2239cbca`, the 270MB pre-8 build, which was still on disk
as a dangling image. Rollback is `docker tag agent-sandbox:pre-8 agent-sandbox:local`, not a
rebuild from git history. The 256m/512m reproduction is recorded in `pass-08-outcome.md` as
the reason for the setting, and the pass file now carries the offered-set number 20 with both
numbers required in 8d's table.

## The enforcement fix landed — `2d92aa2`, 2026-09-24

All four doors filter against the subset, and `AgentLoop.tool_subset` is a **property rather
than a snapshot**. The shipped orchestrator narrows nothing; 8a is what narrows it.

**It went in wrong first, and that is the part worth recording.** The first implementation
snapshotted `set(self.registry.tools)` in `__init__`, and its comment claimed that was
"today's behaviour exactly". It was not: several callers and several tests do
`loop.registry.add(probe)` *after* constructing the loop, and the snapshot locked those tools
out of their own turn. **Two pre-existing tests caught it immediately** —
`test_a_tool_sees_the_approver_the_loop_was_built_with` and
`test_the_manifest_covers_the_tool_output_of_the_turn_that_handed_off`. That is the opposite
of every other defect in this pass, where the answer to "how many pre-existing tests died" was
zero, and it is why the property shape was found before the commit rather than after it. The
same two are now the regression guard for it.

**Mutations re-run by the orchestrator, not taken on report.** Verified directly, suite of 998:

| mutation | new tests killed | pre-existing killed |
|---|---|---|
| door 1 — drop `permitted` from `select` | orchestrator doors 1 and 2 | **0** |
| door 2 — drop the `_with_lookup` guard | orchestrator door 2 | **0** |
| door 3 — `tool_search` ignores the subset | **both** door-3 tests, orchestrator and worker | **0** |
| door 4 — the re-add ignores the subset | orchestrator door 4 | **0** |
| the executor's last line disabled | 3 tests | **0** |
| `None` enforces an empty subset | 1 | **20** |
| the subset snapshotted in `__init__` | 0 | **2** (exactly the pair above) |

**The 20 is the useful number.** Making `None` mean "enforce nothing permitted" kills twenty
pre-existing tests across `test_effect_ledger`, `test_cold_resume`, `test_mcp_client` and
`test_tools_and_daemon` — so `policy/replay.execute_approved` and the MCP callers, which have
no turn and no surface, are genuinely covered rather than merely asserted to be fine.

**Worker doors 1, 2 and 4 turn out to be closed by construction**, and the mutation says by
what: `restricted = Registry(tools=registry.subset(...))` at `agent/subagents.py:211`. Door 2
additionally cannot be reached through `run_subagent` at all, because it builds the worker a
fresh `Session` whose `handoff` is always `None`; that test drives a worker-shaped loop
directly. **Worker door 3 was genuinely open** — `tool_search`'s closure is over the *process*
registry, so a worker was being told it "now had" tools from the whole machine. Dylan's
instruction to test the worker half rather than assume it is what found that.

**Three corrections to how the doors were described, all of which matter to 8a:**

1. **Door 4's re-add does not control what the model is shown.** `tool_schemas` is a separate
   list, appended to only by the `tool_search` reveal. The re-add touches `exposed` only, whose
   effects are the `visible` flag on a *later* call to the same name, and the STUCK withdrawal
   path. A first version of the door-4 test asserted "not offered on the next step" and the
   mutation **survived it**; it now asserts the `visible` flag stays `false` across two calls.
   **8a reads that rate — it should know it is measuring the flag, not the schema list.**
2. **`select` short-circuits when the permitted pool is at or below `ALWAYS_EXPOSE_LIMIT` (20).**
   A narrow surface is therefore offered whole and door 1's filter only bites above it. Since
   8a's target is 8–12 tools, **door 1's filter will be inert at the target surface** and the
   other three doors are what enforce it.
3. **For a directly-named out-of-surface tool the executor is the only thing that refuses
   execution**; the loop guard only stops the name being adopted into `exposed`.

Suite 987 → 998, ruff clean, nothing staged that was not this change, no test file edited.

## Baseline environment armed — 2026-09-24 11:41 EDT

Steps 1–4 of Dylan's sequence, run on his instruction. **Everything here is temporary and has
to be undone at step 8; the restore values are written into the config file itself, beside
each change.**

**1. The heartbeat is silenced, in config rather than by stopping the daemon.**
`[daemon] quiet_hours = [23, 8]` → `[0, 24]`. `in_quiet_hours` reads
`start <= hour < end` when `start <= end`, so `[0, 24]` is true at every hour and
`heartbeat_loop` hits its `continue` before `situation_report` — **no model call at all**,
which is the point, since the reason for stopping it is contention on the shared endpoint and
not any risk of it writing. Confirmed against the loaded config: `quiet now? True`.

**Checked before touching it, because the key is shared:** `daemon/notifier.py:81-83` falls
back to `cfg.daemon.quiet_hours` **only when `cfg.ntfy.quiet_hours is None`**, and the live
config sets `[ntfy] quiet_hours = [0, 0]`. So push is unaffected and keeps its always-on
window. Had `[ntfy]` been unset, this edit would have silenced notifications too.

**2. The clone, and the sha.**

```
~/Projects/agent-baseline   a9036c4465bf3b7d91642b2274fd1f85262f510c
```

`git clone` of the working checkout, then `git checkout main`, which is the same commit — on
a branch rather than detached, so the rows can reset with `git checkout` and read normally in
`git status`. **A clone and not a `git worktree`**, per the verified finding: a worktree's
`.git` is a file pointing at a gitdir outside the container mount, so `git` dies inside the
sandbox. Confirmed here: `.git` is a directory, and `evals/fixtures/b12-mutation.patch` is
present for B12's run condition. No `.venv`, which is correct — the sandbox uses `/opt/venv`.

The working checkout is at the same sha, so the only differences are the untracked files
(`CLAUDE.md`, the three `docs/plans` prompt files, `repo-drop(1).zip`) and whatever Dylan
edits next. **That is the whole reason for the clone.**

**3. `[paths] project` points at the clone.** `project = "~/Projects/agent-baseline"`, with
the old line kept commented directly above it. Verified through the loaded config rather than
by reading the file back: `cfg.paths.project_root` and `builtin_shell.mount_source()` both
resolve to the clone, and `project_block` renders the clone's path in the worker's role
prompt. **So the read-write sandbox mount is now on the clone, not on Dylan's tree.**

**4. Daemon restarted.** `systemctl --user restart agent-daemon.service`, active since
11:41:24 EDT, pid 95082, and `daemon_status` carries that pid with `{'status': 'running'}` at
15:41:25Z. The config file's mtime is 11:41:13 and the process started 11:41:24, so **the
restart is after the edit** and the running daemon has both changes — which is the thing a
`systemctl status` alone would not tell you, since an editable install reloads code but never
config.

**Owed at step 8, and the session that ends without doing it leaves the system wrong:**
restore `[daemon] quiet_hours = [23, 8]`, restore `project = "~/Projects/agent"`, restart the
daemon, and confirm the next heartbeat completes. A pre-edit copy of the whole file is in this
session's scratchpad as `config.toml.pre-baseline`.

## Pass 8a ran — `e6a3b83`, 2026-09-24

The full write-up is `docs/records/pass-08-outcome.md`. What belongs here is the order of
operations and the things that went wrong in the measurement rather than in the system.

**The code landed before the measurement, and the clone was taken at its head.** One commit,
`e6a3b83`, then `git clone` to `~/Projects/agent-8a` at that sha, `[paths] project` pointed at
it, `telemetry.jsonl` rotated to `telemetry-20260924-baseline-coding.jsonl` so the pre-8a rows
stay a separate file, `agent tools sync` (8 embeddings recomputed — `delegate`'s description
changed with the surface), daemon restarted. Same recording approver, same `runrow.py` as the
pre-8a rows, copied rather than rewritten.

**Result: 3 pass / 1 partial / 2 fail → 6 pass, at ×7.1 wall clock.** B10 partial→pass,
B11 fail→pass, B21 fail→pass; B02, B12, B13 hold. Three of the six were verified first-hand
rather than graded off the agent's own sentence, which is how B11's shipped truthiness bug
(`older_than_days=0` silently disables the filter) was found inside a passing row.

**Two incidents, both mine and both in the record.** A `pgrep` came back empty while the first
B11 was still alive; this session read that as "killed" and started a second, so ~40 s of B11
overlapped two runs on one endpoint — B11's latency is the weakest of the six numbers. Killing
the duplicate left run `01a0d498` with a `worker_created` and no `worker_finished`: a real
crash-shaped run in the live journal, not folded, not resumed.

**The measurement wrote durable false beliefs, and the cleanup is owed.** The review gate
promoted five worker/consolidator candidates into `facts`; four name `agent-8a`, a throwaway
clone, and one **superseded** the true fact `01a0c4d5` *"Dylan is building a personal agent
runtime this quarter."* from 2026-09-21. The delete was refused by the sandbox's mass-delete
classifier and was **not** worked around — the exact SQL, including restoring `01a0c4d5`, is in
the outcome record under "the memory store was polluted by the measurement". The better fix for
8b onwards is to run the suite with promotion off rather than clean up after it.

**Step 8 of Dylan's baseline sequence is done.** `~/.config/agent/config.toml` is byte-identical
to the pre-baseline copy (`diff` clean): `project = "~/Projects/agent"`, `quiet_hours = [23, 8]`,
daemon restarted 14:55:48 EDT after the edit. The clone `~/Projects/agent-8a` is left in place at
`e6a3b83` with a clean tree; `~/Projects/agent-baseline` at `a9036c4` is also still there. Both
are the measurement surfaces the two addenda name, and neither is referenced by the running
system any more.

**`~/Documents/agent-db-dependency.md` is B21's artefact and was left on disk**, 3 674 bytes.
It is the file 8d was meant to diff between runs, and it is the first run in which it exists at
the path the answer claims.
