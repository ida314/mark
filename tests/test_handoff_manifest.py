"""The manifest: what a handoff dropped, named item by item with a ref.

Session 5c, and half of one mechanism - Session 5b shipped a successor that was told
its predecessor's conversation was gone, and then found that the same code on the same task
answered a question about that material confidently and wrongly on one run and refused on
the next. The reading taken from that, and Dylan's ruling at the boundary, is that a refusal
needs something to attach to: a list of what is missing, with refs, and a way to fetch one.

So the failures worth testing here are the ones where the mechanism *looks* right:

* **A manifest that describes rather than excerpts.** The one-line description is the first
  characters of the item. If it were a summary it would be a claim about material the reader
  cannot check without doing the fetch the manifest exists to make possible - the same
  laundering channel as a summarised synthetic message, one layer further out.
  (The tool that resolves a ref is session 5d and is tested in
  `tests/test_handoff_lookup.py`; what is here is the list and what the successor is told
  about it.)
* **A manifest that lists only what the handoff replaced.** Most of what a turn learns is in
  its tool results, and tool results are never replayed into any later prompt. A manifest
  that left them out would be a list of the wrong losses.
* **A version-1 object read as though it had an empty manifest.** "Nothing was dropped" and
  "what was dropped is not recoverable" must not reach the successor as the same sentence.
"""

from __future__ import annotations

import pytest

from agentd.agent import handoff as handoff_mod
from agentd.agent.handoff import DroppedItem, Handoff, HandoffError
from agentd.agent.loop import AgentLoop, Session
from agentd.db import repo_archive
from agentd.db.repo_archive import RawEvent
from agentd.llm.fake import FakeProvider
from agentd.policy.approvals import AutoApprover
from agentd.policy.engine import engine_from_config
from agentd.tools.base import Tool, ToolResult, obj
from agentd.tools.registry import Registry

PASTE = "the ingest window sweep, in detail. " * 400  # ~14,000 characters


def draft(**over) -> handoff_mod.HandoffDraft:
    body = {
        "task": "Find the tool cap",
        "user_intent": "cite the file and line",
        "current_state": "read two files, answered",
        "unresolved_questions": ["nothing open"],
        "next_actions": ["wait for the user"],
    }
    body.update(over)
    return handoff_mod.HandoffDraft(**body)


def built(*, manifest=(), watermark: int | None = 41, **over) -> Handoff:
    return handoff_mod.build(
        draft(**over),
        run_id="run-1",
        session_id="s1",
        reason=handoff_mod.REASON_THRESHOLD,
        watermark=watermark,
        source={"messages_read": 9, "dropped_messages": 2, "dropped_chars": 400},
        dropped_manifest=manifest,
    )


def item(ref: str = "msg:7", **over) -> DroppedItem:
    body = {
        "ref": ref, "kind": "tool_result:fs_read", "description": "def select(self",
        "chars": 8000,
    }
    body.update(over)
    return DroppedItem(**body)


async def _seed(session_id, *rows: tuple[str, str, str]) -> list[int]:
    """`(kind, actor, content)` in order, returning the archive ids they landed on."""
    for kind, actor, content in rows:
        await repo_archive.append_event(
            RawEvent(
                kind=kind, actor=actor, content=content, session_id=session_id,
                trust="untrusted" if kind == "tool_result" and "web" in actor else "trusted",
                payload={"private": True} if "gmail" in actor else {},
            )
        )
    return [
        int(r["id"]) for r in await repo_archive.events_for_session(session_id, limit=100)
    ]


# --- the manifest ------------------------------------------------------------


async def test_a_manifest_entry_is_an_excerpt_of_the_item_and_not_a_summary_of_it(cfg):
    """The description has to be checkable against the item, because the whole point of the
    field is to let a reader decide whether to fetch something it cannot see. A summary
    here would be a model's claim about material nobody can verify without the fetch."""
    session = await Session.create("test")
    await _seed(session.id, ("user_message", "user", PASTE))
    rows = await repo_archive.manifest_rows(
        session.id, upto_id=10**9, watermark=10**9, excerpt_chars=40, limit=10
    )
    entries = handoff_mod.manifest(rows)
    assert len(entries) == 1
    assert PASTE.startswith(entries[0].description)
    assert entries[0].chars == len(PASTE)


