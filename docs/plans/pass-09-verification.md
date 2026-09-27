# Pass 9 — Result Verification & Replanning

**Architecture reference:** §14, §21, Phase 6B
**Depends on:** `docs/records/pass-06-outcome.md`, `docs/records/pass-08-outcome.md`
**Load into session:** `CLAUDE.md`, this file, those two outcomes

> **Filed 2026-09-25**, ahead of discovery and evaluation, on Dylan's ranking. Discovery &
> Router became Pass 10 and Evaluate & Tune became Pass 11 in the same edit; a record
> written before that date means the old numbers. The reason for the order is in the goal
> below: Pass 11b measures a suite whose coding, web, mail and memory rows are all graded on
> worker claims, and until those claims are checked the measurement inherits their error.

## Goal

A worker's report is a claim. The runtime should know whether it is true.

Since 8a every code, web, mail and deep-memory answer reaches the orchestrator as a
`WorkerReport` — a second inference over a truncated transcript (`subagents.py`,
`text[:20000]`) made by a model that no longer has the tool results in front of it. It was
measured wrong four times in two days, in both directions:

```
B10   cited three file paths        eleven shell_exec calls had found the real ones;
                                    the three cited files do not exist
B11   "No work was performed"       fifteen tool calls read the repo; report_valid=True
B12   "all 19 tests pass"           2 of 19 failed, re-run on the same commit
B16   "no web access from that      zero tool calls in that run; the same role made ten
       context"                     successful web calls minutes earlier
```

And a fifth class 8d found without naming it this way: four rows emitted tool-call markup as
prose, and the runtime recorded `status=completed, steps=1` without noticing that nothing
ran.

The disproof is already on disk in every one of these. `run_subagent` opens a worker-scoped
`JournalTail` and reads two things out of it — `tool_failed` for the transcript and
`agent_finished` for `budget_exhausted` — and throws the rest away. `tool_requested` carries
the full parsed `args`; the terminal event carries the outcome, and for `shell_exec` the
`exit=N` prefix. No model call is needed to find the contradiction.

Three moves, in dependency order: attach what the worker did and flag what contradicts it;
make a flagged completion something the orchestrator decides about instead of relays; and
give a turn that hits a limit one more move instead of only an ending.

---

## Session 9a — The ledger and the verifier

**Scope.** What the worker actually did, read back out of its own journal, and the
deterministic checks over it.

```
ledger_from_events(events) -> WorkerLedger      # pure, over journal events
verify(result, ledger)     -> tuple[Flag, ...]  # no model call
validation_of(flags)       -> valid | uncertain | invalidated
```

The ledger joins `tool_requested` → `tool_finished`/`tool_failed` on `call_id`, and carries:
every call with its args and outcome, the worker's own turn status, the files and urls it
touched, how many shell commands it ran, the last `shell_exec` exit code, and the failure and
denial counts.

Six checks. Each one names the row that measured it, and each one can name the journal event
it read:

```
tests_not_run             hard    B12
file_not_read             hard    B10
status_conflict           hard    B11
no_attempt                soft    B16
completed_without_tools   soft    8d's four steps=1 prose rows
write_not_performed       soft    symmetric with tests_not_run
```

A hard flag means `invalidated`; soft flags alone mean `uncertain`; no flags means `valid`.
Every flag carries a runtime-written sentence quoting the journal, because a flag is shown to
the orchestrator and nothing shown to a model may be assembled from model output — the rule
`results.py` already keeps for `notes`.

Flags and the validation status live on `WorkerResult`. The verification is journaled as its
own event, emitted before `worker_finished`, so no fold ever sees a finish without it.

**Exit.** Each of the five measured shapes reproduced as a fixture and flagged, with no model
call in the path.

---

## Session 9b — Replanning on the signal

**Scope.** Acting on 9a's flags. Cheap, because the signal already exists.

