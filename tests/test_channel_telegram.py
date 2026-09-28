"""Telegram as a channel: who may talk to it, and what it does with what they say.

The allowlist tests are the ones that matter. Everything else here is plumbing; the
allowlist is the only thing standing between a stranger who guessed a bot username and an
agent holding this person's mail, calendar and filesystem.
"""

from __future__ import annotations

import httpx
import pytest

from agentd.agent.stream import Answer
from agentd.daemon import telegram as tg
from agentd.db import repo_agenda, repo_ops
from agentd.db.pool import fetch_all
from agentd.ids import uuid7
from agentd.journal.render import brief_args
from agentd.journal.runtime import RunJournal


@pytest.fixture
def bot(cfg, monkeypatch, tmp_path):
    from agentd import secrets as vault

    monkeypatch.setattr(vault, "SECRETS_FILE", tmp_path / "secrets.toml")
    vault.put("telegram/bot", tmp_path / "secrets.toml", token="123456:test-token")
    cfg.telegram.enabled = True
    cfg.telegram.allowed_chat_ids = [4242]
    return cfg


class JournalingLoop:
    """A stand-in for `AgentLoop` that journals its tool calls and yields prose.

    The shape is the point. Since session 2c the real loop tells a channel nothing about
    tools: the progress message is a subscriber to the run journal, like the REPL and like a
    frontend in another process. A fake that yielded tool objects would be exercising an
    event path that no longer exists.
    """

    def __init__(self, calls: tuple[tuple[str, dict, str], ...] = (), answer: str = "", **kw):
        self.calls = calls
        self.answer = answer
        self.kw = kw
        self.kwargs: dict = {}

    async def run_turn(self, session, text, *, run_id=None, **kwargs):
        self.kwargs = kwargs
        rj = RunJournal.open(run_id or str(uuid7()))
        for index, (name, args, state) in enumerate(self.calls, start=1):
            sid = rj.step_id(index)
            call_id = f"c{index}"
            rj.emit(
                "tool_requested",
                {"call_id": call_id, "name": name, "args": args, "visible": True, "known": True},
                step_id=sid,
            )
            rj.emit("tool_started", {"call_id": call_id, "name": name}, step_id=sid)
            if state == "ok":
                rj.emit(
                    "tool_finished",
                    {"call_id": call_id, "name": name, "duration_ms": 1,
                     "result_chars": 0, "trust": "trusted"},
                    step_id=sid,
                )
            else:
                rj.emit(
                    "tool_failed",
                    {"call_id": call_id, "name": name, "duration_ms": 1, "error": "no",
                     "denied": state == "denied", "invalid_args": False},
                    step_id=sid,
                )
        yield Answer(turn_id=str(uuid7()), text=self.answer.format(text=text))


class Wire:
    """Records what we said to Telegram, and answers the way Telegram would."""

    def __init__(self, results: dict | None = None) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.results = results or {}

    def client(self) -> httpx.AsyncClient:
        def handler(request: httpx.Request) -> httpx.Response:
            import json

            method = request.url.path.rsplit("/", 1)[-1]
            self.calls.append((method, json.loads(request.content or b"{}")))
            return httpx.Response(200, json={"ok": True, "result": self.results.get(method, {})})

        return httpx.AsyncClient(transport=httpx.MockTransport(handler))

    def said(self) -> list[str]:
        return [args.get("text", "") for method, args in self.calls if method == "sendMessage"]


# --- splitting ---------------------------------------------------------------


def test_a_long_answer_is_split_not_truncated():
    parts = tg.chunk("a" * 50 + "\n\n" + "b" * 50, 60)
    assert len(parts) == 2
    assert parts[0].startswith("a") and parts[1].startswith("b")
    assert "".join(parts).count("a") == 50  # nothing lost


def test_a_paragraph_break_is_preferred_to_a_hard_cut():
    text = "first para\n\n" + "x" * 100
    parts = tg.chunk(text, 60)
    assert parts[0] == "first para"


def test_prose_with_no_breaks_still_gets_cut():
    parts = tg.chunk("x" * 200, 60)
    assert [len(p) for p in parts] == [60, 60, 60, 20]


