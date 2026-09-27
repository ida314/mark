"""Delegation: hand a task specification to an ephemeral sub-agent and get back a summary.

This is the model's door onto `agent.delegation.delegate`, and the only thing it does on its
own is unpack the tool call. The wire argument stays `agent` rather than becoming
`durable_role`: `config/policy.default.yaml` matches on `args.agent` twice - to keep a
private-data turn from delegating its way around the interlock, and to keep the daemon from
delegating its way into the mailbox - and a rename here that did not land in the policy file
in the same edit would open both doors silently.

Session 8c removed the one branch that was not a delegation. `agent="memory"` used to call
`memory.retrieval.pack` in this process and build no worker; it now starts a worker like
every other role, because the memory tools moved to that role and a family cannot move to a
role with no spec behind it. The consequence for this file is that there is no longer any
special case here at all: every value of `agent` is a durable role.

What this file does own is the one thing a delegation can hand back that a status and an
answer do not say: whether the worker put the user's own private data in the caller's
context. `agent/loop.py` raises `session.private` from `private_output` on a tool it ran
itself, and a `mail` delegation has to raise the same flag for the same reason - otherwise
moving the mailbox behind a worker would repeal the interlock by moving it out of reach.
"""

from __future__ import annotations

import json

from ..agent.observations import RUNTIME_NOTE
from .base import Tool, ToolContext, ToolResult, obj, required, tool
from .effects import UNSAFE_WRITE

# Session 9b. Written here, in full, and never assembled from anything the worker said. It is
# appended to a result the runtime could not corroborate, and it names the three moves §21 of
# the architecture already gives the orchestrator - the runtime does not pick one, because a
# runtime that re-delegates on its own can loop on a false positive with nobody watching.
#
# Carries `RUNTIME_NOTE` rather than a marker of its own, so the one tuple that means "the
# runtime wrote this, nobody said it" still covers it: `agent/handoff.py` refuses it into a
# handoff and `memory/promotion.py` refuses it into a durable belief, both for free.
INVALIDATED_BLOCK = f"""
{RUNTIME_NOTE} This result is INVALIDATED. The runtime checked the report
against what this worker actually did, from the run journal, and they contradict:
{{flags}}
Do not relay this answer and do not act on it. Choose one: delegate again with a narrower
brief that names the exact file or command, ask the user, or say what is known and what is
not."""


@tool(
    "delegate",
    (
        "Hand a self-contained task to a sub-agent: 'researcher' for anything outside this "
        "machine - searching the web, opening a url, reading a document, comparing what "
        "sources say - 'coder' for anything in the user's code - reading it, searching it, "
        "editing it or running it - 'memory' for a deep search of what you know about the "
        "user, including how a belief changed - 'mail' for anything in the user's mailbox, "
        "which is the only way to read it. "
        "You get back a status (completed, blocked or uncertain), an answer, its evidence and "
        "what was done - not their whole transcript."
    ),
    required(
        obj(
            agent={"type": "string", "enum": ["researcher", "coder", "memory", "mail"]},
            task={
                "type": "string",
                "description": "A complete brief: they cannot see this conversation.",
            },
            context={
                "type": "string",
                "description": "Background they need and cannot see for themselves",
            },
            constraints={
                "type": "array",
                "items": {"type": "string"},
                "description": "Limits on how they do it: what to avoid, what to prefer",
            },
            expected_output={
                "type": "string",
                "description": "What they must come back with. Defaults to the role's own.",
            },
        ),
        "agent",
        "task",
    ),
    tags=("core",),
    always_on=True,
    # unsafe_write: the sub-agent may call anything, including `fs_write` and the shell,
    # so replaying the delegation replays whatever it chose to do.
    effect_class=UNSAFE_WRITE,
)
async def delegate(args: dict, ctx: ToolContext) -> ToolResult:
    agent_name = args["agent"]

    from ..agent import delegation

    approver = ctx.extra.get("approver")
    if approver is None:
        from ..policy.approvals import QueueApprover

        approver = QueueApprover(origin=ctx.origin)

    try:
        result = await delegation.delegate(
            agent_name,
            args["task"],
            relevant_context=args.get("context") or (),
            constraints=args.get("constraints") or (),
            expected_output=args.get("expected_output") or "",
            parent_session_id=ctx.session_id,
            parent_turn_id=ctx.turn_id,
            parent_autonomy=ctx.autonomy,
            approver=approver,
            parent_action_id=ctx.action_id,
            parent_run_id=ctx.run_id,
            parent_step_id=ctx.step_id,
        )
    except delegation.DelegationError as exc:
        # The model wrote a delegation that is not one. It gets the sentence, not a worker
        # started on a guess at what it meant.
        return ToolResult(content=str(exc), ok=False)
    from ..config import get_config

    # `for_orchestrator` is the only thing that renders a result for a model, and the flag is
    # read here rather than threaded through the worker so that the switch and the render sit
    # in one place. Off, the transcript stays in the archive.
    payload = result.for_orchestrator(debug=get_config().delegation.debug_transcripts)
    content = json.dumps(payload, indent=2)
    if result.validation == "invalidated":
        content += INVALIDATED_BLOCK.format(
            flags="\n".join(f"  - {f.detail}" for f in result.flags)
        )
    # Session 8c. Read off the role, after the delegation succeeded and so after the role
    # name has been validated. `reads_private_data` derives it from what the role's tools
    # declare, so this is not a list of role names that has to be kept in step with one.
    from ..agent.subagents import reads_private_data

    private = reads_private_data(delegation.role_spec(agent_name))
    return ToolResult(
        content=content,
        # Only `completed` is a tool call that did what it was asked. `blocked` and
        # `uncertain` are both `ok=False`, which the executor records as a failed effect -
        # and `agent/observations.py` reads a failed effect as *uncertain*, never as
        # blocked, so a delegation that may have changed files is never offered for a
        # silent retry.
        #
        # Session 9b, on Dylan's ruling: tool success and validation status are separate
        # axes, so an `invalidated` result does **not** flip this. The worker ran and
        # reported; what is refused is its claim, and that refusal travels in `validation`
        # and in the block above. Collapsing the two would make "the delegation failed" and
        # "the delegation lied" the same fact, and they call for different moves.
        ok=result.status == "completed",
        trust="untrusted" if result.tainted else "trusted",
        # `private` is what `agent/loop.py` reads to close the interlock on the *caller*.
        # The worker's own flag died with its session, and its answer is the user's mail.
        data={
            "status": result.status, "report_valid": result.report_valid,
            "private": private,
            # Session 9b. The second axis, in the structured place, so anything downstream
            # matches on a code rather than on the wording of the block above.
            "validation": result.validation,
            "flags": [f.code for f in result.flags],
        },
    )


TOOLS: list[Tool] = [delegate]
