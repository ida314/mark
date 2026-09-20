"""Push delivery: what gets sent, what is held, and what must never happen."""

from __future__ import annotations

import httpx
import pytest

from agentd.daemon import notifier
from agentd.db import repo_agenda
from agentd.db.pool import connection


@pytest.fixture(autouse=True)
def not_quiet(monkeypatch):
    """Delivery tests must not depend on what time it happens to be — these same tests passed
    this morning and failed at 23:45, which is exactly the bug quiet hours is supposed to be."""
    monkeypatch.setattr(notifier, "in_quiet_hours", lambda *_a, **_k: False)


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def _row(**overrides) -> dict:
    nid = await repo_agenda.notify(
        source=overrides.pop("source", "test"),
        title=overrides.pop("title", "Something happened"),
        body=overrides.pop("body", "details"),
        level=overrides.pop("level", "info"),
        ref=overrides.pop("ref", None),
    )
    row = await repo_agenda.get_notification(nid)
    row.update(overrides)
    return row


async def test_level_sets_priority_and_tag(cfg):
    cfg.ntfy.enabled = True
    seen: list[httpx.Request] = []

    async def handler(request):
        seen.append(request)
        return httpx.Response(200)

    async with _client(handler) as client:
        for level, priority in (("info", "3"), ("warn", "4"), ("error", "5")):
            row = await _row(level=level)
            assert await notifier.push(row, cfg, client)
            assert seen[-1].headers["Priority"] == priority
            assert seen[-1].headers["Tags"] == notifier.TAGS[level]


async def test_a_successful_push_is_recorded_so_it_is_not_sent_twice(cfg):
    cfg.ntfy.enabled = True
    calls = 0

    async def handler(request):
        nonlocal calls
        calls += 1
        return httpx.Response(200)

    row = await _row()
    async with _client(handler) as client:
        await notifier._deliver(row, cfg, client)
        again = await repo_agenda.get_notification(row["id"])
        assert again["pushed_at"] is not None
        # a second delivery attempt for the same row claims nothing
        assert await notifier._claim(row["id"]) is False
    assert calls == 1


async def test_a_failed_push_stays_unpushed_and_counts_the_attempt(cfg):
    cfg.ntfy.enabled = True

    async def handler(request):
        return httpx.Response(503)

    row = await _row()
    async with _client(handler) as client:
        await notifier._deliver(row, cfg, client)
    again = await repo_agenda.get_notification(row["id"])
    assert again["pushed_at"] is None, "a 503 must not mark it delivered"
    assert again["push_attempts"] == 1


async def test_a_connection_error_is_audited_rather_than_notified(cfg):
    """Notifying about a push failure would feed the channel this loop subscribes to."""
    cfg.ntfy.enabled = True

    async def handler(request):
        raise httpx.ConnectError("no route to host")

    before = len(await repo_agenda.list_notifications(unread_only=False, limit=200))
    row = await _row()
    async with _client(handler) as client:
        await notifier._deliver(row, cfg, client)
    after = await repo_agenda.list_notifications(unread_only=False, limit=200)
    assert len(after) == before + 1, "the failure must not have produced another notification"

    async with connection() as conn:
        cur = await conn.execute(
            "SELECT count(*) AS c FROM actions WHERE kind = 'push' AND status = 'error'"
        )
        assert (await cur.fetchone())["c"] >= 1


def test_the_notifier_never_calls_notify():
    """Asserted against the parsed source, because the failure mode is a silent infinite loop:
    this loop subscribes to the channel notify() writes to."""
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(notifier))
    called = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    assert "notify" not in called


async def test_quiet_hours_hold_chatter_but_let_errors_through(cfg, monkeypatch):
    cfg.ntfy.enabled = True
    cfg.ntfy.quiet_hours = None
    cfg.daemon.quiet_hours = (23, 8)
    monkeypatch.setattr(notifier, "in_quiet_hours", lambda *_a, **_k: True)

    assert notifier.should_send({"level": "info"}, cfg) is False
    assert notifier.should_send({"level": "warn"}, cfg) is False
    assert notifier.should_send({"level": "error"}, cfg) is True


async def test_min_level_filters_below_the_threshold(cfg):
    cfg.ntfy.enabled = True
    cfg.ntfy.min_level = "warn"

    assert notifier.should_send({"level": "info"}, cfg) is False
    assert notifier.should_send({"level": "error"}, cfg) is True


async def test_catch_up_finds_what_arrived_while_the_daemon_was_down(cfg):
    """pg_notify is fire-and-forget: rows inserted with nobody listening are never replayed."""
    rows = [await _row(title=f"missed {i}") for i in range(3)]
    unpushed = await notifier._unpushed()
    ids = {r["id"] for r in unpushed}
    assert all(row["id"] in ids for row in rows)


async def test_a_credential_in_a_body_does_not_leave_the_box(cfg):
    leaky = "here is the key: ghp_0123456789abcdefghijABCDEFGHIJ012345"
    assert "ghp_" not in notifier._redact(leaky)
    assert notifier._redact("ordinary text") == "ordinary text"


async def test_an_approval_notification_carries_the_command_to_approve_it(cfg):
    row = {"title": "Approval needed", "body": "fs_write", "ref": {"approval": "abc-123"}}
    assert "agent approvals approve abc-123" in notifier._body(row)