def test_an_empty_answer_still_says_something():
    """Telegram rejects an empty message, and silence reads as a crash."""
    assert tg.chunk("   ", 100) == ["(no answer)"]


# --- setup -------------------------------------------------------------------


def test_without_a_token_it_says_what_to_run(cfg, monkeypatch, tmp_path):
    from agentd import secrets as vault

    monkeypatch.setattr(vault, "SECRETS_FILE", tmp_path / "secrets.toml")
    cfg.telegram.enabled = True
    assert "agent secrets set telegram/bot token" in tg.configured(cfg)


def test_an_empty_allowlist_is_a_setup_error_not_an_open_door(bot):
    """The failure mode this prevents: reading an empty list as "no restriction"."""
    bot.telegram.allowed_chat_ids = []
    assert "allowed_chat_ids" in tg.configured(bot)


def test_a_configured_bot_is_ready(bot):
    assert tg.configured(bot) is None


# --- the allowlist -----------------------------------------------------------


async def test_a_stranger_is_dropped_audited_and_reported_once(bot):
    seen: set[int] = set()
    await tg.refuse_stranger(99, seen)
    await tg.refuse_stranger(99, seen)

    rows = await fetch_all("SELECT * FROM actions WHERE name = 'rejected'")
    assert len(rows) == 2, "every attempt is audited"

    notes = await repo_agenda.list_notifications()
    telegram_notes = [n for n in notes if n["source"] == "channel:telegram"]
    assert len(telegram_notes) == 1, "but you are only told once"
    assert "99" in telegram_notes[0]["body"]


async def test_a_stranger_gets_no_reply_at_all(bot):
    """Answering a stranger — even to refuse them — confirms the bot exists and is alive."""
    wire = Wire()
    async with wire.client():
        await tg.refuse_stranger(99, set())
    assert wire.said() == []


# --- commands ----------------------------------------------------------------


async def test_whoami_tells_you_the_id_you_need(bot):
    wire = Wire()
    async with wire.client() as client:
        conv = tg.Conversation(bot)
        handled = await tg.handle_command(bot, client, conv, 4242, "/whoami")
    assert handled and "4242" in wire.said()[0]


async def test_new_starts_a_different_session(bot):
    wire = Wire()
    async with wire.client() as client:
        conv = tg.Conversation(bot)
        first = await conv.session_for(4242)
        await tg.handle_command(bot, client, conv, 4242, "/new")
        second = await conv.session_for(4242)
    assert first.id != second.id


async def test_an_unknown_command_is_not_swallowed(bot):
    """It has to fall through to the model, or "/etc is where configs live" becomes a
    silent no-op."""
    wire = Wire()
    async with wire.client() as client:
        handled = await tg.handle_command(bot, client, tg.Conversation(bot), 4242, "/etc")
    assert handled is False


async def test_approvals_lists_what_is_waiting(bot):
    approval_id = await repo_ops.queue_approval(
        tool_name="web_fetch", args={"url": "https://example.com"}, risk="external",
        policy_rule="test", reason="because", origin="telegram",
    )
    wire = Wire()
    async with wire.client() as client:
        await tg.handle_command(bot, client, tg.Conversation(bot), 4242, "/approvals")
    assert str(approval_id) in wire.said()[0]


async def test_approving_a_stale_approval_refuses(bot):
    """The argument hash is consent to *those* arguments, not to whatever the row says by
    the time somebody reads it on a phone."""
    approval_id = await repo_ops.queue_approval(
        tool_name="web_fetch", args={"url": "https://example.com"}, risk="external",
        policy_rule="test", reason="because", origin="telegram",
    )
    async with pool_patch(approval_id):
        answer = await tg.approve(str(approval_id))
    assert "changed since it was queued" in answer


async def test_approve_needs_an_id(bot):
    assert "Which one" in await tg.approve("")
    assert "not an approval id" in await tg.approve("bananas")


# --- a real turn -------------------------------------------------------------


