"""The connector framework, proven without any GitHub knowledge at all.

Everything here runs against a `FakeConnector` defined in this file. That is deliberate: if
`base.py` could only be exercised through `github.py`, the seam would be in the wrong place,
and this is the cheapest moment to find that out.
"""

from __future__ import annotations

from datetime import timedelta

import httpx
import psycopg
import pytest

from agentd.connectors import base
from agentd.connectors.base import (
    Connector,
    ConnectorAuthError,
    ConnectorRateLimited,
    ConnectorTransient,
    Item,
    PollResult,
    _seconds_until,
    poll_once,
    run_connector,
)
from agentd.db import repo_agenda, repo_connectors
from agentd.db.pool import connection, fetch_all, fetch_one
from agentd.db.repo_archive import RawEvent, append_event, append_event_once
from agentd.ids import utcnow


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


class FakeConnector(Connector):
    """A connector that knows nothing about any real service."""

    name = "fake"
    poll_interval_s = 60.0

    def __init__(self, *, items=None, raises=None, why=None, hint=None):
        self._items = items or []
        self._raises = raises
        self._why = why
        self._hint = hint
        self.reacted: list[str] = []
        self.polls = 0

    def configured(self):
        return self._why

    async def poll(self, client, cursor):
        self.polls += 1
        # Go through the client even when the payload is canned, so "did we make a request"
        # is answerable by counting handler calls rather than by trusting this class.
        await client.get("https://example.invalid/feed", headers={"If-None-Match": cursor.get("etag", "")})
        if self._raises:
            raise self._raises
        return PollResult(
            items=list(self._items),
            cursor={"etag": f"v{self.polls}"},
            next_poll_in_s=self._hint,
        )

    async def react(self, item, event_id):
        self.reacted.append(item.external_id)
        title = f"Fake item {base.safe_label(item.external_id, 'unknown')}"
        if await repo_agenda.loop_exists(title):
            return 0
        await repo_agenda.add_open_loop(title=title, detail=item.title, source_event_id=event_id)
        await repo_agenda.notify(source="connector:fake", title=title, level="info")
        return 1


def _item(external_id="a1", version="v1", **kw) -> Item:
    return Item(external_id=external_id, version=version, kind="thing", **kw)


async def _events() -> list[dict]:
    return await fetch_all("SELECT * FROM raw_events WHERE kind LIKE 'fake.%' ORDER BY id")


async def _notifications() -> list[dict]:
    return await fetch_all("SELECT * FROM notifications ORDER BY id")


# --- the schema guarantees ---------------------------------------------------


async def test_the_dedup_index_stops_a_duplicate_even_if_the_code_forgets(cfg):
    """The guarantee has to live in the database, because the case it defends against is two
    pollers racing — which is precisely the case you cannot reliably test into existence."""
    payload = {"dedup_key": "fake:a1:v1"}
    await append_event(RawEvent(kind="fake.thing", actor="connector:fake", payload=payload))
    with pytest.raises(psycopg.errors.UniqueViolation):
        await append_event(RawEvent(kind="fake.thing", actor="connector:fake", payload=payload))


async def test_ordinary_events_are_untouched_by_the_dedup_index(cfg):
    """The index is partial. Chat traffic has no dedup_key and must be able to repeat."""
    for _ in range(2):
        await append_event(RawEvent(kind="user_message", actor="user", content="hello"))
    rows = await fetch_all("SELECT * FROM raw_events WHERE kind = 'user_message'")
    assert len(rows) == 2


async def test_append_event_once_says_which_it_was(cfg):
    event = RawEvent(kind="fake.thing", actor="connector:fake", payload={"dedup_key": "k"})
    assert await append_event_once(event) is not None
    second = RawEvent(kind="fake.thing", actor="connector:fake", payload={"dedup_key": "k"})
    assert await append_event_once(second) is None


async def test_the_archive_is_still_append_only(cfg):
    """ON CONFLICT DO NOTHING needed no loosening of forbid_mutation(), and DO UPDATE would
    be refused by it — the schema physically forbids the wrong conflict action."""
    await append_event(RawEvent(kind="fake.thing", actor="connector:fake", content="x"))
    with pytest.raises(psycopg.errors.RaiseException):
        async with connection() as conn:
            await conn.execute("UPDATE raw_events SET content = 'tampered'")


async def test_connector_state_starts_enabled_with_an_empty_cursor(cfg):
    state = await repo_connectors.load_state("fake")
    assert state["enabled"] is True
    assert state["cursor"] == {}
    assert state["consecutive_failures"] == 0
    # Idempotent: loading again must not reset anything.
    await repo_connectors.record_success(
        "fake", cursor={"etag": "x"}, next_poll_at=utcnow(), new_items=3
    )
    again = await repo_connectors.load_state("fake")
    assert again["cursor"] == {"etag": "x"} and again["items_seen"] == 3


