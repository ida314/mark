# agentd — a persistent personal agent runtime

Not a chatbot. A long-lived agent that runs on your own hardware, remembers your life,
delegates work to ephemeral sub-agents, reaches tools only through a deterministic policy
engine, and wakes on events instead of thinking continuously.

The governing principle: **models are replaceable; your memory, goals, policies, tools,
history and learned procedures are durable.**

## What is here

```
CLI (agent chat)  ──┐                      ┌── MCP server "agent-core" → other harnesses
                    ├─ AgentLoop → Executor ─ Policy engine → tools
Daemon (watchers) ──┘        │                     │
                             └ Retrieval          └ Approvals (prompt | queue)
Postgres+pgvector ── raw archive (append-only) · facts (bitemporal) · episodes ·
                     candidates · procedures · goals/loops/watchers · approvals · audit
Git memory repo  ── profile/ goals/ projects/ relationships/ skills/
OTel → :4317 → Jaeger / Prometheus / Grafana
```

Two processes, no RPC between them: `agent chat` runs a turn in-process, `agent daemon run`
does background work, and they meet in Postgres (plus `LISTEN/NOTIFY`). A daemon crash can
never break your conversation.

### The invariants worth knowing

- **One chokepoint.** Every tool call — from the main agent, a sub-agent, an approved queue
  item or an MCP client — goes through `tools/executor.py`. Validation, policy, approval and
  the audit row cannot be bypassed.
- **One writer of memory.** Sub-agents and tools never write canonical memory. They propose
  candidates with evidence and confidence; `memory/review.py` decides.
- **Nothing is overwritten.** `raw_events`, `actions` and `fact_evidence` are append-only,
  enforced by database triggers. Facts are *superseded* (the world changed, closing
  `valid_to`) or *retracted* (we were wrong, leaving `valid_to` alone) — never edited.
- **Untrusted by default.** Web pages, sandbox output, sub-agent findings and MCP servers
  arrive wrapped in `<untrusted_content>`. That taints the turn, which escalates any later
  write to needing approval, and untrusted sources can never change identity-level facts.

## Setup

```bash
uv sync
ln -sfn "$PWD/.venv/bin/agent" ~/.local/bin/agent   # or activate the venv, or use `uv run agent`
agent init                      # config + data dirs + git memory repo
agent db up && agent db migrate # postgres+pgvector on 127.0.0.1:55432
agent sandbox build             # container image for shell_exec
agent doctor                    # checks everything, including that tool calling really works
```

`uv sync` installs the `agent` entry point inside `.venv`, which is not on your PATH — hence
the symlink. It carries an absolute shebang, so it works from any directory with no
activation.

`agent doctor` is the one to trust: it probes the model endpoint with a real tool call,
because an endpoint that silently drops `tool_calls` looks healthy and breaks everything.

## Daily use

```bash
agent chat                      # the REPL; /help lists slash commands
agent chat --autonomy act       # workspace writes and offline sandbox runs stop asking
agent ask "one-shot question"
agent remember "I prefer terse answers"
agent memory search "where do I live" --as-of 2026-05-01
agent memory history Dylan      # how belief changed, including former beliefs
agent goals list && agent loops list
agent remind "stretch" --in 20m
agent approvals list            # what the daemon queued while you were away
agent trace <turn-id>           # why did this happen
agent undo <action-id>
```

### Autonomy

| Level | What happens without asking |
|---|---|
| `observe` | Reads only |
| `assist` (default) | Reads and drafts; everything else asks |
| `act` | Also workspace writes and no-network sandbox commands |

Edit `~/.config/agent/policy.yaml` to change the rules; `agent policy explain fs_write
'{"path":"..."}' --autonomy act` tells you exactly what would happen and which rule decided.

## Memory

Five layers, one archive. Everything said or done lands in `raw_events`. When a session
goes idle the consolidator extracts an episode plus candidate facts, goals, loops and
procedures, each citing the event it came from. The review gate merges duplicates, judges
conflicts, supersedes what changed and rejects what has no evidence. Overnight, stable
high-confidence facts are promoted into the git-backed markdown repo at
`~/.local/share/agent/memory`, where you can read, diff and revert them.

Text outside the `agent:generated` markers in those files is yours and is never rewritten.
`agent/instructions.md` is yours alone and goes into the system prompt every turn.

