# Interlude, 2026-09-28 — what session `01a0e9f6` broke, and what was changed for it

**Not a pass.** Dylan waived the pass-file rule for this one ("wave the pass file rule and
implement it now") after reading the diagnosis of Telegram session
`01a0e9f6-879c-7fec-92d2-64e74c7365f6`. Filed here so the next pass can load it as a
dependency; nothing in `docs/plans/` names it.

## What the session showed

Five turns over Telegram, one `coder` worker. Read from a copy of the journal (with the
`-wal`) and from `raw_events` 163298–163349.

| turn | what happened | what the code did about it |
|---|---|---|
| 2 | `delegate {}` — rejected for missing `agent` and `task`; retried correctly on the next step | known 27B shape ([[agentd-27b-tool-fidelity]]). The stream carried no argument bytes and `openai_compat` substituted `{}`, so the journal could not say whether the model omitted them or the parser dropped them |
| 2 | the coder's `shell_exec` and `fs_write` were **queued**, not denied, under `risk_matrix:write/assist`; the model was told *"nobody is at the keyboard"* | `daemon/telegram.py` built a `QueueApprover`. The person was reading the reply on their phone. Neither queued id reached them |
| 2 | `fs_read` → `Not a file` | true, and misleading: the file did not exist |
| 3 | asked "why did delegate fail first?", the model said it had **never called any tool**, confessed to a fabrication it had not committed, then in the same zero-call turn wrote *"Done. The file contains `hello world`"* | `recent_messages` replayed `user_message`/`assistant_message` only. Every `tool_result` was archived and none was ever shown again |
| 4 | repeated the false confession | same cause |

Two fabrications in a row from one gap, and both read as honest self-correction.

## What changed

**A. Tool calls are replayed into the next turn's history.**
`repo_archive.recent_tool_results(session_id, turn_ids, after_id)` joins on the
*orchestrator's* turn ids — a worker's rows carry the worker's `turn_id`, so the coder's
`shell_exec` stays out while the `delegate` result that reported it comes back.
`context.history_messages` now emits, per turn, one `assistant` message carrying every call
of the turn and one `tool` message per result (clipped to `HISTORY_TOOL_RESULT_CHARS = 600`),
immediately before the turn's answer, **as one budget unit** — a cut keeps calls and answer
together or drops both. `tool_result.payload.call_id` is recorded going forward; older rows
get an id minted from the archive row. `budget.carried_messages` counts `tool` messages at
their clipped size, so the handoff decision still measures what carries.

Verified against the live archive, read-only: turn 3 of `01a0e9f6` is now preceded by
`delegate {}` → *Invalid arguments…* and `delegate {task…}` → *blocked…*, and the worker's
three calls are absent.

**B. Telegram asks with buttons and waits.** `TelegramApprover` queues the row first, sends
the preview with Approve/Deny inline buttons (naming the worker when a worker asked), and
awaits the tap for `[telegram] approval_timeout_s` (default 180). A tap resolves the row and
the turn carries on with the real result. No tap leaves the row `pending` with the model told
*"the user was asked on Telegram and did not answer … `/approve <id>` runs it"* — the executor
now relays the approver's `note` instead of the keyboard line. A tap after the wait (daemon
restarted, timeout passed) acts on the queued row through `execute_approved`/`deny_approval`,
so the buttons never go dead. Typing `/approve <id>` while a turn waits is the tap and runs
it once. The poll loop no longer awaits a turn inline (it would be waiting for itself); turns
are tasks, one at a time per chat, and commands skip that lock. `allowed_updates` gained
`callback_query`, with the same allowlist check as a message.

Unchanged: `agent ask` and every daemon path still build a `QueueApprover`. `coder` is
still capped at `act`, where `write` is still `require_approval`. Nothing was loosened.

**C. Guards.**
- `answer_flagged` (vocabulary 25 → 26): a turn that was offered tools, called none, and
  answers with a completed-action claim (`claims_action`: "Done.", "I've created", "has been
  written", …) gets `NO_ACTION_NOTE` appended to its answer — worded to be true on a false
  positive — and the event in the journal with the matched phrase.
- `tool_requested.args_empty_stream` (optional, only when true) from `ToolCall.arguments_missing`.
- `fs_read`: `No such file` vs `Not a file (it is a directory)`.
- `WorkerResult.queued_approvals`, off the ledger, in the `for_orchestrator` door.
- `main.md`: quote the queued id; never describe an action as done without a tool result.

## Numbers

Suite 1033 → **1104**, `ruff check .` clean. One existing test rewritten
(`test_the_two_readings_answer_different_questions`: tool results now carry, clipped) and
the vocabulary pin updated. Daemon restarted 2026-09-28 after the change.

## Also found: push had been dead for eight days

Dylan's second question in the same session ("my notifications are also not sending
timely") was not a latency problem. `notifications` held 303 rows all-time, 35 ever pushed,
none since 2026-09-20 15:54 UTC; 257 of the last seven days' rows had `push_attempts = 6`
and `pushed_at IS NULL`. `actions` held 1 608 `push/ntfy` errors, all `All connection
attempts failed`. Cause: `agentd-ntfy-1` was `Up 3 days` with `NetworkSettings.Networks = {}`
and no published ports — a container restored across the 2026-09-24 reboots without its
endpoint — so nothing listened on 8088. `docker compose up -d ntfy` recreated it; health
200, the `everyone agent rw` grant intact in the volume, a test push accepted. The backlog
is not replayed (`_unpushed` skips rows at the cap), which is the right default and means
those 257 stay unread in `agent inbox`. Twelve further errors on 09-20 were
`'ascii' codec can't encode character '—'`: a Unicode title cannot cross the `Title`
header.

## Still open

- `01a0e9f6`'s two approvals are still `pending` (expire 2026-09-29 21:43 UTC); the
  `shell_exec` one cannot succeed even if approved — it writes outside the mount.
- The approvals row a tap approves ends at `approved`, not `executed`: the live turn runs
  the call and the approver never sees the result.
- The agent still believes the project lives at `~/Projects/agent-9d` (turn 1's answer) —
  eval-clone pollution, [[agentd-eval-writes-durable-beliefs]].
- Whether `args_empty_stream` fires on the 27B's `delegate {}` is a measurement for the
  next eval run, not a thing this session established.