# --- ingestion ---------------------------------------------------------------


async def test_first_sync_archives_everything_as_untrusted(cfg):
    c = FakeConnector(items=[_item("a1"), _item("a2")])
    await poll_once(c, cfg, _client(lambda r: httpx.Response(200)))

    rows = await _events()
    assert len(rows) == 2
    assert {r["trust"] for r in rows} == {"untrusted"}
    assert all(r["session_id"] is None for r in rows)
    assert all(r["kind"] == "fake.thing" for r in rows)


async def test_a_source_cannot_overwrite_its_own_dedup_key(cfg):
    """Payload merge order is load-bearing: ours last."""
    c = FakeConnector(items=[_item("a1", payload={"dedup_key": "attacker-chosen"})])
    await poll_once(c, cfg, _client(lambda r: httpx.Response(200)))
    row = (await _events())[0]
    assert row["payload"]["dedup_key"] == "fake:a1:v1"


async def test_the_cursor_is_sent_on_the_next_poll(cfg):
    seen: list[str] = []

    def handler(request):
        seen.append(request.headers.get("If-None-Match", ""))
        return httpx.Response(200)

    c = FakeConnector(items=[_item("a1")])
    await poll_once(c, cfg, _client(handler))
    await poll_once(c, cfg, _client(handler))
    assert seen == ["", "v1"]


async def test_a_cursor_survives_a_restart(cfg):
    """A fresh connector object must pick up where the last one left off, which is only true
    if nothing that matters lives in the process."""
    seen: list[str] = []

    def handler(request):
        seen.append(request.headers.get("If-None-Match", ""))
        return httpx.Response(200)

    await poll_once(FakeConnector(items=[_item("a1")]), cfg, _client(handler))
    await poll_once(FakeConnector(items=[_item("a2")]), cfg, _client(handler))
    assert seen[-1] == "v1"


async def test_the_same_item_twice_is_one_event_one_loop_one_push(cfg):
    c = FakeConnector(items=[_item("a1")])
    await poll_once(c, cfg, _client(lambda r: httpx.Response(200)))
    await poll_once(c, cfg, _client(lambda r: httpx.Response(200)))

    assert len(await _events()) == 1
    assert len(await repo_agenda.list_open_loops("open")) == 1
    assert len(await _notifications()) == 1


async def test_a_changed_item_archives_again_but_does_not_reopen_the_loop(cfg):
    """The test that justifies two layers of dedup. The archive is version-sensitive, because
    a new comment is new information; the agenda is version-insensitive, because it is still
    the same thing waiting on you."""
    await poll_once(FakeConnector(items=[_item("a1", "v1")]), cfg, _client(lambda r: httpx.Response(200)))
    await poll_once(FakeConnector(items=[_item("a1", "v2")]), cfg, _client(lambda r: httpx.Response(200)))

    assert len(await _events()) == 2
    assert len(await repo_agenda.list_open_loops("open")) == 1
    assert len(await _notifications()) == 1


async def test_react_is_never_called_for_a_duplicate(cfg):
    c = FakeConnector(items=[_item("a1")])
    await poll_once(c, cfg, _client(lambda r: httpx.Response(200)))
    await poll_once(c, cfg, _client(lambda r: httpx.Response(200)))
    assert c.reacted == ["a1"]


async def test_one_bad_item_does_not_lose_the_batch(cfg):
    class Halfway(FakeConnector):
        async def react(self, item, event_id):
            if item.external_id == "a2":
                raise RuntimeError("malformed")
            return await super().react(item, event_id)

    c = Halfway(items=[_item("a1"), _item("a2"), _item("a3")])
    await poll_once(c, cfg, _client(lambda r: httpx.Response(200)))

    assert len(await _events()) == 3  # all archived, including the one that broke react()
    errors = await fetch_all("SELECT * FROM actions WHERE kind = 'connector_react'")
    assert len(errors) == 1 and errors[0]["status"] == "error"
    state = await repo_connectors.load_state("fake")
    assert state["consecutive_failures"] == 0  # the poll itself succeeded


# --- failure handling --------------------------------------------------------