async def test_a_message_becomes_a_turn_and_an_answer(bot, monkeypatch):
    loops: list[JournalingLoop] = []

    def build(**kw) -> JournalingLoop:
        loops.append(JournalingLoop(answer="you said: {text}", **kw))
        return loops[-1]

    monkeypatch.setattr(tg, "AgentLoop", build)
    wire = Wire()
    async with wire.client() as client:
        await tg.handle_message(
            bot, client, tg.Conversation(bot),
            {"chat": {"id": 4242}, "text": "what is on my calendar?"},
        )
    assert wire.said() == ["you said: what is on my calendar?"]
    assert ("sendChatAction", {"chat_id": 4242, "action": "typing"}) in wire.calls
    assert loops[0].kwargs["origin"] == "telegram"


async def test_a_denied_tool_is_explained_rather_than_swallowed(bot, monkeypatch):
    """A turn that produced nothing but a policy denial must not answer with silence."""
    monkeypatch.setattr(
        tg, "AgentLoop",
        lambda **kw: JournalingLoop(calls=(("web_fetch", {}, "denied"),), answer=""),
    )
    wire = Wire()
    async with wire.client() as client:
        await tg.handle_message(
            bot, client, tg.Conversation(bot), {"chat": {"id": 4242}, "text": "look it up"}
        )
    # The progress line is sent first, so the explanation is the last message, not the
    # first. Assert on the answer itself rather than on ordering.
    answer = wire.said()[-1]
    assert "web_fetch" in answer and "/new" in answer


async def test_a_photo_with_no_caption_says_so(bot):
    wire = Wire()
    async with wire.client() as client:
        await tg.handle_message(bot, client, tg.Conversation(bot), {"chat": {"id": 4242}})
    assert "only read text" in wire.said()[0]


# --- the wire ----------------------------------------------------------------


