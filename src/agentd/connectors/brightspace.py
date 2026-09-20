"""Brightspace: the deadlines your courses set, via the feed a student can actually get.

D2L's real API is Valence, and Valence needs an application key that a Brightspace
administrator registers. A student cannot register one, so this reads the per-user calendar
feed instead - the "subscribe" URL out of the Brightspace calendar, which is a plain HTTPS
GET, is read-only, needs no SSO dance, and carries the one thing that has a deadline.

What that costs, stated rather than discovered: no announcements, no grades, no discussion
replies. Those live behind Valence. If you ever get keys, this connector is the wrong shape
for them and a second one is the right answer.

**The feed URL is a credential.** Anyone holding it can read your course deadlines, so it
lives in the vault and not in config.toml, and nothing here ever puts it in an error
message, an audit row or a notification - which is why the failures below are phrased
without it.

Unlike mail and GitHub, this connector gives its loops a real `due_at`: somebody else set
that date and it is not negotiable, which is exactly the case `overdue_loops()` exists for.
"""

from __future__ import annotations

import re
from datetime import timedelta
from typing import Any
from uuid import UUID

import httpx

from .. import secrets as vault
from ..config import Config
from ..db import repo_agenda
from ..ids import utcnow
from . import ics
from .base import (
    Connector,
    ConnectorAuthError,
    ConnectorRateLimited,
    ConnectorTransient,
    Item,
    PollResult,
    tracked_loops,
)

# Course codes, extracted rather than escaped: CSCI-UA 480, MATH-UA 121, DS-GA 1003, CS 101.
# Uppercase-only on purpose, so ordinary words in a summary cannot match.
COURSE_CODE = re.compile(r"\b[A-Z]{2,6}(?:-[A-Z]{1,3})?[ -]?\d{2,4}\b")
LOOP_TITLE_WITH_COURSE = "Coursework due: {course} on {when}"
LOOP_TITLE = "Coursework due on {when}"
WHEN_FORMAT = "%a %d %b %H:%M"
# How many consecutive valid-but-empty reads it takes before an empty feed closes loops.
# Two, because one is how a transient oddity at the source empties your whole agenda.
EMPTY_SWEEPS_BEFORE_CLOSING = 2


def course_code(*texts: str) -> str | None:
    """A course code out of their text, or None.

    This is extraction, not escaping: the returned string is a substring that fully matched
    `COURSE_CODE`, so it contains uppercase letters, digits, at most one hyphen and at most
    one space, and nothing else. Whatever else the summary said is left behind.
    """
    for text in texts:
        match = COURSE_CODE.search(text or "")
        if match:
            return match.group(0)
    return None


def compose_title(course: str | None, when) -> str:
    """Our sentence. `when` is a datetime we formatted ourselves; `course` matched a tiny
    closed charset or is absent. Nothing from the feed reaches this verbatim."""
    stamp = when.astimezone().strftime(WHEN_FORMAT)
    if course:
        return LOOP_TITLE_WITH_COURSE.format(course=course, when=stamp)
    return LOOP_TITLE.format(when=stamp)


