"""The connector framework: how something outside this machine gets in.

A connector is daemon-side code, not a tool. It is never in the registry, the model cannot
call it, and it holds a credential the agent is fenced out of three ways (see `secrets.py`).
That is the entire reason this is a connector and not a `github_search` tool: the credential
and the untrusted text it fetches stay on the opposite side of the boundary from the thing
that could be talked into misusing them.

Four rules that are easy to state and easy to lose:

- **Everything ingested is `trust='untrusted'`.** Not a parameter, not configurable, no
  trusted-sender list. A sender is a claim, not a credential.
- **No connector constructs an `httpx.AsyncClient` or stores one on `self`.** The client is a
  parameter, exactly as `notifier.push(row, cfg, client)` takes one, which is what makes the
  whole framework testable against `httpx.MockTransport`. Auth headers go per request, not on
  the client, so a test can assert on them.
- **`poll_once` never raises.** `supervise()` in `daemon/main.py` notifies on every crash, so
  a propagated network error would mean two notifications and a restart loop for the internet
  being the internet. Errors are recorded, not raised.
- **Connectors *may* call `repo_agenda.notify()`** — unlike the notifier, which is forbidden
  from it because it subscribes to the channel that function writes to. A connector subscribes
  to nothing, so there is no feedback loop. Do not cargo-cult that prohibition here, or the
  one notification that genuinely needs a human (a rejected token) will never arrive.
"""

from __future__ import annotations

import asyncio
import re
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

import httpx

from ..config import Config
from ..db import repo_agenda, repo_connectors, repo_memory, repo_ops
from ..db.repo_archive import RawEvent, append_event_once
from ..ids import utcnow


@dataclass(frozen=True)
class Item:
    """One thing a connector found.

    Every string in here came from outside this machine and is untrusted — `title`, `body`
    and `actor` especially. None of them is ever interpolated into an open-loop title; see
    `safe_label` and each connector's own title composition.
    """

    external_id: str  # stable id of the thing at the source
    version: str = ""  # changes when the thing changes (github: updated_at)
    kind: str = "item"  # archived as f"{connector}.{kind}"
    title: str = ""  # THEIR words
    body: str = ""  # THEIR words
    actor: str = ""  # THEIR words
    url: str | None = None
    occurred_at: datetime | None = None
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass
class PollResult:
    items: list[Item] = field(default_factory=list)
    cursor: dict[str, Any] = field(default_factory=dict)  # replaces the stored cursor wholesale
    next_poll_in_s: float | None = None  # the source's own pacing hint
    note: str | None = None  # "not modified", for the audit row


class ConnectorAuthError(Exception):
    """The credential was rejected, or lacks the permission. Stop; a human must act."""


class ConnectorRateLimited(Exception):
    """The source is working as designed and has asked us to wait. Not a failure."""

    def __init__(self, retry_after_s: float) -> None:
        super().__init__(f"rate limited for {retry_after_s:.0f}s")
        self.retry_after_s = max(1.0, retry_after_s)


class ConnectorTransient(Exception):
    """A 5xx, a timeout, a dropped connection. Back off and try again."""


class Connector(ABC):
    """Something the daemon watches on your behalf."""

    name: str = "connector"  # connector_state PK and the raw_events kind prefix
    poll_interval_s: float = 60.0  # a floor. A source may slow us down, never speed us up.

    @property
    def vault_ref(self) -> str | None:
        """Where this connector's credential lives, for `agent doctor` to fingerprint. The
        ref, never the value — nothing outside the connector itself ever reveals a secret."""
        return None

    def configured(self) -> str | None:
        """None when ready to run; a human-readable reason when not.

        The string is shown by `agent connectors list` and `agent doctor`, so make it the
        command that fixes it rather than a description of the problem.
        """
        return None

    @abstractmethod
    async def poll(self, client: httpx.AsyncClient, cursor: dict[str, Any]) -> PollResult:
        """One fetch. No database writes, no open loops, no notifications.

        This is the only method that touches the network, and keeping it pure is what makes
        the framework testable without one.
        """

    @abstractmethod
    async def react(self, item: Item, event_id: UUID) -> int:
        """Turn one *newly archived* item into agenda. Returns open loops opened.

        Called exactly once per item that was not already in the archive — never for a
        duplicate. That is what makes "one push per review request" survive a daemon
        restart: the memory of having seen it lives in Postgres, not in this process.
        """

    async def sweep(self, client: httpx.AsyncClient) -> None:
        """Optional periodic reconciliation — closing loops the source considers finished.

        Separate from `poll` because it needs an *unconditional* listing: a 304 tells you
        nothing about what vanished, so inferring closure from a conditional poll would
        close every open loop the first quiet minute.
        """
        return None


# --- title composition -------------------------------------------------------

SAFE_SEGMENT = re.compile(r"[A-Za-z0-9._\-/]{1,140}")


