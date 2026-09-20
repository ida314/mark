# Pass 3 — Effect Class & Effect Ledger

**Architecture reference:** §16 (effect ledger), §19 (tool metadata)
**Depends on:** `docs/records/pass-02-outcome.md`
**Load into session:** `CLAUDE.md`, this file, pass-02 outcome

## Goal

Know, for every tool call, whether re-running it is safe. This is the pass that prevents
a crash from sending an email twice.

The audit sessions are mostly judgment, not code. Budget accordingly.

---

## Session 3a — Registry field and validation

**Scope.** Add to the tool registry schema:

```
effect_class    read | idempotent_write | unsafe_write
```

Mandatory. No default. Registration fails if absent — a mislabeled or unlabeled effect
class is exactly the failure that produces a duplicate action on resume, and a silent
fallback to `read` guarantees it.

Definitions to put in the code comment:

```
read              no external state change; free to re-execute
idempotent_write  re-execution converges to the same state
unsafe_write      re-execution may duplicate a real-world action
```

**Exit.** Startup fails loudly with an unclassified tool.

---

## Session 3b — Ledger and key derivation

**Scope.**

```
effect
  idempotency_key   text primary key
  run_id            text
  step_id           text
  tool              text
  effect_class      text
  args_hash         text
  state             text        # intended | started | committed | failed | orphaned
  result_ref        text
  created_at        timestamp
  updated_at        timestamp
```

```
idempotency_key = hash(run_id, step_id, tool_name, canonical_args)
```

Argument canonicalization is the part that quietly breaks. Exclude timestamps, request
ids, nonces, and any field the caller regenerates per attempt. Write the exclusion list
explicitly and test that two attempts at the same logical call produce the same key.

Protocol: `intended` before dispatch → `started` on dispatch → `committed` with
`result_ref`, or `failed`. Each transition appends to the journal.

**Exit.** Every effecting call produces a ledger entry. Killing the process mid-call
leaves an entry stuck at `started`.

---

## Session 3c — Audit: reads and internal tools

**Scope.** Classify every tool that reads or touches only local state. Maintain
`docs/records/effect-classification.md` as a table: tool, class, one-line justification.

Most of these are `read`. The ones that are not are the interesting cases; note why.

---

## Session 3d — Audit: writes and integrations

**Scope.** Classify everything that touches the filesystem, shell, email, calendar, or any
external API.

Default to `unsafe_write` when uncertain. The cost of a false `unsafe_write` is one
avoidable prompt to the user. The cost of a false `idempotent_write` is a duplicate action
you cannot take back.

Specific calls worth arguing about in the record:

```
fs_write        unsafe_write     (overwrite is not idempotent w.r.t. concurrent edits)
shell_exec      unsafe_write     (arbitrary; cannot be reasoned about generically)
reminder_set    depends on whether the API accepts a client-supplied id
calendar create depends on the same
gmail send      unsafe_write     (always)
memory writes   idempotent_write (if keyed) — see Pass 7
```

**Exit.** Classification table complete, every tool covered.

---

## Exit criteria (pass)

Every registered tool has an `effect_class`. Every effecting call writes a ledger entry
with a stable idempotency key. Two attempts at the same logical call produce the same key.

## Must not

- Implement reconciliation, resume, or orphan handling. Nothing reads the ledger yet.
- Change which tools exist or where they live. That is Pass 8.
- Skip the canonicalization test because the keys "look right."

## Outcome record must capture

- Ledger schema as implemented
- Key derivation, including the full argument exclusion list
- The complete classification table
- Any tool where the classification is uncertain and why
