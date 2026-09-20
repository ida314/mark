"""Google Calendar: what your day actually contains, without the agent holding the key.

A rolling window rather than a `syncToken`. The sync token is the incremental API and would
be cheaper per poll, but it answers "what changed since you last asked", and the question
worth asking is "what is coming up" - which a window answers directly, survives a daemon
that was off for a week, and cannot go stale and 410 at three in the morning. Calendar
quota is generous enough that a window every couple of minutes is not worth optimising.

**This connector opens no open loops, deliberately.** A calendar event is not a thing
waiting on you; it is a thing that will happen whether or not you act. Turning fourteen days
of events into fourteen days of loops would bury the ones that mean somebody is actually
blocked. What the events are *for* is the heartbeat's `situation_report`, which reads the
archive and says how much of your day is already spoken for - and says it in counts and
clock times, because an event summary was written by whoever sent the invitation and a
summary in a prompt is the same injection boundary an open-loop title is.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any
from uuid import UUID

import httpx

from ..config import Config
from ..ids import utcnow
from . import google_auth
from .base import Connector, ConnectorTransient, Item, PollResult

MAX_RESULTS = 250


class GoogleCalendarConnector(Connector):
    name = "gcal"

    def __init__(self, cfg: Config, label: str) -> None:
        self.cfg = cfg
        self.label = label
        self.account = cfg.connectors.google.accounts[label]
        self.name = f"gcal-{label}"
        self.poll_interval_s = self.account.poll_interval_s

    @property
    def vault_ref(self) -> str:
        return google_auth.account_ref(self.account.address)

    def configured(self) -> str | None:
        why = google_auth.not_configured(self.account.address)
        if why:
            return why.replace("auth <label>", f"auth {self.label}")
        if google_auth.CALENDAR_SCOPE not in google_auth.granted_scopes(self.account.address):
            # Authorised for mail only. Worth saying precisely, because the alternative is a
            # 403 an hour from now that reads like an administrator problem.
            return f"authorised without calendar access: re-run `agent connectors auth {self.label}`"
        return None

    async def poll(self, client: httpx.AsyncClient, cursor: dict[str, Any]) -> PollResult:
        now = utcnow()
        body = await google_auth.get_json(
            self.cfg,
            client,
            address=self.account.address,
            url=(
                f"{self.cfg.connectors.google.calendar_api_base.rstrip('/')}"
                f"/calendar/v3/calendars/{self.account.calendar_id}/events"
            ),
            params=[
                # An hour back, so an event that started before the poll is still archived
                # once - otherwise a daemon restart at 14:05 loses the 14:00 meeting.
                ("timeMin", _rfc3339(now - timedelta(hours=1))),
                ("timeMax", _rfc3339(now + timedelta(days=self.account.horizon_days))),
                ("singleEvents", "true"),  # recurrences expanded into their instances
                ("orderBy", "startTime"),
                ("showDeleted", "false"),
                ("maxResults", str(MAX_RESULTS)),
            ],
            what="Calendar",
        )
        events = body.get("items") or []
        if not isinstance(events, list):
            raise ConnectorTransient("unexpected events.list shape")

        items = [
            _to_item(e, self.account.address, self.account.calendar_id)
            for e in events
            if isinstance(e, dict) and e.get("status") != "cancelled"
        ]
        return PollResult(
            items=items,
            cursor={"window_days": self.account.horizon_days, "last_count": len(items)},
            note=f"{len(items)} events in the window",
        )

    async def react(self, item: Item, event_id: UUID) -> int:
        """Archive only. See the module docstring: an event is not an open loop."""
        return 0


def _rfc3339(when: datetime) -> str:
    return when.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _moment(spec: Any) -> tuple[datetime | None, bool]:
    """(start, all_day) from Calendar's two shapes: a `dateTime` or a bare `date`."""
    if not isinstance(spec, dict):
        return None, False
    if spec.get("dateTime"):
        try:
            return datetime.fromisoformat(str(spec["dateTime"]).replace("Z", "+00:00")), False
        except ValueError:
            return None, False
    if spec.get("date"):
        try:
            day = date.fromisoformat(str(spec["date"]))
        except ValueError:
            return None, True
        return datetime(day.year, day.month, day.day, tzinfo=UTC), True
    return None, False


def _to_item(event: dict, address: str, calendar_id: str) -> Item:
    start, all_day = _moment(event.get("start"))
    end, _ = _moment(event.get("end"))
    organizer = (event.get("organizer") or {}) if isinstance(event.get("organizer"), dict) else {}
    attendees = event.get("attendees") if isinstance(event.get("attendees"), list) else []
    return Item(
        external_id=str(event.get("id") or ""),
        # `updated` bumps when the event is edited, so a moved meeting is a second archive
        # row rather than a silent overwrite of the first.
        version=str(event.get("updated") or ""),
        kind="event",
        title=str(event.get("summary") or ""),
        body=str(event.get("description") or "")[:4000],
        actor=str(organizer.get("email") or ""),
        url=str(event.get("htmlLink") or "") or None,
        occurred_at=start,
        payload={
            "start": start.isoformat() if start else None,
            "end": end.isoformat() if end else None,
            "all_day": all_day,
            "location": str(event.get("location") or "")[:300],
            "attendees": len(attendees),
            "organizer": organizer.get("email"),
            "account": address,
            "calendar_id": calendar_id,
            "html_link": event.get("htmlLink"),
        },
    )
