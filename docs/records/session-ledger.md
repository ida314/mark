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
| 5b | pending | — | — | |
| 5c | pending | — | — | |
| 6a | pending | — | — | |
| 6b | pending | — | — | |
| 6c | pending | — | — | |
| 7a | pending | — | — | **hard stop — human runs this** (harness inspection) |
| 7b | pending | — | — | |
| 7c | pending | — | — | |
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