async def test_an_auth_error_disables_the_connector_and_says_so_once(cfg):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200)

    c = FakeConnector(raises=ConnectorAuthError("token rejected"))
    await poll_once(c, cfg, _client(handler))

    state = await repo_connectors.load_state("fake")
    assert state["enabled"] is False
    assert "token rejected" in state["disabled_reason"]
    notes = await _notifications()
    assert len(notes) == 1 and notes[0]["level"] == "error"
    assert "agent connectors enable fake" in notes[0]["body"]

    # And now it is genuinely off: no further requests at all.
    await poll_once(c, cfg, _client(handler))
    assert len(calls) == 1


async def test_being_rate_limited_is_not_a_failure(cfg):
    """A busy hour must not look like an outage, or the connector disables itself for doing
    exactly what it was told."""
    c = FakeConnector(raises=ConnectorRateLimited(120))
    await poll_once(c, cfg, _client(lambda r: httpx.Response(200)))

    state = await repo_connectors.load_state("fake")
    assert state["consecutive_failures"] == 0
    assert state["enabled"] is True
    assert state["next_poll_at"] > utcnow() + timedelta(seconds=60)
    assert await _notifications() == []


async def test_a_transient_failure_backs_off_and_only_shouts_once(cfg):
    cfg.connectors.notify_after_failures = 3
    c = FakeConnector(raises=ConnectorTransient("boom"))
    for _ in range(5):
        await poll_once(c, cfg, _client(lambda r: httpx.Response(200)))

    state = await repo_connectors.load_state("fake")
    assert state["consecutive_failures"] == 5
    notes = await _notifications()
    assert len(notes) == 1 and notes[0]["level"] == "warn"


async def test_backoff_is_capped(cfg):
    cfg.connectors.max_backoff_s = 300.0
    c = FakeConnector(raises=ConnectorTransient("boom"))
    for _ in range(8):
        await poll_once(c, cfg, _client(lambda r: httpx.Response(200)))
    state = await repo_connectors.load_state("fake")
    assert state["next_poll_at"] <= utcnow() + timedelta(seconds=301)


async def test_a_connection_error_is_audited_not_notified(cfg):
    def handler(request):
        raise httpx.ConnectError("no route")

    c = FakeConnector()
    await poll_once(c, cfg, _client(handler))

    assert await _notifications() == []
    row = await fetch_one("SELECT * FROM actions WHERE kind = 'connector_poll' ORDER BY ts DESC")
    assert row["status"] == "error" and "ConnectError" in row["error"]


async def test_recovery_after_a_reported_outage_says_so(cfg):
    cfg.connectors.notify_after_failures = 2
    failing = FakeConnector(raises=ConnectorTransient("boom"))
    for _ in range(2):
        await poll_once(failing, cfg, _client(lambda r: httpx.Response(200)))

    await poll_once(FakeConnector(items=[_item("a1")]), cfg, _client(lambda r: httpx.Response(200)))

    levels = [n["level"] for n in await _notifications()]
    assert levels.count("warn") == 1
    assert "info" in levels
    state = await repo_connectors.load_state("fake")
    assert state["consecutive_failures"] == 0


# --- pacing and the loop -----------------------------------------------------


@pytest.mark.parametrize("hint,floor,expected", [(120.0, 60.0, 120.0), (5.0, 60.0, 60.0)])
async def test_a_hint_can_slow_us_down_but_not_speed_us_up(cfg, hint, floor, expected):
    c = FakeConnector(hint=hint)
    c.poll_interval_s = floor
    before = utcnow()
    await poll_once(c, cfg, _client(lambda r: httpx.Response(200)))
    state = await repo_connectors.load_state("fake")
    gap = (state["next_poll_at"] - before).total_seconds()
    assert expected - 5 <= gap <= expected + 5


async def test_a_disabled_connector_makes_no_requests(cfg):
    calls = []
    await repo_connectors.load_state("fake")
    await repo_connectors.set_enabled("fake", False, reason="by hand")

    def handler(request):
        calls.append(request)
        return httpx.Response(200)

    assert await poll_once(FakeConnector(items=[_item("a1")]), cfg, _client(handler)) is None
    assert calls == []


async def test_an_unconfigured_connector_reports_itself_instead_of_crashing(cfg):
    import asyncio

    stop = asyncio.Event()
    stop.set()
    await run_connector(FakeConnector(why="no token: run `agent secrets set fake/x token`"), cfg, stop)

    state = await repo_connectors.load_state("fake")
    assert state["enabled"] is False
    assert "no token" in state["disabled_reason"]


# --- structural invariants ---------------------------------------------------


def test_no_tool_can_reach_a_connector(cfg):
    """Stays true forever, unlike asserting on a list of tool names."""
    from agentd.tools.registry import build_registry

    for tool in build_registry().tools.values():
        module = getattr(tool.handler, "__module__", "")
        assert not module.startswith("agentd.connectors"), tool.name


