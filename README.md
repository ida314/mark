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
agent init                      # config + data dirs + git memory repo
agent db up && agent db migrate # postgres+pgvector on 127.0.0.1:55432
agent sandbox build             # container image for shell_exec
agent doctor                    # checks everything, including that tool calling really works
```

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

## Background work

`agent daemon run` (or `agent daemon install-unit` for systemd) runs watchers, reminders,
file watching over the memory repo, idle and nightly consolidation, and a heartbeat. The
heartbeat builds a deterministic situation report first and only spends a model call when
something actually changed and is actionable — the infrastructure is always on, the model
is not.

## Operating notes

- **Local model.** `llm.base_url` points at the OpenAI-compatible endpoint. The SIR router
  on `:8000` is the right long-term target because it schedules GPU residency; it needs the
  tool-call passthrough fix deployed first. Pointing straight at vLLM on `:8001` works but
  bypasses that scheduler, so requests fail when the backend is swapped. The provider
  retries connection errors with backoff for exactly this reason.
- **Docker is rootful here**, so the sandbox never mounts the docker socket and runs
  `--network none --read-only --cap-drop ALL` as an unprivileged user.
- **Backups.** Everything durable is in the `agent_pgdata` volume and the memory git repo.
  `pg_dump` and a git remote are worth adding.

## Tests

```bash
pytest                    # unit + integration; skips if postgres is down
pytest -m docker          # sandbox tests
pytest -m live            # needs a reachable model endpoint
```

`scripts/e2e_manual.md` is the end-to-end walkthrough.