async def test_a_tool_result_is_in_the_manifest_although_it_was_never_in_the_conversation(
    cfg,
):
    """The distinction the manifest exists to get right. A tool result is not replayed into
    any later prompt and never was, so a successor has no more access to it than to a
    message the handoff replaced - and it is usually most of what the turn learned."""
    session = await Session.create("test")
    await _seed(
        session.id,
        ("user_message", "user", "where is the cap"),
        ("tool_result", "tool:fs_read", "TOP_K = 8"),
        ("assistant_message", "main", "registry.py:20"),
    )
    rows = await repo_archive.manifest_rows(
        session.id, upto_id=10**9, watermark=10**9, excerpt_chars=40, limit=10
    )
    kinds = [e.kind for e in handoff_mod.manifest(rows)]
    assert "tool_result:fs_read" in kinds
    assert kinds == ["user_message", "tool_result:fs_read", "assistant_message"]


async def test_a_message_the_successor_still_has_is_not_offered_as_something_it_lost(cfg):
    """Above the watermark is carried verbatim. Listing it would invite a fetch of what the
    reader is already holding, which is a step spent re-reading its own prompt."""
    session = await Session.create("test")
    ids = await _seed(
        session.id,
        ("user_message", "user", "first"),
        ("assistant_message", "main", "second"),
        ("user_message", "user", "third, still carried"),
    )
    rows = await repo_archive.manifest_rows(
        session.id, upto_id=ids[-1], watermark=ids[1], excerpt_chars=40, limit=10
    )
    descriptions = [e.description for e in handoff_mod.manifest(rows)]
    assert descriptions == ["first", "second"]


async def test_a_tool_result_below_the_watermark_is_listed_even_with_nothing_dropped(cfg):
    """The `watermark = NULL` case: a conversation short enough to carry whole still ran
    tools, and those results are gone from the successor's view all the same."""
    session = await Session.create("test")
    ids = await _seed(
        session.id,
        ("user_message", "user", "where is the cap"),
        ("tool_result", "tool:fs_read", "TOP_K = 8"),
    )
    rows = await repo_archive.manifest_rows(
        session.id, upto_id=ids[-1], watermark=None, excerpt_chars=40, limit=10
    )
    entries = handoff_mod.manifest(rows)
    assert [e.kind for e in entries] == ["tool_result:fs_read"]


async def test_tool_output_above_the_watermark_is_still_listed(cfg):
    """The asymmetry the two arguments exist for, and the bug the first version had.

    A conversation message above the watermark is carried, so it is not listed. A tool
    result above the watermark is *not* carried - nothing replays one, ever - so it is. The
    archive lays a turn down as user message, then results, then the answer, which means a
    watermark below the last user message puts every result that turn produced above it.
    Reading the watermark as "the line for everything" dropped exactly the rows a successor
    most needs named, and left a manifest that looked healthy.
    """
    session = await Session.create("test")
    ids = await _seed(
        session.id,
        ("user_message", "user", "first, dropped"),
        ("user_message", "user", "second, carried"),
        ("tool_result", "tool:fs_read", "TOP_K = 8, and not carried either way"),
        ("assistant_message", "main", "third, carried"),
    )
    rows = await repo_archive.manifest_rows(
        session.id, upto_id=ids[-1], watermark=ids[0], excerpt_chars=60, limit=10
    )
    listed = {e.kind: e.description for e in handoff_mod.manifest(rows)}
    assert listed["user_message"] == "first, dropped"
    assert "tool_result:fs_read" in listed
    assert "second, carried" not in listed.values()