async def test_the_bearer_token_comes_from_the_vault_not_the_config(cfg, tmp_path, monkeypatch):
    from agentd import secrets as vault

    path = tmp_path / "secrets.toml"
    monkeypatch.setattr(vault, "SECRETS_FILE", path)
    vault.put("ntfy/default", path, token="tk_secret")

    headers = notifier._headers({"level": "info", "title": "t"}, cfg)
    assert headers["Authorization"] == "Bearer tk_secret"
    # and nothing about the token is in the config the doctor prints
    assert "tk_secret" not in repr(cfg.model_dump())


# --- saying what actually happened -------------------------------------------


async def test_why_held_names_the_reason_push_is_off(cfg):
    cfg.ntfy.enabled = False
    assert "ntfy.enabled = false" in notifier.why_held("error", cfg)


async def test_why_held_names_the_min_level(cfg):
    cfg.ntfy.enabled = True
    cfg.ntfy.min_level = "warn"
    assert "min_level is warn" in notifier.why_held("info", cfg)


async def test_why_held_says_when_quiet_hours_end(cfg, monkeypatch):
    cfg.ntfy.enabled = True
    # Explicit, because `cfg` is built from the real config file: whoever runs this suite may
    # have set [ntfy] quiet_hours, and a test that silently inherits it tests their machine.
    cfg.ntfy.quiet_hours = None
    cfg.daemon.quiet_hours = (23, 8)
    monkeypatch.setattr(notifier, "in_quiet_hours", lambda *_a, **_k: True)

    held = notifier.why_held("info", cfg)
    assert "quiet hours" in held and "08:00" in held
    # An error wakes you on purpose; that is the whole point of the level.
    assert notifier.why_held("error", cfg) is None


async def test_why_held_is_none_when_it_really_will_go(cfg, monkeypatch):
    cfg.ntfy.enabled = True
    monkeypatch.setattr(notifier, "in_quiet_hours", lambda *_a, **_k: False)
    assert notifier.why_held("info", cfg) is None


async def test_notify_user_does_not_claim_delivery_it_cannot_know(cfg, monkeypatch):
    """The bug this replaces: the tool answered "Notification sent." while the notifier was
    holding the row, so the only account of an absent push was the model's imagination."""
    from agentd.tools.base import ToolContext
    from agentd.tools.builtin_agenda import notify_user

    cfg.ntfy.enabled = True
    cfg.daemon.quiet_hours = (23, 8)
    monkeypatch.setattr(notifier, "in_quiet_hours", lambda *_a, **_k: True)

    result = await notify_user.handler(
        {"title": "Test push notification", "body": "x"}, ToolContext(actor="main")
    )
    assert "not pushed" in result.content and "quiet hours" in result.content
    assert "sent" not in result.content.lower()

    rows = await repo_agenda.list_notifications()
    assert any(r["title"] == "Test push notification" for r in rows)


async def test_notify_user_says_so_when_it_really_will_push(cfg, monkeypatch):
    from agentd.tools.base import ToolContext
    from agentd.tools.builtin_agenda import notify_user

    cfg.ntfy.enabled = True
    monkeypatch.setattr(notifier, "in_quiet_hours", lambda *_a, **_k: False)

    result = await notify_user.handler({"title": "hello"}, ToolContext(actor="main"))
    assert "Queued" in result.content and "not pushed" not in result.content


# --- two windows, two questions ----------------------------------------------


async def test_push_inherits_the_daemon_window_when_it_has_none(cfg):
    """Back-compat: a config written before [ntfy] quiet_hours existed must not change
    behaviour the day this field appears."""
    cfg.ntfy.quiet_hours = None
    cfg.daemon.quiet_hours = (23, 8)
    assert notifier.quiet_window(cfg) == (23, 8)


async def test_push_can_keep_its_own_window(cfg, monkeypatch):
    """The case this exists for: wake me whenever, but do not run the heartbeat overnight."""
    cfg.ntfy.enabled = True
    cfg.ntfy.quiet_hours = (0, 0)  # empty window: push never waits
    cfg.daemon.quiet_hours = (23, 8)  # the heartbeat still sleeps

    assert notifier.quiet_window(cfg) == (0, 0)
    assert notifier.should_send({"level": "info"}, cfg) is True
    assert notifier.why_held("info", cfg) is None

    # And the heartbeat, reading its own setting, is unaffected.
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from agentd.daemon.heartbeat import in_quiet_hours

    three_am = datetime(2026, 9, 20, 3, tzinfo=ZoneInfo("America/New_York"))
    assert in_quiet_hours(three_am, tuple(cfg.daemon.quiet_hours)) is True


async def test_a_credential_in_a_title_does_not_leave_the_box(cfg):
    """`_redact` only ever saw the body. A title is pushed to a phone over the network like
    everything else, and it is also an HTTP header value, so a newline in one is header
    injection rather than a cosmetic problem."""
    headers = notifier._headers(
        {"title": "token is ghp_0123456789abcdefghijklmnopqrstuvwxyz", "level": "info"}, cfg
    )
    assert "ghp_" not in headers["Title"]

    flattened = notifier._headers({"title": "line one\nline two", "level": "info"}, cfg)
    assert "\n" not in flattened["Title"]
    assert flattened["Title"] == "line one line two"


async def test_an_empty_title_still_says_something(cfg):
    assert notifier._headers({"title": "", "level": "info"}, cfg)["Title"] == "agent"
