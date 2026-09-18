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