def test_the_framework_only_ever_archives_untrusted():
    """Asserted against the parsed source, because the failure mode is silent: a connector
    that archived one thing as trusted would launder external text into trusted context and
    past the identity guard, and nothing would look wrong."""
    import ast
    import inspect

    from agentd.connectors import github

    for module in (base, github):
        tree = ast.parse(inspect.getsource(module))
        raw_events = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "RawEvent"
        ]
        for call in raw_events:
            trust = [k for k in call.keywords if k.arg == "trust"]
            assert trust, f"{module.__name__}: RawEvent without an explicit trust"
            assert isinstance(trust[0].value, ast.Constant)
            assert trust[0].value.value == "untrusted"

        called = {
            node.func.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        }
        assert not called & {"insert_fact", "supersede_fact", "retract_fact"}


# --- what a parked connector tells you ---------------------------------------


async def test_the_command_that_fixes_a_connector_survives_the_renderer(cfg):
    """`configured()` returns a shell command naming a TOML section, and Rich reads square
    brackets as markup: unescaped, the listing prints "set  host and username" and drops the
    one part that says where. Worth a test because the failure is silent and looks cosmetic.
    """
    from rich.console import Console

    from agentd.cli import commands_connect
    from agentd.config import ImapAccountConfig

    cfg.connectors.enabled = True
    cfg.connectors.imap.enabled = True
    cfg.connectors.imap.accounts = {"dodds": ImapAccountConfig()}

    console = Console(width=200, force_terminal=False, no_color=True)
    with console.capture() as captured:
        await commands_connect.render_list(cfg, console)

    assert "[connectors.imap.accounts.dodds]" in captured.get()


# --- a credential that arrives after the daemon did ---------------------------
#
# Live 2026-09-20: the daemon came up at 13:39:58 and parked `brightspace` with "no feed
# URL"; the URL was set at 14:03:20 and nothing ever looked again. `configured()` ran once,
# at startup, and the connector sat on `stop.wait()` for the life of the process — while
# `disabled_reason` went on naming a feed URL that by then existed, which is what sent the
# search after the link rather than after the daemon.


async def _one_pass(connector, cfg) -> dict:
    """Reconcile once and return the row. A pre-set stop leaves before any request."""
    import asyncio

    stop = asyncio.Event()
    stop.set()
    await run_connector(connector, cfg, stop)
    return await repo_connectors.load_state(connector.name)


async def test_a_credential_set_after_startup_brings_the_connector_back(cfg):
    connector = FakeConnector(why="no feed URL: run `agent secrets set fake/x ics_url`")

    parked = await _one_pass(connector, cfg)
    assert parked["enabled"] is False
    assert parked["disabled_reason"].startswith("not configured: ")

    # The credential shows up 23 minutes later. Nothing restarts.
    connector._why = None
    revived = await _one_pass(connector, cfg)

    assert revived["enabled"] is True
    assert revived["disabled_reason"] is None
    # Re-enabling asks for an immediate retry rather than another `disabled_recheck_s`.
    assert _seconds_until(revived, connector, cfg) == 0.0


async def test_the_parked_reason_tracks_what_is_missing_now(cfg):
    """A snapshot reason accuses whatever was wrong first, forever — which is how twenty
    minutes went into checking a feed URL that had been correct the whole time."""
    connector = FakeConnector(why="no feed URL")
    assert "no feed URL" in (await _one_pass(connector, cfg))["disabled_reason"]

    connector._why = "feed URL is not https"
    assert "not https" in (await _one_pass(connector, cfg))["disabled_reason"]


async def test_a_rejected_credential_is_not_revived_by_the_recheck(cfg):
    """`_handle_auth_error` needs a human. Reviving it would retry against an endpoint that
    is already refusing us, and would bury the notification that asked for the fix."""
    connector = FakeConnector()  # configured() passes: the token exists, it is just refused
    await repo_connectors.load_state("fake")
    await base._handle_auth_error(connector, ConnectorAuthError("token rejected"))

    state = await _one_pass(connector, cfg)
    assert state["enabled"] is False
    assert "token rejected" in state["disabled_reason"]


async def test_a_connector_switched_off_by_hand_stays_off(cfg):
    """A decision, not a fault."""
    connector = FakeConnector()
    await repo_connectors.load_state("fake")
    await repo_connectors.set_enabled("fake", False, reason="disabled by hand")

    state = await _one_pass(connector, cfg)
    assert state["enabled"] is False
    assert state["disabled_reason"] == "disabled by hand"
