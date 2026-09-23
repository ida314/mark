"""Working-memory tools: the agent's scratchpad for the task in front of it.

Session 7b. These are the only model-facing door into the working bucket, and they are
deliberately thin - the handle they use is built by the turn (`agent/loop.py`) and carries
the scope, so there is no scope argument for a model to pass and no way to name another
agent's. What crosses that boundary is a result or a structured message, not a tool call.

Nothing here writes memory. A note is run-scoped execution state that disappears when the
task scope ends; turning one into something durable is classification and promotion, which
is a later pass's and goes through the review gate like everything else.
"""

from __future__ import annotations

from ..agent.working_memory import WorkingMemory, WorkingMemoryError
from .base import Tool, ToolContext, ToolResult, obj, required, tool
from .effects import IDEMPOTENT_WRITE, READ

# What a handler says when the turn gave it no working memory to write to. It is an error
# and not an empty scratchpad: "there is nowhere to put this" and "there is nothing here
# yet" are different answers, and the second one quietly loses what the model was told to
# keep. Reachable only off the turn path - `policy/replay.execute_approved` runs a queued
# call long after its turn ended, and that call has no run to be scoped to.
NO_SCOPE = (
    "working memory is not available here: this call is not running inside a turn, so it "
    "has no task scope to write to. Nothing was stored."
)


def _handle(ctx: ToolContext) -> WorkingMemory | None:
    handle = ctx.extra.get("working_memory")
    return handle if isinstance(handle, WorkingMemory) else None


@tool(
    "working_memory_note",
    "Keep a short note for the rest of this task: a finding, a decision, something to come "
    "back to. Private to you, and discarded when the task ends.",
    required(
        obj(
            key={
                "type": "string",
                "description": "A short label. Noting the same label again replaces it.",
            },
            note={"type": "string", "description": "The note itself, in a sentence or two."},
        ),
        "key",
        "note",
    ),
    tags=("memory", "core"),
    # `read` risk, `idempotent_write` effect class, and the two are answering different
    # questions. Nothing leaves this process and nothing durable is touched, so there is no
    # reason to ask the user first - denying a scratch note at `observe` autonomy would
    # prompt about a danger that is not there. But the note *is* journaled and does survive
    # a crash within its run, unlike `tool_search`'s turn-scoped `ctx.extra`, so `read`
    # would be a lie to the resume path. Re-running converges: the same key and the same
    # text fold to the same state.
    effect_class=IDEMPOTENT_WRITE,
)
async def working_memory_note(args: dict, ctx: ToolContext) -> ToolResult:
    handle = _handle(ctx)
    if handle is None:
        return ToolResult(content=NO_SCOPE, ok=False)
    try:
        note = handle.note(
            args["key"], args["note"], tainted=ctx.tainted, private=ctx.private
        )
    except WorkingMemoryError as exc:
        # Surfaced to the model with the reason, because every one of them is something the
        # caller can fix on the next call. Never swallowed into a success.
        return ToolResult(content=f"Not stored: {exc}", ok=False)
    return ToolResult(content=f"Noted under '{note.key}' for the rest of this task.")


@tool(
    "working_memory_list",
    "Read back the notes you have kept for this task.",
    obj(),
    tags=("memory", "core"),
    # Reads this scope's own fold and writes nothing at all.
    effect_class=READ,
)
async def working_memory_list(args: dict, ctx: ToolContext) -> ToolResult:
    handle = _handle(ctx)
    if handle is None:
        return ToolResult(content=NO_SCOPE, ok=False)
    notes = handle.notes()
    if not notes:
        return ToolResult(content="No notes kept for this task yet.")
    lines = [f"- {n.key}: {n.text}" for n in notes]
    # A note written while untrusted content was in context is still untrusted when it is
    # read back. Marking the whole result rather than the line is the conservative reading
    # and the one the executor can act on.
    tainted = any(n.tainted for n in notes)
    return ToolResult(
        content="\n".join(lines),
        trust="untrusted" if tainted else "trusted",
        data={"count": len(notes), "tainted": tainted},
    )


TOOLS: list[Tool] = [working_memory_note, working_memory_list]
