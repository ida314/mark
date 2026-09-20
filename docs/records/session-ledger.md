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
| 3c | pending | — | — | **hard stop — human runs this** (effect classification) |
| 3d | pending | — | — | **hard stop — human runs this** (effect classification) |
| 4a | pending | — | — | |
| 4b | pending | — | — | |
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
