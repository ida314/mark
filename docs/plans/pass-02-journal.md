# Pass 2 — Durable Run Journal

**Architecture reference:** §15, §16 (journal), Phase 2B, Phase 9
**Depends on:** `docs/records/pass-01-outcome.md`
**Load into session:** `CLAUDE.md`, this file, pass-01 outcome

## Goal

One append-only event log that is both the durability substrate and the frontend feed.
This is the pass that makes everything after it cheap. If there are two event paths at the
end of this pass, the pass failed.

---

## Session 2a — Store and writer

**Scope.** SQLite journal, append-only.

```
journal
  run_id        text
  seq           integer        # monotonic per run
  ts            timestamp
  type          text
  payload       json
  primary key (run_id, seq)
```

Writer enforces monotonic `seq` per run and rejects out-of-order appends. Append is
synchronous on the write path for `effect_*` and `checkpoint_written` events; other event
types may be buffered.

Also define retention: initial policy is keep everything, with a documented pruning hook
for later.

**Exit.** Events append durably. Process kill mid-write leaves the journal readable and
consistent.

---

## Session 2b — Event coverage

**Scope.** Wire the full event vocabulary through the existing runtime:

```
agent_started        agent_finished
tool_requested       tool_started       tool_progress
tool_finished        tool_failed
worker_created       worker_finished
handoff_started      handoff_finished
message_appended
checkpoint_written
effect_intended      effect_committed
run_resumed          run_forked
```

The last six are written by Passes 3, 4, and 5. Define their payload shapes now so those
passes do not re-litigate the schema.

Every event carries `run_id`, `seq`, `ts`, and where applicable `worker_id`, `step_id`.

**Exit.** A complete run produces a journal from which the sequence of what happened is
readable without reference to any other source.

---

## Session 2c — Frontend subscription

**Scope.** Frontend consumes a journal-backed stream. Reconnection replays from a
last-seen event id rather than losing state.

Remove any pre-existing in-memory event bus. Do not leave it running alongside.

**Exit.** Kill the frontend mid-run, reconnect, and the UI state is correct with no gap.

---

## Exit criteria (pass)

Kill the process mid-run at three different points; the journal contains the complete
event sequence up to the kill in each case. Frontend reconnect after a drop replays
without visible loss.

## Must not

- Implement checkpoints. Nothing reads the journal for recovery yet.
- Change agent behavior, prompts, or the tool surface.
- Build a second event path parallel to the journal.

## Outcome record must capture

- Journal schema as implemented
- The full event type list with payload shapes, including the six not yet written
- Which events are synchronous on the write path and which are buffered
- Retention and pruning policy
- Frontend replay mechanism
