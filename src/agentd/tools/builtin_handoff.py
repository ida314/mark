"""The lookup: one ref from a handoff's manifest, one bounded excerpt back.

Session 5d. This is the half of Dylan's 5b ruling that changes the behaviour rather than
nudging it - 5c ships the manifest, which tells a successor what it does not have, and this
is the only thing it can do about that other than refuse. His reading of the pass file's
second *Must not*, which is what makes this permissible and is recorded verbatim in
`docs/records/pass-05-outcome.md`:

> it forbids the runtime restoring the old context wholesale as a fallback. It does not
> forbid the successor requesting a specific named item. The line is who chooses what comes
> back and how much.

So every property of this tool is on the far side of that line, and each one is load
bearing:

    takes        exactly one manifest ref, and nothing else
    returns      a bounded excerpt - `[handoff] lookup_max_chars`
    cannot       take a free-text query: there is no search here, only resolution
    cannot       return "everything": the argument is one ref, not a list and not a range
    invoked by   the model, on its own turn. The runtime never calls it, and in particular
                 never calls it at orchestrator start, which is what would make it the
                 wholesale restore wearing a tool's name
    journaled    every call, like any other tool call - `tool_requested` / `tool_finished`,
                 with no new event type, so the Pass 10 question ("do successors just fetch
                 everything?") is a query over the journal rather than a second mechanism

**The authorisation is the manifest, not the archive.** A ref is resolvable only if it is
in the handoff in force on this turn. That is why `Handoff.item` exists: without it this
would be a tool that reads any row of the archive by integer, which is a different and much
larger thing than the one that was argued for.

**Two refusals that are not bugs.** A private tool result - mail - is listed in the manifest
and is *not* fetchable. Reading mail closes the outside world for the rest of a conversation
(`ToolContext.private`, the interlock), and a door that returns the same text one turn later
without the tool that closed it is the interlock defeated in one hop. The successor is told
the item exists and is told to ask the original tool. And an item whose original result was
untrusted comes back untrusted, wrapped by the executor exactly as it was the first time:
a web page does not become trustworthy by being read out of an archive.
"""

from __future__ import annotations

from ..config import get_config
from ..db import repo_archive
from .base import Tool, ToolContext, ToolResult, obj, required, tool
from .effects import READ

LOOKUP = "handoff_lookup"

NO_HANDOFF = (
    "There is no handoff in force on this turn, so there is no manifest and no ref to "
    "resolve. Nothing in this conversation has been compressed away."
)

NOT_IN_MANIFEST = (
    "{ref!r} is not a ref in this conversation's manifest. Only the refs listed in the "
    "handoff block can be fetched - this is not a search, and there is no way to ask for "
    "anything else. Check the ref and try again, or tell the user you do not have it."
)

PRIVATE_REFUSED = (
    "{ref} is a private tool result ({kind}, {chars:,} characters) and cannot be fetched "
    "this way. Reading the user's private data is what closed the outside world for this "
    "conversation, and handing the same text back through a manifest would undo that "
    "without the tool that did it. If you need it, call the tool that produced it again, "
    "or tell the user it is out of reach here."
)

GONE = (
    "{ref} is in the manifest and is no longer in the archive. That should not happen - "
    "the archive is append-only - so report it rather than working around it."
)

TRUNCATED = (
    "\n\n[... {withheld:,} further characters of this item were not returned. This is a "
    "bounded excerpt of a {total:,}-character item; fetching it again returns the same "
    "excerpt, not the rest.]"
)

HEADER = "Manifest item {ref} ({kind}, {total:,} characters), fetched from the archive:"


@tool(
    LOOKUP,
    "Fetch one item from this conversation's handoff manifest by its ref (for example "
    "msg:4812), and get back a bounded excerpt of it. One ref per call. This is not a "
    "search: only refs listed in the handoff block can be fetched.",
    required(
        obj(
            ref={
                "type": "string",
                "description": "A ref exactly as it appears in the handoff manifest.",
            }
        ),
        "ref",
    ),
    # read: it selects one archive row. Nothing outside changes, so a crash between the
    # intent and the result leaves nothing to reconcile - which is also why it gets no
    # ledger row and can never be surfaced to the user as an uncertain effect.
    effect_class=READ,
    tags=("core", "handoff"),
    always_on=True,
)
async def handoff_lookup(args: dict, ctx: ToolContext) -> ToolResult:
    handoff = ctx.extra.get("handoff")
    if handoff is None:
        return ToolResult(content=NO_HANDOFF, ok=False)

    ref = str(args.get("ref") or "").strip()
    item = handoff.item(ref)
    if item is None:
        return ToolResult(content=NOT_IN_MANIFEST.format(ref=ref), ok=False)
    if item.private:
        return ToolResult(
            content=PRIVATE_REFUSED.format(ref=item.ref, kind=item.kind, chars=item.chars),
            ok=False,
        )

    archive_id = item.archive_id
    row = (
        None
        if archive_id is None or ctx.session_id is None
        else await repo_archive.archived_item(ctx.session_id, archive_id)
    )
    if row is None:
        return ToolResult(content=GONE.format(ref=item.ref), ok=False)

    body = row.get("content") or ""
    limit = get_config().handoff.lookup_max_chars
    excerpt = body[:limit]
    head = HEADER.format(ref=item.ref, kind=item.kind, total=len(body))
    text = f"{head}\n\n{excerpt}"
    if len(body) > limit:
        text += TRUNCATED.format(withheld=len(body) - limit, total=len(body))
    return ToolResult(
        content=text,
        # Whatever it was when it was first returned. A web page read back out of the
        # archive is the same stranger's text it was an hour ago, and the executor's
        # quarantine wrapper keys off exactly this field.
        trust=str(row.get("trust") or "trusted"),
        data={"ref": item.ref, "kind": item.kind, "chars": len(body)},
    )


TOOLS: list[Tool] = [handoff_lookup]

__all__ = ["LOOKUP", "TOOLS", "handoff_lookup"]