async def test_a_rejected_token_is_not_a_transient_failure(bot):
    """It will never fix itself, so it must stop the channel rather than retry forever."""
    def handler(request):
        return httpx.Response(401, json={"ok": False, "description": "Unauthorized"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(PermissionError):
            await tg.call(bot, client, "getMe")


async def test_an_api_error_is_reported_with_its_reason(bot):
    def handler(request):
        return httpx.Response(200, json={"ok": False, "description": "chat not found"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(RuntimeError, match="chat not found"):
            await tg.call(bot, client, "sendMessage", chat_id=1, text="hi")


async def test_the_token_never_reaches_the_query_string(bot):
    """It goes in the path, which is Telegram's design, and the body carries the rest — so
    an error message quoting params cannot leak it."""
    captured = {}

    def handler(request):
        captured["url"] = str(request.url)
        return httpx.Response(200, json={"ok": True, "result": {}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await tg.call(bot, client, "sendMessage", chat_id=1, text="hi")
    assert "?" not in captured["url"]


import contextlib  # noqa: E402


@contextlib.asynccontextmanager
async def pool_patch(approval_id):
    """Corrupt the stored hash so the staleness check has something to catch."""
    from agentd.db.pool import connection

    async with connection() as conn:
        await conn.execute(
            "UPDATE approvals SET args_sha256 = 'stale' WHERE id = %s", (approval_id,)
        )
    yield



async def test_a_rejected_token_never_reaches_a_traceback(bot, monkeypatch, capsys):
    """A 401 is the one failure that cannot fix itself, so it has to arrive as an
    instruction. It used to propagate out of the typer command as a raw traceback, which
    tells a person nothing about @BotFather."""
    from agentd.cli.app import _telegram_check

    real = httpx.AsyncClient

    def rejecting(**kw):
        return real(transport=httpx.MockTransport(
            lambda request: httpx.Response(401, json={"ok": False, "description": "Unauthorized"})
        ))

    monkeypatch.setattr(httpx, "AsyncClient", rejecting)
    assert await _telegram_check(bot) == 1

    out = capsys.readouterr().out
    assert "401" in out and "BotFather" in out
    assert "Traceback" not in out


async def test_a_working_token_reports_the_bot(bot, monkeypatch, capsys):
    from agentd.cli.app import _telegram_check

    real = httpx.AsyncClient

    def ok(**kw):
        return real(transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200, json={"ok": True, "result": {"username": "dylans_agent_bot"}}
            )
        ))

    monkeypatch.setattr(httpx, "AsyncClient", ok)
    assert await _telegram_check(bot) == 0
    assert "dylans_agent_bot" in capsys.readouterr().out


async def test_an_empty_allowlist_is_called_out_even_when_the_token_works(bot, monkeypatch, capsys):
    from agentd.cli.app import _telegram_check

    bot.telegram.allowed_chat_ids = []
    real = httpx.AsyncClient

    def ok(**kw):
        return real(transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"ok": True, "result": {"username": "b"}})
        ))

    monkeypatch.setattr(httpx, "AsyncClient", ok)
    await _telegram_check(bot)
    assert "nobody can talk to it" in capsys.readouterr().out


# --- the allowlist holds people, not the bot ---------------------------------


def test_a_username_in_the_allowlist_is_refused_with_an_explanation():
    """Two mistakes are easy here and the raw parse error names neither: a username instead
    of a numeric id, and the *bot's* identity instead of your own."""
    from pydantic import ValidationError

    from agentd.config import TelegramConfig

    with pytest.raises(ValidationError) as caught:
        TelegramConfig(allowed_chat_ids=["Unsettled0645_bot"])
    message = str(caught.value)
    assert "username, not a chat id" in message
    assert "not the bot's" in message
    assert "agent telegram whoami" in message


def test_negative_ids_are_fine():
    """Telegram gives groups and channels negative ids; only a *username* is the error."""
    from agentd.config import TelegramConfig

    assert TelegramConfig(allowed_chat_ids=[-1001234567890]).allowed_chat_ids == [-1001234567890]


# --- showing what it did -----------------------------------------------------


def test_tool_lines_read_in_the_order_things_happened():
    rendered = tg.render_tools(
        [
            ("gmail_search", "query=is:unread", "ok"),
            ("memory_search", "query=lease", "running"),
            ("web_fetch", "url=https://example.com", "denied"),
        ]
    )
    assert rendered.splitlines() == [
        "✓ gmail_search(query=is:unread)",
        "… memory_search(query=lease)",
        "✗ web_fetch(url=https://example.com) (refused by policy)",
    ]


def test_tool_lines_say_what_the_call_was_for():
    """"Which tools ran" without "on what" is a list of verbs, not an account of what the
    agent did. Same information `agent chat` prints, through the same function."""
    rendered = tg.render_tools([("gmail_search", brief_args({"query": "from:alice"}), "ok")])
    assert rendered == "✓ gmail_search(query=from:alice)"


def test_an_argument_cannot_forge_a_second_tool_line():
    """The reason the arguments go through `brief_args` rather than into the f-string. A
    subject line a stranger chose is a tool argument one search later, and this function
    joins calls with newlines."""
    forged = "lunch?\n✓ fs_write(path=/etc/passwd"
    rendered = tg.render_tools([("gmail_search", brief_args({"query": forged}), "ok")])
    assert len(rendered.splitlines()) == 1
    assert "fs_write" in rendered  # it is shown, as their text, on the one line it belongs on


def test_the_reason_argument_is_not_worth_a_phone_screen():
    assert brief_args({"path": "notes.md", "reason": "the user asked me to"}) == "path=notes.md"


def test_a_long_argument_is_cut_rather_than_wrapped():
    brief = brief_args({"statement": "x" * 200})
    assert brief.endswith("…")
    assert len(brief) <= 120


async def test_a_turn_shows_its_tools_and_edits_one_message(bot, monkeypatch):
    """Edited rather than re-sent: a trail of near-identical messages is what makes a phone
    unusable."""
    monkeypatch.setattr(
        tg, "AgentLoop",
        lambda **kw: JournalingLoop(
            calls=(("gmail_search", {}, "ok"), ("web_fetch", {}, "denied")),
            answer="here you go",
        ),
    )
    wire = Wire(results={"sendMessage": {"message_id": 77}})
    async with wire.client() as client:
        await tg.handle_message(
            bot, client, tg.Conversation(bot), {"chat": {"id": 4242}, "text": "check my mail"}
        )

    edits = [args for method, args in wire.calls if method == "editMessageText"]
    assert edits, "the status message is edited, not re-sent"
    assert all(e["message_id"] == 77 for e in edits), "always the same message"
    assert "gmail_search" in edits[-1]["text"] and "refused by policy" in edits[-1]["text"]
    assert "here you go" in wire.said()


async def test_tools_off_says_nothing_about_them(bot, monkeypatch):
    monkeypatch.setattr(
        tg, "AgentLoop",
        lambda **kw: JournalingLoop(calls=(("gmail_search", {}, "ok"),), answer="done"),
    )
    conv = tg.Conversation(bot)
    conv.verbose[4242] = False
    wire = Wire(results={"sendMessage": {"message_id": 77}})
    async with wire.client() as client:
        await tg.handle_message(bot, client, conv, {"chat": {"id": 4242}, "text": "hi"})

    assert wire.said() == ["done"]
    assert not [m for m, _ in wire.calls if m == "editMessageText"]


async def test_the_tools_command_toggles_per_chat(bot):
    wire = Wire()
    conv = tg.Conversation(bot)
    async with wire.client() as client:
        await tg.handle_command(bot, client, conv, 4242, "/tools off")
        assert conv.show_tools(4242) is False
        assert conv.show_tools(9999) is True, "other chats keep the default"
        await tg.handle_command(bot, client, conv, 4242, "/tools on")
        assert conv.show_tools(4242) is True


async def test_a_failing_status_edit_never_costs_the_answer(bot, monkeypatch):
    """Telegram rate-limits edits. Losing the progress line is a cosmetic problem; losing
    the answer because of it is not."""
    monkeypatch.setattr(
        tg, "AgentLoop",
        lambda **kw: JournalingLoop(calls=(("gmail_search", {}, "ok"),), answer="the answer"),
    )

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        method = request.url.path.rsplit("/", 1)[-1]
        body = json.loads(request.content or b"{}")
        if method == "editMessageText":
            return httpx.Response(429, json={"ok": False, "description": "Too Many Requests"})
        if method == "sendMessage" and body.get("text") == "the answer":
            return httpx.Response(200, json={"ok": True, "result": {"message_id": 9}})
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 77}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await tg.handle_message(
            bot, client, tg.Conversation(bot), {"chat": {"id": 4242}, "text": "hi"}
        )  # must not raise


