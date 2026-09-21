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
one. Session **3d** (writes and integrations) ruled on the remaining six. The one
deferral, `memory_search`, was settled by Dylan at the **Pass 3/4 boundary** — `read`, as
a deliberate exception, recorded in the 3c table below. Every registered tool now carries
a ruling and `UNAUDITED_TOOLS` is empty.
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

## Deferred to a human, and settled at the Pass 3/4 boundary

| tool | class | why it was deferred, and how it was ruled |
|---|---|---|
| `memory_search` | read | **Ruled by Dylan at the Pass 3/4 boundary, as a deliberate exception rather than a clean fit.** `retrieval.pack` ends in `repo_memory.touch_accessed`, which runs `access_count = access_count + 1` on every fact it returned: that counter is non-idempotent and does not converge on replay, so this does **not** strictly satisfy `read`. It is classified `read` because the drift is bookkeeping rather than state the system's correctness depends on, and because escalating the runtime's most-called tool would make resume prompt on memory lookups — which trains the user to blind-confirm and destroys the value of `uncertain` for the cases that matter. **Checked before the ruling was committed:** `access_count` and `last_accessed_at` are written at `repo_memory.py:286` and read nowhere in `src/`; the retrieval score is `0.60·rrf + 0.15·recency + 0.10·importance + 0.10·confidence + 0.05·prior` (`retrieval.py:710`) with no access term, and `recency` reads `valid_from`/`recorded_at`/`created_at`, never `last_accessed_at`. Had the counter fed ranking the answer would have been `idempotent_write` with a counter reset on reconcile. **Rejected alternative: `unsafe_write`**, the conservative value it held through 3c and 3d. |

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

## Session 3d — writes and integrations

| tool | class | why |
|---|---|---|
| `fs_write` | unsafe_write | `append` duplicates the content and `create` fails the second time; even `overwrite` does not converge, because the pre-write backup that makes the change undoable is retaken on replay over the first attempt's output, so the undo then restores the wrong file. |
| `shell_exec` | unsafe_write | An arbitrary command line the model wrote. `/workspace` is mounted rw and outlives the container, and `network=true` reaches anything the host can. |
| `gmail_search` | read | `GET users.messages.list` then one `GET users.messages.get?format=metadata` per hit, under `gmail.readonly`. Google refuses any mutation with that token, and a `get` does not clear UNREAD — only a labels modify does. |
| `gmail_message` | read | One `GET users.messages.get?format=full`, same read-only scope. Nothing is marked, moved or downloaded. |
| `web_search` | read | One query against DDGS's fixed search endpoint; the model supplies the query, not a URL. A repeat spends quota and changes nothing. |
| `web_fetch` | unsafe_write | **Against 3c's expectation.** A GET is only conventionally safe: unsubscribe links, confirmation links and GET-shaped API endpoints all act, the far end decides which, and the URL is chosen by the model — often from untrusted text this same tool returned. |

### What was checked before classifying anything as idempotent, and the result

The instruction for this session was to establish, for each integration, whether the API
accepts a **client-supplied id** before anything is called `idempotent_write`.
**Nothing in 3d was classified `idempotent_write`**, so no such claim rests on an id. What
was checked, all of it by reading this repo's client code and the providers' documented
contracts — no live API was called, no mail sent, no remote state touched:

