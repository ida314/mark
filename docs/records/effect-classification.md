# Effect classification — the audit table

One row per registered tool, the class it declares, and the one line that justifies it.
The question every row answers is **not** "is this dangerous" — that is `risk`, which
gates approval — but "if a crash left this call unresolved, may the runtime simply run it
again?"

```
read              no external state change; free to re-execute
idempotent_write  re-execution converges to the same state
unsafe_write      re-execution may duplicate a real-world action
unaudited         no ruling recorded; declares unsafe_write until there is one
```

Session **3c** (reads and local-state tools) ruled on 19 of the 26 builtins and deferred
one. Session **3d** (writes and integrations) owns the six still marked `unaudited`.
`tests/test_tool_effect_class.py::test_every_tool_in_the_classification_table_declares_what_the_table_says`
holds this file and the declarations in the source to each other, so a class changed in
one place and not the other fails the suite rather than going quietly stale.

---

## Session 3c — reads and local-state tools

| tool | class | why |
|---|---|---|
| `time_now` | read | Formats the clock. Touches nothing. |
| `fs_read` | read | Opens one file for reading and stops at 400 kB. |
| `fs_list` | read | `iterdir` plus `stat` on one directory. |
| `fs_search` | read | Runs `rg`/`grep` by argv, never through a shell; neither binary can write. |
| `goals_list` | read | One `SELECT` over `goals`. |
| `open_loops_list` | read | `SELECT` over `open_loops`, plus connector state to say how fresh the feeds are. |
| `memory_history` | read | `SELECT` over `facts` for one subject, superseded rows included. |
| `profile_read` | read | Reads markdown files out of the memory repo; no git command runs. |
| `calendar_upcoming` | read | `SELECT` over the `raw_events` archive the daemon fills. Google is never called. |
| `coursework_due` | read | The same `SELECT` shape against the Brightspace rows. No D2L call. |
| `tool_search` | read | `SELECT` over `tools` plus an embedding call that stores nothing; the tools it adds live in `ctx.extra` and die with the turn. |
| `goal_upsert` | idempotent_write | `INSERT ... ON CONFLICT (slug) DO UPDATE`, and `slug` defaults to `slugify(title)`, so the same arguments always land on the same row. |
| `open_loop_close` | idempotent_write | `UPDATE open_loops SET status='closed' WHERE id`; closing a closed loop is a no-op. Only `closed_at` moves. |
| `open_loop_add` | unsafe_write | Fresh `uuid7` per call, no dedup key: a replay opens a second loop the user has to close twice. |
| `reminder_set` | unsafe_write | Inserts a watcher that fires a notification at the user. A replay is a second reminder, and a reminder that has gone out cannot be recalled. |
| `watcher_add` | unsafe_write | A replay is a second watcher; an `action_type="agent"` watcher wakes the agent to do arbitrary work on a schedule. |
| `notify_user` | unsafe_write | The row it writes is what the daemon pushes to chat and inbox within a minute. "Queued" is not "not yet real". |
| `memory_remember` | unsafe_write | `insert_candidate` has no dedup key, so a replay queues the claim twice; with a correction cue it reaches `review.propose_and_review`, which writes canonical memory. |
| `delegate` | unsafe_write | Runs a sub-agent that may call anything, including `fs_write` and the shell. Replaying the delegation replays whatever it chose to do. |

## Deferred, with the reasoning that made it uncertain

| tool | class | why it is not ruled on |
|---|---|---|
| `memory_search` | unaudited | Examined, not settled. `retrieval.pack` ends in `repo_memory.touch_accessed`, which runs `access_count = access_count + 1` on every fact it returned, so re-execution does **not** converge and the tool is not literally `read`. But the state it moves is bookkeeping *about accesses*, and a replay genuinely is another access; nothing outside the machine changes. Calling it `read` is a judgement about what counts as state. Calling it `unsafe_write` — the conservative option, and the one left standing — puts a ledger row, two journal events and four fsyncs on the most-called tool in the runtime, and will make Pass 4 ask the user to confirm a search. **Rejected alternative: `read`.** A human should pick; if the answer is `read`, the counter is the only thing that argues otherwise and it is arguably right either way. |

Also flagged, though they were ruled on:

- **`fs_search` spawns a process** and was still called `read`. The rejected alternative was
  to leave it unaudited on the grounds that it executes something. It takes a pattern and a
  path, not a command line; `create_subprocess_exec` means no shell; neither `rg` nor `grep`
  has a write mode reachable from those arguments.
- **`calendar_upcoming` and `coursework_due` are named under session 3d's heading**
  ("filesystem, shell, email, calendar, or any external API") but both are pure `SELECT`s
  over the archive the daemon already filled — the module docstrings say so at length, and
  no credential is touched. They are read-only local state, so they were ruled on here. If
  3d disagrees it can move them; the class would not change.
- **`goal_upsert` is the only `idempotent_write` that depends on an argument default.**
  If a caller ever passes a `slug` that varies per attempt, or if `slugify` stops being
  deterministic, the class becomes a lie. Nothing enforces that today.
- **`delegate` is double-counted on purpose.** The sub-agent's own calls each get their own
  ledger row through the executor, so a replayed delegation appears once as itself and again
  as everything underneath it. That is the safe direction, but Pass 4 should expect it.

## Session 3d — not yet ruled on

| tool | class | expected, for 3d to confirm or reject |
|---|---|---|
| `fs_write` | unaudited | Almost certainly `unsafe_write`; `mode="append"` duplicates content on replay, and one class per tool cannot say "except in overwrite mode" (pass-03 outcome, 3a open question 3). |
| `shell_exec` | unaudited | `unsafe_write`. Arbitrary commands cannot be reasoned about generically. |
| `gmail_search` | unaudited | Expected `read` — it is a `GET` against the Gmail API and changes no mailbox state — but it leaves the machine, so 3d rules. |
| `gmail_message` | unaudited | Same: `GET` one message, `format=full`. Does not mark anything read. Expected `read`, 3d rules. |
| `web_fetch` | unaudited | Expected `read`, with the caveat that a `GET` at somebody else's URL can be an action at the far end. |
| `web_search` | unaudited | Expected `read`; a query against DDGS changes nothing but spends quota. |

`mcp:<server>/<tool>` is `unsafe_write` at wrap time and is not in this table: the class
cannot be known at registration, and `read_only_hint` is the server's claim about itself
(pass-03 outcome, 3a deviation 3).

## What this table costs when it is wrong

A tool wrongly called `read` gets no ledger row, so a crash mid-call leaves no record that
it may have happened and Pass 4 will re-run it silently — that is the duplicate action
nobody can take back. A tool wrongly called `unsafe_write` costs four fsyncs per call and
one avoidable prompt. The asymmetry is why every uncertain row above stayed conservative.
