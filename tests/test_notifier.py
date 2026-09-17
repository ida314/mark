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
    cfg.daemon.quiet_hours = (23, 8)
    monkeypatch.setattr(notifier, "in_quiet_hours", lambda *_a, **_k: True)

    assert notifier._should_send({"level": "info"}, cfg) is False
    assert notifier._should_send({"level": "warn"}, cfg) is False
    assert notifier._should_send({"level": "error"}, cfg) is True


async def test_min_level_filters_below_the_threshold(cfg):
    cfg.ntfy.enabled = True
    cfg.ntfy.min_level = "warn"

    assert notifier._should_send({"level": "info"}, cfg) is False
    assert notifier._should_send({"level": "error"}, cfg) is True


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
