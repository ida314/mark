"""Telegram as a channel: who may talk to it, and what it does with what they say.

The allowlist tests are the ones that matter. Everything else here is plumbing; the
allowlist is the only thing standing between a stranger who guessed a bot username and an
agent holding this person's mail, calendar and filesystem.
"""

from __future__ import annotations

import httpx
import pytest

from agentd.daemon import telegram as tg
from agentd.db import repo_agenda, repo_ops
from agentd.db.pool import fetch_all


@pytest.fixture
def bot(cfg, monkeypatch, tmp_path):
    from agentd import secrets as vault

    monkeypatch.setattr(vault, "SECRETS_FILE", tmp_path / "secrets.toml")
    vault.put("telegram/bot", tmp_path / "secrets.toml", token="123456:test-token")
    cfg.telegram.enabled = True
    cfg.telegram.allowed_chat_ids = [4242]
    return cfg


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
    from agentd.agent.events import TurnFinished

    class FakeLoop:
        def __init__(self, **kw):
            self.kw = kw

        async def run_turn(self, session, text, **kwargs):
            assert kwargs["origin"] == "telegram"
            yield TurnFinished(text=f"you said: {text}", turn_id=None, steps=1)

    monkeypatch.setattr(tg, "AgentLoop", FakeLoop)
    wire = Wire()
    async with wire.client() as client:
        await tg.handle_message(
            bot, client, tg.Conversation(bot),
            {"chat": {"id": 4242}, "text": "what is on my calendar?"},
        )
    assert wire.said() == ["you said: what is on my calendar?"]
    assert ("sendChatAction", {"chat_id": 4242, "action": "typing"}) in wire.calls


async def test_a_denied_tool_is_explained_rather_than_swallowed(bot, monkeypatch):
    """A turn that produced nothing but a policy denial must not answer with silence."""
    from agentd.agent.events import ToolFinished, TurnFinished

    class FakeLoop:
        def __init__(self, **kw):
            pass

        async def run_turn(self, session, text, **kwargs):
            yield ToolFinished(name="web_fetch", ok=False, summary="", denied=True)
            yield TurnFinished(text="", turn_id=None, steps=1)

    monkeypatch.setattr(tg, "AgentLoop", FakeLoop)
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
        [("gmail_search", "ok"), ("memory_search", "running"), ("web_fetch", "denied")]
    )
    assert rendered.splitlines() == [
        "✓ gmail_search",
        "… memory_search",
        "✗ web_fetch (refused by policy)",
    ]


def test_tool_lines_carry_names_but_never_arguments():
    """An argument can hold a query built from something a stranger emailed. The rule about
    rendering their words is one rule, not one per surface."""
    rendered = tg.render_tools([("gmail_search", "ok")])
    assert "gmail_search" in rendered
    assert "query" not in rendered and "{" not in rendered


async def test_a_turn_shows_its_tools_and_edits_one_message(bot, monkeypatch):
    """Edited rather than re-sent: a trail of near-identical messages is what makes a phone
    unusable."""
    from agentd.agent.events import ToolFinished, ToolStarted, TurnFinished

    class FakeLoop:
        def __init__(self, **kw):
            pass

        async def run_turn(self, session, text, **kwargs):
            yield ToolStarted(name="gmail_search", args={})
            yield ToolFinished(name="gmail_search", ok=True, summary="", denied=False)
            yield ToolStarted(name="web_fetch", args={})
            yield ToolFinished(name="web_fetch", ok=False, summary="", denied=True)
            yield TurnFinished(text="here you go", turn_id=None, steps=2)

    monkeypatch.setattr(tg, "AgentLoop", FakeLoop)
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
    from agentd.agent.events import ToolFinished, ToolStarted, TurnFinished

    class FakeLoop:
        def __init__(self, **kw):
            pass

        async def run_turn(self, session, text, **kwargs):
            yield ToolStarted(name="gmail_search", args={})
            yield ToolFinished(name="gmail_search", ok=True, summary="", denied=False)
            yield TurnFinished(text="done", turn_id=None, steps=1)

    monkeypatch.setattr(tg, "AgentLoop", FakeLoop)
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
    from agentd.agent.events import ToolFinished, ToolStarted, TurnFinished

    class FakeLoop:
        def __init__(self, **kw):
            pass

        async def run_turn(self, session, text, **kwargs):
            yield ToolStarted(name="gmail_search", args={})
            yield ToolFinished(name="gmail_search", ok=True, summary="", denied=False)
            yield TurnFinished(text="the answer", turn_id=None, steps=1)

    monkeypatch.setattr(tg, "AgentLoop", FakeLoop)

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
