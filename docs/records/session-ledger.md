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
| 4c | pending | — | — | **hard stop — human runs this** (user-facing wording) |
| 4d | pending | — | — | |
| 5a | pending | — | — | |
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