class BrightspaceConnector(Connector):
    name = "brightspace"

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.settings = cfg.connectors.brightspace
        self.poll_interval_s = self.settings.poll_interval_s
        self.sweep_interval_s = self.settings.sweep_interval_s
        # Consecutive sweeps that read a valid but empty calendar. In memory rather than in
        # `connector_state.cursor`, which `poll_once` replaces wholesale on every success;
        # a restart resets it to zero, which only ever delays a close.
        self._empty_sweeps = 0

    @property
    def vault_ref(self) -> str:
        return f"brightspace/{self.settings.label}"

    def configured(self) -> str | None:
        if vault.get(self.vault_ref, "ics_url") is None:
            return (
                f"no feed URL: in Brightspace open Calendar -> Subscribe, copy the link, then "
                f"run `agent secrets set {self.vault_ref} ics_url`"
            )
        return None

    # --- polling -------------------------------------------------------------

    def _feed_url(self) -> str:
        url = vault.get(self.vault_ref, "ics_url")
        if url is None:
            raise ConnectorAuthError("no feed URL in the vault")
        # Brightspace hands out webcal:// links, which are https:// wearing a hat so that a
        # desktop calendar app claims the click.
        return re.sub(r"^webcal://", "https://", url.reveal().strip())

    async def _fetch(self, client: httpx.AsyncClient, cursor: dict[str, Any]) -> httpx.Response:
        headers = {"Accept": "text/calendar", "User-Agent": "agentd (personal agent daemon)"}
        if cursor.get("etag"):
            headers["If-None-Match"] = str(cursor["etag"])
        elif cursor.get("last_modified"):
            headers["If-Modified-Since"] = str(cursor["last_modified"])

        url = self._feed_url()
        for _hop in (1, 2):  # one redirect, and only within the same host
            try:
                response = await client.get(url, headers=headers)
            except (httpx.TransportError, httpx.TimeoutException) as exc:
                # No URL in the message: it is the credential.
                raise ConnectorTransient(f"calendar feed: {type(exc).__name__}") from exc
            if response.status_code in (301, 302, 307, 308):
                location = response.headers.get("location", "")
                # The token is *in* the URL, so following a redirect off-host would hand the
                # credential to whoever controls the redirect target.
                if not _same_host(url, location):
                    raise ConnectorAuthError(
                        "the feed redirected to another host; re-copy the subscribe link"
                    )
                url = location
                continue
            return response
        raise ConnectorTransient("calendar feed redirected more than once")

    async def poll(self, client: httpx.AsyncClient, cursor: dict[str, Any]) -> PollResult:
        response = await self._fetch(client, cursor)

        if response.status_code == 304:
            return PollResult(items=[], cursor=cursor, note="not modified")
        if response.status_code in (401, 403):
            raise ConnectorAuthError(
                "the feed URL was rejected - regenerate it in Brightspace "
                f"(Calendar -> Subscribe) and re-run `agent secrets set {self.vault_ref} ics_url`"
            )
        if response.status_code == 404:
            raise ConnectorAuthError("the feed URL no longer exists - regenerate it in Brightspace")
        if response.status_code == 429:
            raise ConnectorRateLimited(_retry_after(response))
        if response.status_code >= 500:
            raise ConnectorTransient(f"the feed returned {response.status_code}")
        if response.status_code != 200:
            raise ConnectorTransient(f"the feed returned {response.status_code}")

        body = response.text
        if "BEGIN:VCALENDAR" not in body:
            # Almost always an SSO login page served with a 200, which is what happens when
            # a subscribe link is copied from the address bar instead of the dialog.
            raise ConnectorAuthError(
                "the feed did not return a calendar - copy the link from Calendar -> "
                "Subscribe rather than from the browser's address bar"
            )

        now = utcnow()
        horizon = now + timedelta(days=self.settings.horizon_days)
        items = [
            _to_item(event)
            for event in ics.parse(body)
            if event.start is not None and now - timedelta(hours=12) <= event.start <= horizon
        ]
        return PollResult(
            items=items,
            cursor={
                "etag": response.headers.get("ETag"),
                "last_modified": response.headers.get("Last-Modified"),
            },
            note=f"{len(items)} events in the next {self.settings.horizon_days} days",
        )

    # --- reacting ------------------------------------------------------------

    async def react(self, item: Item, event_id: UUID) -> int:
        when = item.occurred_at
        if when is None:
            return 0
        if item.payload.get("all_day") and not self.settings.all_day_loops:
            # Reading week is not a deadline. It is archived; it just does not become a
            # thing you owe somebody.
            return 0
        if self.settings.require and not any(
            needle.lower() in (item.title or "").lower() for needle in self.settings.require
        ):
            return 0

        title = compose_title(course_code(item.title, item.body), when)
        if await repo_agenda.loop_exists(title):
            return 0

        loop_id = await repo_agenda.add_open_loop(
            title=title,
            detail=_detail(item),
            due_at=when,
            source_event_id=event_id,
        )
        if self.settings.notify:
            await repo_agenda.notify(
                source=f"connector:{self.name}",
                level="info",
                title=title,
                body=item.url or "",
                ref={"loop": str(loop_id), "event": str(event_id)},
            )
        return 1

    # --- closing what the course withdrew -------------------------------------

    async def sweep(self, client: httpx.AsyncClient) -> None:
        """Close loops for deadlines the feed no longer carries.

        Unconditional on purpose: `poll` sends `If-None-Match`, and a 304 says nothing at
        all about what vanished. An ICS feed cannot say "you submitted this", so the only
        thing it can tell us is that a deadline no longer exists - and the cost of getting
        that wrong is a missed grade. Hence four separate refusals:

        - **An ambiguous read closes nothing.** A non-200 - including the 401 a regenerated
          feed URL returns - or a body with no `VCALENDAR`, which is the SSO login page
          `poll` already knows to recognise.
        - **Existence is asked of the whole feed, not the poll window.** `poll` filters to
          `now - 12h .. horizon` to decide what is worth *archiving*; this asks what still
          *exists*. Conflating the two closes an assignment that was merely pushed past the
          horizon, which is the one direction a student cannot afford.
        - **An empty calendar is believed, but only twice over.** A 200 carrying a
          `VCALENDAR` with no events is a real empty calendar and not a failure - end of
          term happens - but it is the single reading that closes everything at once, so it
          has to survive two consecutive sweeps.
        - **Only a deadline still in the future is eligible.** One leaves the archive window
          simply by passing, and passing is not withdrawal; closing on it would delete
          precisely the overdue loops `overdue_loops()` exists to surface.

        A rescheduled assignment keeps its UID, so it stays in `live` and is never closed.
        Where a feed omits UID entirely, `_to_item` falls back to a summary-and-start key and
        a reschedule does read as withdrawal - correctly, since `react` has by then opened a
        fresh loop under the new time and the old title names the old one.
        """
        response = await self._fetch(client, {})
        if response.status_code != 200 or "BEGIN:VCALENDAR" not in response.text:
            self._empty_sweeps = 0
            return

        live = {_to_item(event).external_id for event in ics.parse(response.text)}
        if live:
            self._empty_sweeps = 0
        else:
            self._empty_sweeps += 1
            if self._empty_sweeps < EMPTY_SWEEPS_BEFORE_CLOSING:
                return

        now = utcnow()
        for row in await tracked_loops(self.name):
            if row["external_id"] in live:
                continue
            # `occurred_at` on the source event is the deadline itself - see `_to_item`.
            if row["occurred_at"] is None or row["occurred_at"] <= now:
                continue
            await repo_agenda.close_open_loop(row["id"])


