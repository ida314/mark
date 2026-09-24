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

# Session 8c. The memory family to `memory`, and the mail family to `mail`.
#
# Two moves in one session because they are one decision each and neither is the other's:
#
# `memory_search` and `memory_history` go to a role that did not exist as a worker until
# now. `delegate(agent="memory")` used to be the one branch of the delegate tool that was
# not a delegation - it packed retrieval in this process and built no worker - so there was
# nothing for these two to move *to*. 8c makes `memory` a real durable role with a spec, a
# budget and a reporting contract, which is what "reachable through a durable role" has to
# mean if the exit criterion is a test rather than a sentence. The orchestrator still gets
# what it knows: the retrieved context block is built for every turn and is unchanged.
#
# `gmail_search` and `gmail_message` go to `mail`, a role created for them, and the choice
# of role was a safety decision rather than a filing one. The obvious home was `researcher`,
# which already reads things from outside the machine - and that is exactly what it must not
# be. `private-data-no-outward-delegation` exists because a sub-agent starts with a fresh
# session, unaware the caller ever read the mailbox, holding `web_search` and `web_fetch`;
# after 8b the researcher is the *only* holder of those two, so putting the mailbox in it
# would put the user's mail and the open web inside one worker and defeat the interlock in
# the one place it was written to hold. `mail` holds the two read-only Gmail tools and
# nothing else, which is a premise `tests/test_tool_surface_pass8.py` checks rather than a
# property this comment asserts.
#
# Three of the four - everything but `memory_history` - are `always_on`, so this is the
# first session of the pass that shrinks the orchestrator's *permanent* set and not only
# its ceiling: 13 -> 10 permanent, 22 -> 18 permitted. The flags themselves are untouched,
# because `always_on` means "always offered by the registry that holds it" and the registry
# that holds these four is now the role's.
MOVED_TO_ROLE: dict[str, str] = {
    "fs_list": "coder",
    "fs_read": "coder",
    "fs_search": "coder",
    "fs_write": "coder",
    "shell_exec": "coder",
    "web_search": "researcher",
    "web_fetch": "researcher",
    "memory_search": "memory",
    "memory_history": "memory",
    "gmail_search": "mail",
    "gmail_message": "mail",
}


def owner_of(name: str) -> str | None:
    """The durable role a moved tool now belongs to, or `None` if it has not moved."""
    return MOVED_TO_ROLE.get(name)


def orchestrator_surface(names: Iterable[str]) -> set[str]:
    """The names an orchestrator may run, given everything its registry holds."""
    return {name for name in names if name not in MOVED_TO_ROLE}
