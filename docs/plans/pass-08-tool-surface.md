# Pass 8 — Tool Surface Reduction

**Architecture reference:** §8, §9, §10, Phase 4
**Depends on:** `docs/records/pass-01-outcome.md`, `docs/records/pass-06-outcome.md`
**Load into session:** `CLAUDE.md`, this file, pass-01 and pass-06 outcomes

## Goal

Get the orchestrator to 8–12 permanent tools without losing completion rate.

Removing a tool from the orchestrator before delegation is reliable converts a working
path into a broken one. Do not start this pass until Pass 6 is done.

Move one capability family per session, verifying between each.

---

## Target permanent surface

```
delegate
tool_search

time_now
calendar_upcoming
coursework_due

reminder_set
watcher_add
notify_user

goals_list
open_loops_list
```

Possibly retained, decided empirically:

```
goal_upsert
open_loop_add
open_loop_close
profile_read
```

---

## Session 8a — Coder role, filesystem and shell

**Scope.** Establish the durable `coder` role. Move out of the orchestrator surface:

```
fs_list  fs_read  fs_search  fs_write  shell_exec
```

The coder owns repository exploration, code search, file inspection, implementation,
editing, test execution, debugging, build commands, and relevant shell operations.

Define ephemeral workers within the role where they help: repo explorer, implementation
worker, test/debug worker.

The orchestrator may still answer very small coding questions that require no repository
access.

**Exit.** Re-run the coding tasks from the Pass 1 suite. Completion rate not regressed.

---

## Session 8b — Researcher role, web

**Scope.** Establish `researcher`. Move out `web_search`, `web_fetch`.

The researcher handles open-ended research, multiple searches, source comparison,
retrieval, document reading, evidence extraction, and synthesis, and returns a compressed
structured result rather than its search transcript.

The orchestrator supplies the research objective, not individual searches.

Ephemeral workers: source finder, document analyst, synthesis worker.

**Exit.** Re-run the research tasks. Completion rate not regressed.

---

## Session 8c — Memory and integrations

**Scope.** Move out `memory_history`, `memory_search`, `gmail_search`, `gmail_message`.
Establish the `memory` durable role over the episodic and semantic operations from Pass 7.

Integrations reachable through dynamic discovery or a durable role, not permanent
visibility.

**Exit.** Re-run the memory and email tasks. Completion rate not regressed.

---

## Session 8d — Verification

**Scope.** Full Pass 1 suite against the Pass 1 baseline. Record the comparison.

If completion rate regressed on a specific family, the answer is usually that the role's
task specification is underspecified, not that the tool should return to the orchestrator.
Establish that before reverting anything.

**Exit.** Orchestrator surface in the 8–12 range with completion rate at or above baseline.

---

## Exit criteria (pass)

Target surface reached. No regression against baseline. Every moved tool reachable through
a durable role.

## Must not

- Run before Pass 6.
- Move more than one family per session.
- Keep a tool permanently visible because moving it was inconvenient. Record the tension
  instead and resolve it in Pass 10.

## Outcome record must capture

- Final permanent surface, with the empirically decided borderline cases
- Which durable role owns each moved capability
- Ephemeral workers defined per role
- The before/after comparison table