| integration | reached by | what the call is | client-supplied id? |
|---|---|---|---|
| Gmail API | `gmail_search`, `gmail_message` | `users.messages.list` / `.get`, both `GET`, via `builtin_mail.fetch` → `google_auth.get_json` | Not applicable — nothing is created. The scope is `google_auth.GMAIL_SCOPE = .../auth/gmail.readonly`, so no mutating call is reachable at all. |
| Google OAuth token endpoint | every Gmail call, indirectly | `POST` refresh_token grant in `google_auth.access_token` | No id, and none needed: it mints a short-lived access token into an in-process cache (`_tokens`), writes nothing to the vault, and re-running converges on "a valid token". |
| Google Calendar API | **nobody** | — | Not reached by any tool. `calendar_upcoming` is a `SELECT` over the `raw_events` archive the daemon fills, and the scope is `CALENDAR_SCOPE = .../auth/calendar.readonly`. |
| DDGS | `web_search` | scraped search query | No account, no id, nothing created. |
| arbitrary HTTP | `web_fetch` | `GET`, up to 5 manually re-checked redirect hops | **No contract of any kind** — the far end is whoever the URL names. That absence is the whole argument for `unsafe_write`. |
| Docker | `shell_exec` | `docker run --rm --name agent-sbx-<uuid7 hex>` | The container name *is* client-supplied, but it is freshly random per attempt by design, so it is a handle for `docker kill`, not a dedup key. |
| local filesystem | `fs_write` | `write_text` / `open("a")` | No id concept. The path is not one: the same path written twice is two writes. |

Two calls the pass file names as "worth arguing about" have nothing to argue about here:
**`gmail send` does not exist** in this registry (no tool sends mail, and the scope could
not), and neither does **`calendar create`**. Recorded rather than silently skipped, because
the next reader will look for those rows. For whoever adds them: Gmail's
`users.messages.send` takes no client idempotency key — the id in the response is
server-assigned and a repeated send is a second mail, which is why the pass says
`unsafe_write` always — whereas Google Calendar's `events.insert` *does* accept a
client-generated `id`, and that is the one thing that could make a calendar create
`idempotent_write`. Neither was tested against a live API, and neither should be.

### The uncertain calls in 3d, with the reasoning that made them uncertain

- **`web_fetch` is the session's one reversal**, and the least comfortable row in the table.
  3c expected `read`; the ruling is `unsafe_write`. The argument for `read` is strong and
  was rejected: this tool cannot write, `GET` is defined as safe in RFC 9110, and a replayed
  page fetch is normally nothing. The argument that won is that "safe" in that spec is a
  property the *server* promises and routinely breaks, that this tool has no way to tell a
  wiki page from a confirmation link, and that the URL arrives from the model, which may
  have read it out of somebody else's email. The pass's own asymmetry then decides it: a
  wrong `unsafe_write` costs one prompt, a wrong `read` silently replays somebody's
  one-click action. **Rejected alternative: `read`.** The cost being accepted is four
  fsyncs per fetch and a Pass 4 prompt on a common tool; the cheap fix, if it hurts, is an
  argument-aware class (Pass 8) or a per-host allowlist, not a downgrade of the tool.
- **The two Gmail tools were ruled `read` although they leave the machine and touch the
  user's private data.** Neither of those makes a call re-executable or not. What decided
  it is the scope, which is read-only and enforced at Google rather than here. The rejected
  alternative was `unsafe_write` on the grounds that mail is private and a replay re-exposes
  it — but a replay re-exposes it to the same agent in the same run, and `private_output`
  raises `session.private` either way. **If a future tool asks for `gmail.modify` or
  `gmail.send`, these rulings do not extend to it**;
  `test_the_gmail_tools_are_read_because_the_scope_makes_them_read` fails if the scope
  constant changes, which is the only thing holding the two together.
- **`fs_write` was ruled at the tool's worst case, not per argument.** `overwrite` alone
  might have been `idempotent_write`; the backup-on-replay problem above means it is not,
  and even if it were, one field cannot say "except in this mode". Rejected alternative:
  splitting the tool, which is Pass 8's and explicitly not this pass's (*Must not*: do not
  change which tools exist).
- **`shell_exec` with `network=false` was considered and rejected as a separate case.** The
  workspace mount is rw in both, so "no network" is not "no effect".

## What this table costs when it is wrong

A tool wrongly called `read` gets no ledger row, so a crash mid-call leaves no record that
it may have happened and Pass 4 will re-run it silently — that is the duplicate action
nobody can take back. A tool wrongly called `unsafe_write` costs four fsyncs per call and
one avoidable prompt. The asymmetry is why every uncertain row above stayed conservative.