Retrieval stays small on purpose: keyword, vector and entity channels fused with RRF, scored
with recency/importance/confidence priors, reranked, stripped of contradictions and
duplicates, then packed into a token budget.

Deep retrieval (`agent memory search --deep`, or the model asking for it) first rewrites the
question into a couple of search variants, because the words you use to ask are rarely the
words that were stored — "that thing I'm building with the GPU box" has nothing in common
with "Dylan runs a DGX Spark homelab". Variants run the same channels and fuse into the same
RRF, discounted so a guess never outweighs what you actually typed. It is a small
non-thinking call on a timeout: if the model is slow or down, you get ordinary retrieval
rather than a stall.

## Background work

`agent daemon run` (or `agent daemon install-unit` for systemd) runs watchers, reminders,
file watching over the memory repo, idle and nightly consolidation, and a heartbeat. The
heartbeat builds a deterministic situation report first and only spends a model call when
something actually changed and is actionable — the infrastructure is always on, the model
is not.

## MCP, in both directions

`agent mcp serve` exposes this agent's memory, goals and loops to other harnesses — that is
the seam OpenClaw plugs into. The reverse is configuration only: list a server under
`[mcp.servers.<name>]` in `config.toml` and its tools are imported at startup as
`mcp_<server>_<tool>`, with `agent mcp tools` to see what arrived.

Imported tools are foreign code, and the runtime treats them that way. They carry
`source="mcp:<server>"`, which the shipped policy routes to approval at *every* autonomy
level, and their output is wrapped as untrusted unless you set `trust_output = true` for a
server you control. A server's own `readOnlyHint` is ignored unless you set
`trust_annotations = true` — a server should not get to grade its own homework.

## Notifications, and credentials

