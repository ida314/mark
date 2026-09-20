"""Just enough iCalendar to read a feed, and no more.

RFC 5545 is a large specification and almost none of it matters for reading a published
feed: no VTIMEZONE arithmetic, no RRULE expansion, no VALARM, no attendee state machine.
This parses VEVENT blocks into start times and text, and treats anything it does not
recognise as absent rather than guessing.

Two properties of the parse are load-bearing, because the input arrives over the network
from something we do not control:

- **A malformed line is skipped, never fatal.** One bad DTSTART must not cost the other
  forty events, so every conversion that can fail returns None instead of raising.
- **Nothing here composes a title.** The strings it returns are exactly as untrusted as the
  bytes they came from; `brightspace.py` extracts a course code out of them against a tiny
  charset and uses its own sentence, which is where that boundary lives.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# NAME;PARAM=value;PARAM2="quoted:value":the rest of the line is the value
PROPERTY = re.compile(r"^(?P<name>[A-Za-z0-9\-]+)(?P<params>(?:;[^:]*)?):(?P<value>.*)$")


@dataclass
class Event:
    uid: str = ""
    summary: str = ""
    description: str = ""
    location: str = ""
    url: str = ""
    start: datetime | None = None
    end: datetime | None = None
    all_day: bool = False
    last_modified: str = ""
    categories: str = ""
    raw: dict[str, str] = field(default_factory=dict)


def unfold(text: str) -> list[str]:
    """RFC 5545 folds long lines by inserting CRLF + one space or tab. Unfolding first means
    every other function here can assume one property per line."""
    lines: list[str] = []
    for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if raw[:1] in (" ", "\t") and lines:
            lines[-1] += raw[1:]
        else:
            lines.append(raw)
    return lines


def unescape(value: str) -> str:
    out = value.replace("\\N", "\n").replace("\\n", "\n")
    return out.replace("\\,", ",").replace("\\;", ";").replace("\\\\", "\\")


def _params(raw: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for chunk in raw.split(";"):
        if "=" in chunk:
            key, _, value = chunk.partition("=")
            out[key.strip().upper()] = value.strip().strip('"')
    return out


def _moment(value: str, params: dict[str, str]) -> tuple[datetime | None, bool]:
    """The three DTSTART shapes: a UTC stamp, a local stamp with a TZID, and a bare date.

    An unknown TZID falls back to UTC rather than dropping the event. That can put a due
    time a few hours out, which is visible and fixable; dropping the deadline entirely is
    neither.
    """
    value = value.strip()
    if params.get("VALUE", "").upper() == "DATE" or re.fullmatch(r"\d{8}", value):
        try:
            day = date.fromisoformat(value[:8])
        except ValueError:
            return None, True
        return datetime(day.year, day.month, day.day, tzinfo=UTC), True

    match = re.fullmatch(r"(\d{8})T(\d{6})(Z)?", value)
    if not match:
        return None, False
    stamp, clock, zulu = match.groups()
    try:
        naive = datetime.strptime(stamp + clock, "%Y%m%d%H%M%S")
    except ValueError:
        return None, False
    if zulu:
        return naive.replace(tzinfo=UTC), False
    tzid = params.get("TZID", "")
    if tzid:
        try:
            return naive.replace(tzinfo=ZoneInfo(tzid)), False
        except (ZoneInfoNotFoundError, ValueError):
            pass
    # A floating time, or a TZID this machine has never heard of.
    return naive.replace(tzinfo=UTC), False


def parse(text: str, limit: int = 1000) -> list[Event]:
    """Every VEVENT in the feed, in file order. `limit` bounds what a hostile or broken feed
    can cost us; a calendar with more than a thousand events in the window is a bug
    somewhere else."""
    events: list[Event] = []
    current: Event | None = None

    for line in unfold(text):
        if line.startswith("BEGIN:VEVENT"):
            current = Event()
            continue
        if line.startswith("END:VEVENT"):
            if current is not None:
                events.append(current)
            current = None
            if len(events) >= limit:
                break
            continue
        if current is None:
            continue

        match = PROPERTY.match(line)
        if not match:
            continue
        name = match.group("name").upper()
        params = _params(match.group("params") or "")
        value = match.group("value")
        current.raw[name] = value

        if name == "UID":
            current.uid = value.strip()
        elif name == "SUMMARY":
            current.summary = unescape(value)
        elif name == "DESCRIPTION":
            current.description = unescape(value)
        elif name == "LOCATION":
            current.location = unescape(value)
        elif name == "URL":
            current.url = value.strip()
        elif name == "CATEGORIES":
            current.categories = unescape(value)
        elif name == "LAST-MODIFIED":
            current.last_modified = value.strip()
        elif name == "DTSTART":
            current.start, current.all_day = _moment(value, params)
        elif name == "DTEND":
            current.end, _ = _moment(value, params)

    return events
