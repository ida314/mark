# Session protocol

## Running a pass

1. `/clear`
2. Confirm the working tree is clean and the previous pass is committed.
3. Prompt: `Read docs/plans/pass-NN-<name>.md and the dependency records it lists. Plan session NNa.`
4. Review the plan. Check it against the `Must not` section before accepting.
5. Execute.
6. Write or update `docs/records/pass-NN-outcome.md`.
7. Commit. Then `/clear` before the next session.

## Within a session

- `/compact` when you are deep in one problem and the decisions matter.
- `/clear` when the context has picked up a wrong assumption it keeps returning to.
- Watch `/context`. Act before auto-compact does.
- Use a subagent for codebase exploration during planning so file reads do not accumulate
  in the session you are going to implement in.

## Sub-sessions

Every pass file lists its sessions as `NNa`, `NNb`, and so on. Each is a separate
`/clear`-to-`/clear` cycle. A pass with four sessions is four sittings, not one long one.

Between sub-sessions, append to the same outcome record rather than starting a new one.

## Outcome records

Written at the end of every session. The next pass trusts the record, not the plan,
because the plan describes what was intended and the record describes what exists.

Required sections:

```
what shipped
what deviated from the plan, and why
what is now true about the code that was not before
schemas as actually implemented
deferred items, and where they went
open questions for later passes
```