Tool success and validation status are separate axes (Dylan's ruling, 2026-09-25).
`delegate` returns `ok=True` when the worker ran and reported `completed`; a contradiction is
carried by `validation_status`, not by flipping `ok`. What `invalidated` costs:

```
not cacheable            result_cache.remember refuses anything not valid
no candidate memories    a proposal grounded in a disproved report is not a proposal
not an answer            runtime-written text names the flags and the three moves
```

The three moves are §21's, already in the architecture: re-delegate with a narrower brief
that names the file or the command, ask Dylan, or return what is known and say what is not.
The orchestrator picks; the runtime does not re-delegate on its own, because a runtime that
re-delegates automatically can loop on a false positive with no one watching.

**Exit.** An invalidated completion cannot reach the user as an answer, cannot be served from
the cache, and leaves no candidate memory behind.

---

## Session 9c — The escape hatch

**Scope.** `max_steps`, `FINAL_NUDGE` and `STUCK_LIMIT` all end the turn today. Hitting a
limit should offer one more choice first.

```
[agent] escape_step = true      one extra iteration, delegate only, then the tool-free step
ended_by_choice                 a step that was offered tools and asked for none
denial finality                 a denial repeated with identical args withdraws the tool
```

The escape step is offered to every caller. For a worker it degrades to no tools, because no
role holds `delegate`, so worker behaviour does not change.

`ended_by_choice` replaces `steps >= max_steps` as the test for `abandoned`. This also settles
the logged heartbeat item: a heartbeat that answers on its escape step reports `completed`
instead of being `abandoned` by construction.

Denial finality is the other half of the heartbeat item — nothing in the loop tells a model
that a `deny` is final rather than an argument problem worth rephrasing. Counting denials per
`(tool, rule, args-fingerprint)` and reusing the existing `STUCK_LIMIT` withdrawal makes a
repeated identical denial withdraw the tool, while a differently-argued call to the same tool
is untouched — which is what keeps `delegate` alive when only `agent="mail"` was refused.

**Exit.** A turn that spends its budget gets exactly one escape step; a turn that chose to
stop is `completed`; a denial repeated verbatim costs one step, not the budget.

---

## Session 9d — Measured verification

**Scope.** Not run in the session that implements 9a–9c. The five measured shapes, re-run
against a clone at a recorded sha under `~/Projects/agent-evals/`, by the 8d method: the
daemon stopped for the window, promotion off, per-row reset, and **both sides measured the
same way** — a before taken on a clone and an after taken on a live checkout measures the
checkout too.

The number this session exists for is the **false-positive rate per flag**: how often a flag
fires on a worker whose report was true. It is the only thing that decides whether a hard
flag stays hard, and a hard flag with no measurement behind it is a guess that costs a second
worker.

**Exit.** A per-flag table: fired / true positive / false positive, with the row behind each.

---

## Exit criteria (pass)

A worker's report is checked against its own journal without a model call. A contradiction
reaches the orchestrator as a decision rather than as a caveat. A turn that hits a limit has
one move left. Every flag has a measured false-positive rate.

## Must not

- Make a model call in the verification path. A verifier that costs an inference is a second
  thing that can be wrong about the same transcript.
- Ship a check that cannot name the journal event it read. A heuristic over the answer text
  alone is a grader, not a verifier.
- Re-delegate automatically inside the runtime. The orchestrator decides; a false positive
  must cost a decision, not a worker.
- Change the `blocked`/`uncertain` → `ok=False` mapping. `observations.py` reads a failed
  effect as *uncertain*, which is what stops a delegation that may have written files from
  being offered for a silent retry. Flipping it belongs in a pass about the effect ledger.
- Keep a hard flag whose false-positive rate 9d has not measured.

## Outcome record must capture

- The ledger as implemented: which events it reads, and what it cannot see
- Every check, its class, and the row it was built from
- The journal vocabulary change, with the event schema as written
- What `invalidated` costs a result, enumerated
- The escape step's measured cost in extra model calls
- Every check's known false-positive shape, even before 9d measures the rate
