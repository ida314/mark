"""Which tool families have left the orchestrator, and which durable role owns each.

Pass 8 moves capability families off the orchestrator one session at a time, and this is the
one place that records a move. It is a *subtraction* from whatever the registry holds rather
than a hand-written list of what the orchestrator keeps: a positive list would silently lock
out every tool registered after it was written - an MCP server's, a test's probe - and the
question this module answers is only ever "has this family moved", never "is this tool
allowed to exist".

The mapping is name -> durable role, not name -> True, because the name of the owner is what
makes the refusal useful. A model told "that is not yours" spends its next step looking for
another route; a model told "that is the coder's, delegate the work" has somewhere to go. The
owner string is checked against the real role registry by
`tests/test_tool_surface_pass8.py`, so a family cannot be moved to a role that does not exist
or to one whose spec does not actually grant the tool - which is the pass's exit criterion
"every moved tool reachable through a durable role", as a test rather than as a claim.

What this does *not* do is decide what a turn is shown. `Registry.select` still picks from
what is left, and `always_on` still decides what it always picks. This is the ceiling: the
names the orchestrator may run at all, by any of the four doors into a turn.
"""

from __future__ import annotations

from collections.abc import Iterable

# Session 8a. The filesystem and shell family, to the `coder` role.
#
# The orchestrator may still answer a coding question that needs no repository - it has no
# tools for that and never did. What it may no longer do is read, search, write or run
# anything on the user's machine itself; that whole workflow goes to one worker that owns it
# end to end, which is the architecture's "subagents complete an entire coherent workflow"
# rather than the orchestrator narrating a file at a time into its own context.

# Session 8b. The web family, to the `researcher` role.
#
# The orchestrator supplies the research objective, not the individual searches. A turn that
# keeps `web_search` reads three pages into its own context to answer one question, and the
# context it spends is the context the conversation runs in; a `researcher` delegation reads
# the same three pages into a context that is discarded and returns the compressed result.
# That is the asymmetry the move is for, and it is the reason the role's reporting contract
# says what it found rather than what it searched.
#
# Neither name was `always_on`, so this move does not change the permanent set. It lowers the
# ceiling - what the orchestrator may run by any of the four doors - from 24 to 22.
MOVED_TO_ROLE: dict[str, str] = {
    "fs_list": "coder",
    "fs_read": "coder",
    "fs_search": "coder",
    "fs_write": "coder",
    "shell_exec": "coder",
    "web_search": "researcher",
    "web_fetch": "researcher",
}


def owner_of(name: str) -> str | None:
    """The durable role a moved tool now belongs to, or `None` if it has not moved."""
    return MOVED_TO_ROLE.get(name)


def orchestrator_surface(names: Iterable[str]) -> set[str]:
    """The names an orchestrator may run, given everything its registry holds."""
    return {name for name in names if name not in MOVED_TO_ROLE}
