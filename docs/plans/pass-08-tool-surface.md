# Pass 8 — Tool Surface Reduction

**Architecture reference:** §8, §9, §10, Phase 4
**Depends on:** `docs/records/pass-01-outcome.md`, `docs/records/pass-06-outcome.md`
**Load into session:** `CLAUDE.md`, this file, pass-01 and pass-06 outcomes

## Goal

Get the orchestrator to 8–12 permanent tools without losing completion rate.

Removing a tool from the orchestrator before delegation is reliable converts a working
path into a broken one. Do not start this pass until Pass 6 is done.

Move one capability family per session, verifying between each.

### The two numbers, and which one 8d compares against

*Added 2026-09-24, on Dylan's ruling, after the surface was measured rather than assumed.*

"Permanent surface" above reads as the `always_on` set, which is **13** tools. But
`Registry.select` also hands a turn its session-used tools and the top-k embedding matches,
and what a turn actually pays for — in prompt tokens, and in the selection error this pass
exists to reduce — is the **offered** set. Measured from live telemetry, 46 records with
`role == "main"`:

| | value |
|---|---|
| tools offered per turn | **20** on 39 of 46 turns; 19 on six; 18 on one |
| `always_on` | 13 |
| enabled in the registry | 29 (26 at the Pass 1 baseline) |

**The before-number 8d compares against is 20, not 13.** Report both in the 8d table: the
permanent set is what this pass moves, the offered set is what it is trying to make smaller.

Two consequences for the plan below. The target surface names `reminder_set`, `watcher_add`
and `open_loops_list` as permanent, and **none of the three is `always_on` today** — so
8a–8c are not purely subtractive, and reaching the target means promoting as well as
moving. And the borderline cases the pass defers to measurement (`goal_upsert`,
`open_loop_add`, `open_loop_close`, `profile_read`) start from a mixed state: only
`profile_read` is `always_on` now.

### "Moved out of the orchestrator surface" has to mean something

*Added 2026-09-24. Ruled as its own commit, landing before the 8a baseline run.*

Four doors put a tool in front of a turn: `Registry.select`; `AgentLoop._with_lookup`;
`tool_search` via `ctx.extra["added_tools"]`; and `agent/loop.py:699-700`, which added **any
registered tool the model named** to `exposed` and ran it, with no visibility check anywhere
in `tools/executor.py`. Dropping a tool from `always_on` closes the first two only, so
before that commit this pass's central verb was advisory.

All 258 real `tool_requested` events in the live journal are `visible=1, known=1`, so no past
run was affected and the pre-8a baseline is not contaminated by the fix. **Every session of
this pass must treat "the orchestrator cannot call X" as a claim needing a test**, not a
description of what was removed from a list.

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

### Both sides of the comparison are measured the same way

*Added 2026-09-24, on Dylan's ruling.*

The pre-8a baseline (B11–B13 under `chat`) is run against a **clone at a recorded sha**, with
`[paths] project` pointed at the clone, not at the working checkout. Two reasons, and the
second is the one that binds 8d: the real checkout holds untracked files and is edited in
parallel, so it is not a stable measurement surface; and the rows reset between tasks with
`git checkout` and a patch apply/reverse, which should not happen in a tree someone is
working in.

**8d's closing comparison runs the same way — a clone at 8a's head, with the sha recorded —
so both sides of the comparison are measured identically.** A comparison where the before was
taken on a clone and the after on a live checkout is measuring the checkout as well as the
tool surface.

Note for whoever sets this up: **clone it, do not `git worktree` it.** Verified 2026-09-24 —
a worktree's `.git` is a file pointing at a gitdir outside the container mount, so `git`
inside the sandbox dies with `fatal: not a git repository`. A clone works, including
`git apply` of the B12 fixture.

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