`agent daemon run` pushes every notification to [ntfy](https://ntfy.sh) as well as to any open
chat, so a reminder that fires while no terminal is open still reaches you. The server is in
`compose.yaml`, bound to this box's Tailscale address — your phone reaches it over the tailnet
and nothing about a notification leaves hardware you own. Set `[ntfy] enabled = true` once the
server is up; it ships disabled, because a notifier pointed at nothing just accumulates failed
attempts. Quiet hours hold `info` and `warn` and flush them in the morning rather than dropping
them, and delivery state lives in the database, so a notification raised while the daemon was
restarting is delivered when it comes back rather than lost.

Credentials go in `~/.config/agent/secrets.toml` via `agent secrets set <ref> <field>`, which
reads from stdin if you leave the value off, keeping it out of your shell history. `agent
secrets list` shows names and fingerprints, never values.

Be clear about what that file is: a 0600 file read unattended by a daemon that starts at boot,
so it is access control, not encryption. The *agent* is fenced out of it three ways — the path
is outside `allowed_roots`, a named `hard_deny` rule covers it, and the sandboxed shell mounts
only the workspace, so there is no route to `~/.config` from a sandboxed command at all. None of
that helps against code running as you. If that ever needs to be a real boundary, the answer is
a separate uid for the daemon and a broker socket, not a cipher stored next to the thing it
encrypts.

## Connectors

A connector is daemon-side code that watches something outside this machine. It is emphatically
not a tool: it is never in the registry, the model cannot call it, and it holds a credential the
agent is fenced out of. That is the whole reason this is a connector and not a `github_search`
tool — the credential and the untrusted text it fetches end up on the opposite side of the
boundary from the thing that could be talked into misusing them.

Everything a connector ingests is archived as a `raw_events` row marked `untrusted`. There is no
trusted-sender list and there will not be one, because a sender is a claim and not a credential.

The GitHub connector turns review requests, mentions and assignments into open loops. A review
request becomes a loop titled `Review requested: PR #412 in org/repo` — a sentence this code
composed, whose only variable parts are a dictionary lookup, an integer, and a repository name
checked against GitHub's own charset for owner/repo. The pull request's *own* title, which
anybody with an account can write, goes into `detail`, and nothing renders `detail` to the model.
That asymmetry is deliberate: open-loop titles reach an LLM prompt through the heartbeat's
situation report, so a title is model-visible input and composing one is an injection boundary,
not formatting. `agent loops show <id>` is how a human reads the other half.

```bash
agent secrets set github/<your-login> token   # a PAT; stdin keeps it out of your history
# then [connectors] enabled = true and [connectors.github] enabled/user in config.toml
agent connectors poll github                  # one poll, in the foreground, with output
agent connectors list                         # is it working, and when did it last succeed
```

The token needs the account-level **Notifications** read permission, which on a fine-grained PAT
is separate from repository access. If it is missing, GitHub answers 403 with no rate-limit
headers; the connector reads that as a permissions problem, disables itself and tells you,
rather than retrying forever.

Two limitations worth knowing rather than discovering. It reads the *unread* notification feed,
so a review request you dismissed on your phone never reaches it — fixing that means
`/search/issues`, a second cursor and a second rate limit, and it is deferred. And it is strictly
read-only: it never marks anything read on GitHub, so your inbox is untouched and a read-only
token is enough. An hourly unconditional sweep closes loops whose thread has left your unread
list; the minute-by-minute poll is conditional and deliberately never closes anything, because a
304 means "nothing changed", not "everything is finished".

### Mail, calendars and coursework

Four more connectors, all read-only, all holding their own credential. They share one rule
with GitHub and with each other: an open-loop title is a sentence this code composed, and
the sender's own words — subject lines, event summaries, course names — go into `detail`,
which nothing renders to the model.

| connector | source | credential | opens |
|---|---|---|---|
| `gmail-<label>` | Gmail API | OAuth refresh token, `gmail.readonly` | one loop per sender |
| `gcal-<label>` | Calendar API | the same token, `calendar.readonly` | nothing — see below |
| `imap-<label>` | any IMAP server | a password | one loop per sender |
| `brightspace` | the per-user iCal feed | the feed URL | one loop per deadline, with a real `due_at` |

**Google.** One Cloud project, one Desktop OAuth client, then one browser round trip per
account:

```bash
agent secrets set google/client client_id        # and client_secret
agent connectors auth nyu                        # --port 8771 + `ssh -L` if headless
agent connectors poll gmail-nyu
```

Mail and calendar share a credential but are two connectors, because Gmail being rate-limited
at midday should not stall the calendar poll, and `agent connectors list` should say which of
the two is unhappy. The scopes are read-only, which is stronger than a promise: `gmail.readonly`
cannot mark a message read, so seeing a message never changes your unread count — and that is
what makes the hourly sweep meaningful, since your own inbox stays the source of truth for
whether somebody is still waiting.

**Why a calendar event is not an open loop.** It is not a thing waiting on you; it is a thing
that will happen whether or not you act, and fourteen days of events would bury the loops that
mean somebody is blocked. Events are archived, and the heartbeat says *how much* of the next
24 hours is spoken for — a count and a clock time, never a summary, because a summary was
written by whoever sent the invitation and the situation report goes straight into a prompt.

**IMAP**, for the mailboxes with no API. A mail password is the whole mailbox; there is no
read-only scope to hide behind, so the guarantee is structural and doubled: the mailbox is
opened with `EXAMINE` and headers are fetched with `BODY.PEEK`, neither of which can set
`\Seen`. This is also the one connector that ignores the `httpx.AsyncClient` the framework
hands it — IMAP is a stateful TLS socket, so the conversation runs in a worker thread.

**Brightspace** reads the per-user calendar feed, not the Valence API: Valence needs an
application key that a D2L administrator registers, which a student cannot do. The cost is no
announcements and no grades. The feed URL contains a token, so it is a credential and lives in
the vault:

```bash
# Brightspace -> Calendar -> Subscribe, copy the link
agent secrets set brightspace/nyu ics_url
```

Two failure modes are handled on purpose rather than discovered. A 200 carrying HTML is an SSO
login page — what you get when the link is copied from the address bar — and is treated as an
auth error, because reading it as "no events" would close every deadline you have. And a
redirect to another host is refused outright, since the token is *in* the URL and following it
would hand the credential to whoever controls the target.

Unlike mail and GitHub, this connector sets a real `due_at`: somebody else set that deadline,
which is exactly the case `overdue_loops()` exists for, so the heartbeat does speak up once one
passes.

**Your noise budget** is `[connectors.*.rules]`: `direct_only` (you in To or Cc), `skip_bulk`
(`List-Unsubscribe`, `List-Id`, `Precedence`, `Auto-Submitted`), `aliases`, `skip_senders`,
`due_in_h`, `notify`. Titles are not tunable and will not be; they are an injection boundary,
not formatting.

One design note worth knowing: **one loop per sender, not per message.** Five emails from the
same person while you have not replied is one thing waiting on you. The sweep closes it when
nothing from them is unread any more.

## Channels

A **channel** is the third category beside connectors and tools, and keeping the three apart
is what keeps the rest of the design honest:

| | holds a credential | model can call it | direction |
|---|---|---|---|
| connector | yes, fenced from the agent | never | inbound only |
| tool | no | yes, policy-gated | whatever the tool does |
| channel | yes, fenced from the agent | never | your words in, the loop's words out |

`agent chat` is a channel. So is ntfy, for the outbound half. Telegram is both halves.

```bash
# @BotFather -> /newbot -> copy the token
agent secrets set telegram/bot token
agent telegram check          # token good? which bot is it?
# message the bot anything, then:
agent telegram whoami         # prints the chat id to allowlist
# [telegram] enabled = true, allowed_chat_ids = [<that id>]
```

`allowed_chat_ids` is the entire security boundary — anyone who learns a bot's username can
message it, so an empty list means *nobody*, not everybody. An unlisted sender is dropped
without a reply (answering confirms the bot is alive), every attempt is audited, and the
first one notifies you once.

In the chat: `/new` starts a fresh conversation and is how you lift the private-data
interlock; `/status`, `/approvals` and `/approve <id>` are there because a queued approval
you cannot see from your phone is a queued approval you will not act on; `/tools on|off`
controls the progress line.

That line is on by default and is the phone's version of watching tool calls scroll past in
a terminal — one message that fills in as the turn runs:

```
✓ gmail_search
… memory_search
✗ web_fetch (refused by policy)
```

Names, never arguments: an argument can hold a query built out of something a stranger
emailed, and there is already one rule in this codebase about rendering their words rather
than one rule per surface. It is edited rather than re-sent, because a trail of
near-identical messages is what makes a phone unusable, and every edit is best-effort — a
rate limit costs you the progress line, never the answer. `/approve` shares
`policy.replay.execute_approved` with the CLI rather than reimplementing consent — a half
approval that marked a row `approved` without running it would strand the action forever,
since the CLI returns early on anything that is not `pending`.

What a channel is *not* is a way for the model to send something. There is no tool here, the
`no-mail-send-tool` tripwires are untouched, and the only addresses reachable are the ones in
the allowlist. A turn from Telegram runs at `origin: telegram`, which the policy treats as
interactive — a human is present — except that anything `external` still stops at an
approval, stated as its own rule so a future loosening of the risk matrix cannot take it away.

**The honest cost.** Telegram bot messages are not end-to-end encrypted; they cross
Telegram's servers in a form Telegram can read. Everything else here was built so nothing
leaves hardware you control — ntfy is self-hosted on the tailnet for precisely that reason —
and this is the one place that stops being true. An answer about your mail is an answer that
reached Telegram. If that trade is not acceptable, the same channel shape would fit Signal or
a self-hosted Matrix homeserver, which is a different `call()` and the same everything else.

## Operating notes

- **Local model.** `llm.base_url` points at the OpenAI-compatible endpoint: the SIR router on
  `:8000`, which schedules GPU residency and queues across models. Its tool-call passthrough
  fix is deployed, so there is no longer any reason to talk to vLLM directly — doing so
  bypasses the scheduler, and requests then fail when the backend is swapped. The provider
  retries connection errors with backoff for exactly that reason.
- **Docker is rootful here**, so the sandbox never mounts the docker socket and runs
  `--network none --read-only --cap-drop ALL` as an unprivileged user.
- **Backups.** `agent backup` snapshots both durable things — a `pg_dump -Fc` of the database
  and a `git bundle` of the memory repo — into `~/.local/share/agent/backups`, keeping the last
  14. The daemon runs it nightly after consolidation and notifies you if it fails, since a
  backup job whose failures are silent is worse than none. `pg_dump` runs inside the database
  container, because the host has no client and a mismatched major version would refuse anyway.

  `agent backup --verify` restores into a scratch database and counts the rows back; `agent
  restore --from <dir>` restores into a *new* database by default and refuses to overwrite the
  live one without `--force`. Rehearse it — an untested backup is a rumour. A git remote on the
  memory repo is still worth adding: it is the only copy that would survive this box.

## Tests

```bash
pytest                    # unit + integration; skips if postgres is down
pytest -m docker          # sandbox tests
pytest -m live            # needs a reachable model endpoint
```

`scripts/e2e_manual.md` is the end-to-end walkthrough.