async def test_a_private_tool_result_is_listed_and_never_excerpted(cfg):
    """Reading mail closes the outside world for the rest of a conversation. The successor
    is still told the mail was read - not knowing would let it conclude the mailbox was
    never opened - but the text does not travel in the manifest, and the lookup refuses it
    too. Listing without excerpting is the only shape that says both true things."""
    session = await Session.create("test")
    await _seed(session.id, ("tool_result", "tool:gmail_message", "Dear Dylan, your rent"))
    rows = await repo_archive.manifest_rows(
        session.id, upto_id=10**9, watermark=10**9, excerpt_chars=100, limit=10
    )
    entry = handoff_mod.manifest(rows)[0]
    assert entry.private is True
    assert "Dylan" not in entry.description
    assert "private" in entry.description


def test_a_ref_round_trips_and_a_ref_that_is_not_one_is_an_answer_rather_than_a_raise():
    """`parse_ref` is called with a string the model typed. "That is not a ref I know" is
    something to tell the model; an exception is something that ends its turn."""
    assert handoff_mod.parse_ref(handoff_mod.ref_for(4812)) == 4812
    assert handoff_mod.parse_ref("4812") is None
    assert handoff_mod.parse_ref("msg:not-a-number") is None
    assert handoff_mod.parse_ref("") is None


def test_only_a_ref_in_the_manifest_resolves_to_anything():
    """The whole authorisation check for the lookup, asserted where it lives. Without it
    the tool is an archive reader that takes an integer, which is a different thing from
    the one that was argued for."""
    handoff = built(manifest=(item("msg:7"), item("msg:9")))
    assert handoff.item("msg:7") is not None
    assert handoff.item("msg:8") is None
    assert handoff.item(" msg:9 ") is not None


# --- what the successor is told ----------------------------------------------


def test_the_successor_is_given_the_refs_and_told_it_must_not_answer_from_the_list():
    handoff = built(manifest=(item("msg:7"), item("msg:9", kind="user_message")))
    block = handoff_mod.render(handoff, lookup_tool="handoff_lookup")
    assert "msg:7" in block and "msg:9" in block
    assert "handoff_lookup" in block
    assert "never" in block or "do not" in block


def test_a_successor_with_no_way_to_fetch_is_told_to_say_so_rather_than_to_call_something():
    """A turn where the lookup is not on the tool list. Telling a model to fetch a ref with
    a tool it has not been given spends a step discovering the call does not exist."""
    handoff = built(manifest=(item("msg:7"),))
    block = handoff_mod.render(handoff)
    assert "msg:7" in block
    assert "handoff_lookup" not in block
    assert handoff_mod.MANIFEST_NO_LOOKUP in block


def test_a_handoff_that_dropped_nothing_renders_no_manifest_at_all():
    """Empty is a real answer, and the block for it is silence rather than a heading with
    nothing under it."""
    block = handoff_mod.render(built(manifest=()))
    assert handoff_mod.MANIFEST_HEADING not in block


def test_an_older_object_is_read_and_says_what_it_cannot_offer():
    """5a's open question 2, now a real migration rather than a rule nobody had exercised.
    A version 1 handoff has no manifest and cannot grow one, and the successor must not
    read that as "nothing was dropped" - those lead to opposite behaviour."""
    stored = built(manifest=(item(),)).as_dict()
    stored["handoff_schema"] = 1
    stored.pop("dropped_manifest")

    older = Handoff.from_dict(stored)
    assert older.dropped_manifest == ()
    assert "handoff_schema 1" in older.source["manifest"]
    assert older.source["manifest"] in handoff_mod.render(older)


def test_an_object_from_a_build_that_does_not_exist_yet_is_still_refused():
    """The half of the rule that did not change. Reading unknown fields as absent ones is
    the failure the version exists to prevent, and "newer" is exactly that case."""
    stored = built().as_dict()
    stored["handoff_schema"] = 99
    with pytest.raises(HandoffError, match="handoff_schema"):
        Handoff.from_dict(stored)


def test_a_manifest_longer_than_the_block_says_how_many_it_left_out():
    """Trailing off is how a list becomes a claim that it is complete."""
    handoff = built(manifest=tuple(item(f"msg:{i}") for i in range(10)))
    block = handoff_mod.manifest_block(handoff, limit=3)
    assert "msg:9" in block and "msg:0" not in block
    assert "7 further items" in block