# --- parsing -----------------------------------------------------------------


def _same_host(current: str, location: str) -> bool:
    from urllib.parse import urlparse

    here, there = urlparse(current), urlparse(location)
    return bool(there.scheme == "https" and there.netloc and there.netloc == here.netloc)


def _retry_after(response: httpx.Response, default: float = 300.0) -> float:
    try:
        return float(response.headers["retry-after"])
    except (KeyError, ValueError):
        return default


def _to_item(event: ics.Event) -> Item:
    return Item(
        external_id=event.uid or f"{event.summary}:{event.start}",
        # A due date that moves is new information, so LAST-MODIFIED is the version - and
        # where the feed omits it, the start time serves, so a rescheduled deadline still
        # lands in the archive as a second row.
        version=event.last_modified or (event.start.isoformat() if event.start else ""),
        kind="assignment",
        title=event.summary,
        body=event.description[:4000],
        actor=event.categories[:100],
        url=event.url or None,
        occurred_at=event.start,
        payload={
            "all_day": event.all_day,
            "start": event.start.isoformat() if event.start else None,
            "end": event.end.isoformat() if event.end else None,
            "location": event.location[:200],
            "categories": event.categories[:200],
            "uid": event.uid,
        },
    )


def _detail(item: Item) -> str:
    """Their words, where no tool renders them to the model."""
    lines = [f"due: {item.occurred_at:%Y-%m-%d %H:%M %Z}" if item.occurred_at else ""]
    if item.title:
        lines.append(f"summary: {item.title[:500]}")
    if item.payload.get("categories"):
        lines.append(f"course: {item.payload['categories'][:200]}")
    if item.body:
        lines.append(f"detail: {item.body[:1000]}")
    if item.url:
        lines.append(item.url)
    return "\n".join(line for line in lines if line)