# --- approvals from the phone ------------------------------------------------
#
# Session 01a0e9f6 (2026-09-28). A Telegram turn was built with a `QueueApprover`: the
# coder's `fs_write` was parked, the model was told "nobody is at the keyboard" while the
# user was reading the reply on their phone, and the queued id never reached them. The person
# is present. They have no keyboard. So the turn asks with buttons and waits.

import asyncio  # noqa: E402
from uuid import UUID  # noqa: E402

from agentd.policy.approvals import ApprovalRequest  # noqa: E402
from agentd.policy.engine import Decision  # noqa: E402


def _request(origin: str = "telegram") -> ApprovalRequest:
    return ApprovalRequest(
        tool_name="fs_write", args={"path": "/tmp/agent-test.md", "content": "hello world"},
        risk="write", decision=Decision(outcome="require_approval", rule_id="risk_matrix:write/assist"),
        reason="the user asked for a test file",
        preview="--- a/agent-test.md\n+++ b/agent-test.md\n+hello world",
        origin=origin,
    )


async def _asked(wire: Wire) -> tuple[dict, UUID]:
    """The approval message once it has been sent, and the id its buttons carry."""
    for _ in range(200):
        for method, args in wire.calls:
            if method == "sendMessage" and "reply_markup" in args:
                data = args["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
                return args, tg.parse_tap(data)[1]
        await asyncio.sleep(0.005)
    raise AssertionError("no approval message was sent")


def _tap(approval_id: UUID, *, approve: bool, text: str = "") -> dict:
    return {
        "id": "q1", "data": f"{'approve' if approve else 'deny'}:{approval_id}",
        "message": {"message_id": 5, "chat": {"id": 4242}, "text": text},
    }


async def test_a_turn_from_the_phone_is_built_with_the_phone_approver(bot, monkeypatch):
    built: list[dict] = []

    def build(**kw) -> JournalingLoop:
        built.append(kw)
        return JournalingLoop(answer="ok")

    monkeypatch.setattr(tg, "AgentLoop", build)
    wire = Wire()
    async with wire.client() as client:
        await tg.handle_message(bot, client, tg.Conversation(bot), {"chat": {"id": 4242}, "text": "hi"})
    assert isinstance(built[0]["approver"], tg.TelegramApprover)


async def test_an_approval_is_asked_with_buttons_and_a_tap_approves_it(bot):
    wire = Wire(results={"sendMessage": {"message_id": 5}})
    async with wire.client() as client:
        conv = tg.Conversation(bot)
        approver = tg.TelegramApprover(bot, client, 4242, conv.taps)
        waiting = asyncio.create_task(approver.request(_request("subagent:coder")))
        asked, approval_id = await _asked(wire)

        assert "fs_write" in asked["text"] and "coder" in asked["text"]
        assert "+hello world" in asked["text"], "the preview is what you are approving"
        assert str(approval_id) in asked["text"]
        assert [b["text"] for b in asked["reply_markup"]["inline_keyboard"][0]] == ["Approve", "Deny"]
        # Queued before it was asked: durable whatever happens to the wait.
        assert (await repo_ops.get_approval(approval_id))["status"] == "pending"

        await tg.handle_tap(bot, client, conv, _tap(approval_id, approve=True, text=asked["text"]))
        result = await asyncio.wait_for(waiting, 2)

    assert result.approved is True and result.decided_by == "user"
    assert (await repo_ops.get_approval(approval_id))["status"] == "approved"
    acks = [a for m, a in wire.calls if m == "answerCallbackQuery"]
    assert acks and acks[0]["callback_query_id"] == "q1"
    edits = [a for m, a in wire.calls if m == "editMessageText"]
    assert edits and "carrying on" in edits[-1]["text"]


async def test_a_deny_tap_is_a_denial_the_model_is_told_about(bot):
    wire = Wire(results={"sendMessage": {"message_id": 5}})
    async with wire.client() as client:
        conv = tg.Conversation(bot)
        approver = tg.TelegramApprover(bot, client, 4242, conv.taps)
        waiting = asyncio.create_task(approver.request(_request()))
        _, approval_id = await _asked(wire)
        await tg.handle_tap(bot, client, conv, _tap(approval_id, approve=False))
        result = await asyncio.wait_for(waiting, 2)

    assert result.approved is False and result.queued_id is None
    assert "denied" in (result.note or "").lower()
    assert (await repo_ops.get_approval(approval_id))["status"] == "denied"


async def test_no_tap_in_time_leaves_it_queued_and_says_so_rather_than_nobody_is_here(bot):
    bot.telegram.approval_timeout_s = 0.05
    wire = Wire(results={"sendMessage": {"message_id": 5}})
    async with wire.client() as client:
        approver = tg.TelegramApprover(bot, client, 4242, tg.Conversation(bot).taps)
        result = await asyncio.wait_for(approver.request(_request()), 2)

    assert result.approved is False and result.decided_by == "queued"
    assert result.queued_id is not None
    assert "did not answer" in result.note and str(result.queued_id) in result.note
    assert "keyboard" not in result.note
    assert (await repo_ops.get_approval(result.queued_id))["status"] == "pending"
    edits = [a for m, a in wire.calls if m == "editMessageText"]
    assert edits and "still queued" in edits[-1]["text"]


async def test_the_executor_relays_the_approvers_account_not_the_keyboard_line(bot):
    """What the model reads back is the sentence the approver wrote."""
    import json

    from agentd.policy.engine import engine_from_config
    from agentd.tools.base import ToolContext
    from agentd.tools.executor import ToolExecutor
    from agentd.tools.registry import get_registry

    bot.telegram.approval_timeout_s = 0.05
    wire = Wire(results={"sendMessage": {"message_id": 5}})
    async with wire.client() as client:
        approver = tg.TelegramApprover(bot, client, 4242, tg.Conversation(bot).taps)
        executor = ToolExecutor(get_registry().tools, engine_from_config(bot), approver)
        target = bot.paths.workspace / "agent-test.md"
        result = await executor.run(
            "fs_write", {"path": str(target), "content": "hello world", "reason": "test"},
            ToolContext(actor="main", origin="telegram", autonomy="assist", tool_subset=None),
        )
    body = json.loads(result.content)
    assert body["queued_for_approval"]
    assert "did not answer" in body["message"] and "/approve" in body["message"]
    assert "keyboard" not in body["message"]
    assert not target.exists()


async def test_a_tap_after_the_wait_is_over_still_works_on_the_queued_row(bot):
    """The daemon restarted, or the timeout passed: the buttons on the phone are still there
    and pressing one must still mean something."""
    approval_id = await repo_ops.queue_approval(
        tool_name="fs_write", args={"path": "/tmp/x", "content": "hi"}, risk="write",
        policy_rule="risk_matrix:write/assist", reason="because", origin="telegram",
    )
    wire = Wire()
    async with wire.client() as client:
        conv = tg.Conversation(bot)  # nothing waiting in it
        await tg.handle_tap(bot, client, conv, _tap(approval_id, approve=False, text="Approval needed"))

    assert (await repo_ops.get_approval(approval_id))["status"] == "denied"
    edits = [a for m, a in wire.calls if m == "editMessageText"]
    assert edits and edits[-1]["text"].startswith("Approval needed")
    assert "Denied" in edits[-1]["text"]


async def test_typing_approve_while_a_turn_waits_is_the_tap_and_runs_it_once(bot):
    wire = Wire(results={"sendMessage": {"message_id": 5}})
    async with wire.client() as client:
        conv = tg.Conversation(bot)
        approver = tg.TelegramApprover(bot, client, 4242, conv.taps)
        waiting = asyncio.create_task(approver.request(_request()))
        _, approval_id = await _asked(wire)
        await tg.handle_command(bot, client, conv, 4242, f"/approve {approval_id}")
        result = await asyncio.wait_for(waiting, 2)

    assert result.approved is True
    assert "carrying on" in wire.said()[-1]
    # The row says approved, not executed: the live turn is the one that runs it.
    assert (await repo_ops.get_approval(approval_id))["status"] == "approved"


async def test_a_bot_that_cannot_send_falls_back_to_the_queue_honestly(bot):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": False, "description": "chat not found"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        approver = tg.TelegramApprover(bot, client, 4242, tg.Conversation(bot).taps)
        result = await approver.request(_request())

    assert result.approved is False and result.queued_id is not None
    assert "Could not reach you" in result.note
    assert (await repo_ops.get_approval(result.queued_id))["status"] == "pending"


def test_only_my_buttons_parse():
    assert tg.parse_tap("approve:not-a-uuid") is None
    assert tg.parse_tap("launch:01a0e9f9-8ad2-726a-b15f-c4bbd6b063ac") is None
    assert tg.parse_tap("") is None
    assert tg.parse_tap("deny:01a0e9f9-8ad2-726a-b15f-c4bbd6b063ac") == (
        False, UUID("01a0e9f9-8ad2-726a-b15f-c4bbd6b063ac")
    )


async def test_a_command_does_not_wait_behind_the_turn_it_would_release(bot, monkeypatch):
    """`attend` takes the per-chat lock for a turn and skips it for a command. Otherwise
    `/approve <id>` typed during a wait would queue behind the very turn waiting on it."""
    started = asyncio.Event()
    release = asyncio.Event()

    class BlockingLoop(JournalingLoop):
        async def run_turn(self, session, text, *, run_id=None, **kwargs):
            started.set()
            await release.wait()
            yield Answer(turn_id=str(uuid7()), text="finally")

    monkeypatch.setattr(tg, "AgentLoop", lambda **kw: BlockingLoop())
    wire = Wire()
    async with wire.client() as client:
        conv = tg.Conversation(bot)
        turn = asyncio.create_task(tg.attend(bot, client, conv, {"chat": {"id": 4242}, "text": "slow"}))
        await asyncio.wait_for(started.wait(), 2)
        await asyncio.wait_for(
            tg.attend(bot, client, conv, {"chat": {"id": 4242}, "text": "/whoami"}), 2
        )
        assert "4242" in wire.said()[-1], "the command answered while the turn was still running"
        release.set()
        await asyncio.wait_for(turn, 2)
    assert wire.said()[-1] == "finally"