async def test_the_manifest_covers_the_tool_output_of_the_turn_that_handed_off(cfg):
    """End to end, against the shape the confabulation eval actually runs: a turn reads a
    file, hands off, and the successor's manifest names the file body it will never see.
    A manifest built only from the conversation would list two short messages and none of
    what the turn actually learned."""

    async def handler(args, ctx):
        return ToolResult(content="ALWAYS_EXPOSE_LIMIT = 20\n" + "x" * 4000)

    reader = Tool(
        name="reads_a_file", description="returns a lot", parameters=obj(path={}),
        handler=handler, effect_class="read", risk="read",
    )
    small = cfg.model_copy(
        update={"handoff": cfg.handoff.model_copy(
            # Forced: this conversation is two short messages and would never cross the
            # real 24,000 ceiling. What is under test is the manifest's contents, not the
            # threshold, which `tests/test_context_budget.py` owns.
            update={"ceiling_tokens": 1000, "threshold_tokens": 999}
        )}
    )
    provider = FakeProvider(
        turns=[[("reads_a_file", {"path": "registry.py"})], "The cap is 20."],
        json_results=[draft().model_dump()],
    )
    loop = AgentLoop(
        cfg=small, registry=Registry(), engine=engine_from_config(cfg),
        approver=AutoApprover(True), provider=provider,
    )
    loop.registry.add(reader)
    session = await Session.create("test")
    async for _ in loop.run_turn(session, "where is the cap"):
        pass

    assert session.handoff is not None
    kinds = [e.kind for e in session.handoff.dropped_manifest]
    assert "tool_result:reads_a_file" in kinds
    body = next(
        e for e in session.handoff.dropped_manifest if e.kind == "tool_result:reads_a_file"
    )
    assert body.description.startswith("ALWAYS_EXPOSE_LIMIT = 20")


async def test_the_manifest_still_names_the_tool_output_when_the_watermark_cuts_below_it(
    cfg,
):
    """The same end-to-end shape, with a watermark actually set - which is the case the
    confabulation eval runs and the one the first version of this code got wrong.

    The archive lays a turn down as user message, then tool results, then the answer. With
    a carry window small enough to draw a watermark inside the conversation, every result
    that turn produced sits *above* it. Reading the watermark as the manifest's upper bound
    therefore dropped exactly the rows a successor most needs named, while leaving a
    manifest that still listed things and still looked healthy.

    A mutation that restores that reading survives every test that asserts on
    `manifest_rows` directly, because those pass the bound in themselves. This one fails.
    """

    async def handler(args, ctx):
        return ToolResult(content="ALWAYS_EXPOSE_LIMIT = 20\n" + "x" * 4000)

    reader = Tool(
        name="reads_a_file", description="returns a lot", parameters=obj(path={}),
        handler=handler, effect_class="read", risk="read",
    )
    small = cfg.model_copy(
        update={"handoff": cfg.handoff.model_copy(
            update={
                "ceiling_tokens": 1000, "threshold_tokens": 999,
                # Small enough that the watermark lands inside the conversation rather
                # than below all of it, which is what puts the results above the line.
                "carry_tokens": 40, "carry_messages": 2,
            }
        )}
    )
    provider = FakeProvider(
        turns=[
            "Ask me about the repository.",
            [("reads_a_file", {"path": "registry.py"})],
            "The cap is 20.",
        ],
        json_results=[draft().model_dump(), draft().model_dump()],
    )
    loop = AgentLoop(
        cfg=small, registry=Registry(), engine=engine_from_config(cfg),
        approver=AutoApprover(True), provider=provider,
    )
    loop.registry.add(reader)
    session = await Session.create("test")
    async for _ in loop.run_turn(session, "hello, I have a question coming"):
        pass
    async for _ in loop.run_turn(session, "where is the cap"):
        pass

    assert session.handoff is not None
    assert session.handoff.watermark is not None, (
        "the fixture needs a watermark for this to be the case under test"
    )
    kinds = [e.kind for e in session.handoff.dropped_manifest]
    assert "tool_result:reads_a_file" in kinds, (
        f"the turn's own tool output fell outside the manifest; listed: {kinds}"
    )