def safe_label(value: str | None, fallback: str) -> str:
    """A field from outside, rendered fit for a title — or replaced entirely.

    This is not escaping, it is substitution. A value that is not a full match for a
    deliberately tiny character class is thrown away and `fallback` is used instead. That
    makes the set of strings a title can contain a regular language we can state and test,
    rather than a set we hope we escaped correctly.

    It matters more than it looks: `situation_report()` in `daemon/heartbeat.py`
    interpolates open-loop *titles* into an LLM prompt, so a title is model-visible input.
    This is an injection boundary, not formatting.
    """
    if not value or not SAFE_SEGMENT.fullmatch(value):
        return fallback
    return value


# --- ingestion ---------------------------------------------------------------


async def ingest(connector: Connector, item: Item) -> UUID | None:
    """Archive one item as an untrusted raw event.

    Returns the event id, or None when this exact version was already archived. `trust` is
    not a parameter and never will be.
    """
    event = RawEvent(
        kind=f"{connector.name}.{item.kind}",
        actor=(f"{connector.name}:{item.actor}" if item.actor else connector.name)[:200],
        content=(item.body or item.title or "")[:20000],
        trust="untrusted",
        occurred_at=item.occurred_at or utcnow(),
        payload={
            # Source fields first, ours last: the other order would let a field named
            # `dedup_key` at the source overwrite the key this all depends on.
            **item.payload,
            "connector": connector.name,
            "external_id": item.external_id,
            "version": item.version,
            "dedup_key": f"{connector.name}:{item.external_id}:{item.version}",
            "url": item.url,
            "source_title": item.title[:500],
        },
    )
    return await append_event_once(event)


async def propose_fact(
    connector: Connector, statement: str, *, event_id: UUID, confidence: float = 0.4
) -> UUID:
    """Propose a durable *fact* for the review gate to judge.

    Facts only — never a procedure, never a preference, and never something a connector
    merely observed happening. Connectors do not write canonical memory; the gate does, and
    `source_trust="untrusted"` is what earns this the identity-category guard in
    `memory/review.py`.

    Nothing calls this yet. It is here so that the first connector that has a genuine fact
    to propose does not have to invent the path under delivery pressure.
    """
    return await repo_memory.insert_candidate(
        statement=statement,
        proposed_by=f"connector:{connector.name}",
        kind="fact",
        confidence=confidence,
        source_trust="untrusted",
        evidence=[{"kind": "raw_event", "id": str(event_id)}],
    )


# --- what a connector opened -------------------------------------------------


async def tracked_loops(connector: str) -> list[dict]:
    """Open loops this connector opened, with the item they came from.

    The join is `open_loops.source_event_id -> raw_events.event_id`, which is why migration
    0007 indexes `(payload->>'connector', payload->>'external_id')`. Every sweep needs this
    and none of them should be writing the SQL again.
    """
    from ..db.pool import fetch_all

    return await fetch_all(
        """
        SELECT l.id, l.title, e.payload->>'external_id' AS external_id, e.occurred_at
        FROM open_loops l
        JOIN raw_events e ON e.event_id = l.source_event_id
        WHERE l.status <> 'closed' AND e.payload->>'connector' = %s
        """,
        (connector,),
    )


# --- one poll ----------------------------------------------------------------


async def _audit(connector: Connector, status: str, **kw: Any) -> None:
    await repo_ops.write_action(
        repo_ops.ActionRecord(
            actor=f"connector:{connector.name}",
            kind="connector_poll",
            name=connector.name,
            status=status,
            **kw,
        )
    )


async def _handle_auth_error(connector: Connector, exc: Exception) -> None:
    """A rejected credential is the one failure that always speaks up: it needs a human and
    will never fix itself. After this the connector makes no further requests at all."""
    await repo_connectors.set_enabled(connector.name, False, reason=str(exc)[:300])
    await _audit(connector, "error", error=str(exc)[:300])
    await repo_agenda.notify(
        source=f"connector:{connector.name}",
        level="error",
        title=f"{connector.name} connector disabled: credential rejected",
        body=(
            f"{exc}\n\nFix the credential, then:\n"
            f"agent connectors enable {connector.name}"
        ),
    )


async def poll_once(
    connector: Connector, cfg: Config, client: httpx.AsyncClient
) -> PollResult | None:
    """One poll: fetch, archive, react, record.

    Never raises. It records instead — see the module docstring for why that matters.
    """
    state = await repo_connectors.load_state(connector.name)
    if not state.get("enabled", True):
        return None

    started = time.monotonic()
    try:
        result = await connector.poll(client, dict(state.get("cursor") or {}))
    except ConnectorAuthError as exc:
        await _handle_auth_error(connector, exc)
        return None
    except ConnectorRateLimited as exc:
        # Not a failure: the source is healthy and has told us its terms.
        await repo_connectors.record_failure(
            connector.name,
            error=str(exc),
            next_poll_at=utcnow() + timedelta(seconds=exc.retry_after_s),
            count_it=False,
        )
        await _audit(connector, "skipped", error=str(exc)[:300])
        return None
    except Exception as exc:
        await _handle_transient(connector, cfg, state, exc)
        return None

    new_items = new_loops = 0
    for item in result.items[: cfg.connectors.max_items_per_poll]:
        event_id = await ingest(connector, item)
        if event_id is None:
            continue  # already archived: no loop, no push, no candidate
        new_items += 1
        try:
            new_loops += await connector.react(item, event_id)
        except Exception as exc:
            # One malformed item must not lose the rest of the batch, and it is already
            # archived, so nothing here is unrecoverable.
            await repo_ops.write_action(
                repo_ops.ActionRecord(
                    actor=f"connector:{connector.name}",
                    kind="connector_react",
                    name=connector.name,
                    status="error",
                    error=str(exc)[:300],
                    refs={"event": str(event_id)},
                )
            )

    interval = max(result.next_poll_in_s or 0.0, connector.poll_interval_s)
    await repo_connectors.record_success(
        connector.name,
        cursor=result.cursor,
        next_poll_at=utcnow() + timedelta(seconds=interval),
        new_items=new_items,
        new_loops=new_loops,
    )
    # `state` was read before the poll, so this is the pre-poll failure count — which is
    # exactly what decides whether we owe an all-clear.
    if state.get("consecutive_failures", 0) >= cfg.connectors.notify_after_failures:
        await repo_agenda.notify(
            source=f"connector:{connector.name}",
            level="info",
            title=f"{connector.name} connector is working again",
            body=f"after {state['consecutive_failures']} failed polls",
        )
    await _audit(
        connector,
        "ok",
        duration_ms=int((time.monotonic() - started) * 1000),
        output={
            "fetched": len(result.items),
            "new": new_items,
            "loops": new_loops,
            "note": result.note,
        },
    )
    return result


async def _handle_transient(
    connector: Connector, cfg: Config, state: dict, exc: Exception
) -> None:
    failures = state.get("consecutive_failures", 0) + 1
    backoff = min(
        connector.poll_interval_s * (2 ** min(failures, 10)), cfg.connectors.max_backoff_s
    )
    await repo_connectors.record_failure(
        connector.name,
        error=f"{type(exc).__name__}: {exc}",
        next_poll_at=utcnow() + timedelta(seconds=backoff),
    )
    await _audit(connector, "error", error=f"{type(exc).__name__}: {exc}"[:300])
    # Exactly on the crossing, so a flaky network is silent and a real outage tells you once.
    if failures == cfg.connectors.notify_after_failures:
        await repo_agenda.notify(
            source=f"connector:{connector.name}",
            level="warn",
            title=f"{connector.name} connector has failed {failures} times",
            body=str(exc)[:300],
        )


# --- the supervised loop -----------------------------------------------------


def _seconds_until(state: dict, connector: Connector, cfg: Config) -> float:
    if not state.get("enabled", True):
        # Re-check periodically rather than never: `agent connectors enable` should take
        # effect without a daemon restart. Without this the wait would be zero and the loop
        # would spin.
        return cfg.connectors.disabled_recheck_s
    next_at = state.get("next_poll_at")
    if next_at is None:
        return 0.0
    return max(0.0, (next_at - utcnow()).total_seconds())


async def run_connector(connector: Connector, cfg: Config, stop: asyncio.Event) -> None:
    """The supervised loop for one connector. Registered in `daemon/main.py`."""
    why = connector.configured()
    if why:
        # The notifier_loop precedent: park, do not crash, do not spin. `agent doctor` reads
        # the reason out of connector_state and tells you what to do about it.
        await repo_connectors.load_state(connector.name)
        await repo_connectors.set_enabled(connector.name, False, reason=f"not configured: {why}")
        await stop.wait()
        return

    last_sweep = 0.0
    async with httpx.AsyncClient(
        follow_redirects=False, timeout=cfg.connectors.timeout_s
    ) as client:
        while not stop.is_set():
            state = await repo_connectors.load_state(connector.name)
            try:
                # Sleep first, so a restart never polls an external API the instant it comes
                # up, and so shutdown never starts a poll on the way out.
                await asyncio.wait_for(stop.wait(), timeout=_seconds_until(state, connector, cfg))
                return
            except TimeoutError:
                pass

            await poll_once(connector, cfg, client)

            now = time.monotonic()
            sweep_every = getattr(connector, "sweep_interval_s", 0.0)
            if sweep_every and now - last_sweep >= sweep_every:
                last_sweep = now
                try:
                    await connector.sweep(client)
                    await repo_connectors.record_sweep(connector.name)
                except Exception as exc:
                    # A failed sweep is a missed tidy-up, not an outage: the loops it would
                    # have closed simply stay open, which is the safe direction to fail in.
                    await _audit(connector, "error", error=f"sweep: {exc}"[:300])
