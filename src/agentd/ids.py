"""Identifiers, clocks and small time parsing helpers."""

from __future__ import annotations

import os
import re
import struct
import time
import uuid
from datetime import UTC, datetime, timedelta


def utcnow() -> datetime:
    return datetime.now(UTC)


def uuid7() -> uuid.UUID:
    """Time-ordered UUID (RFC 9562 v7): keeps primary keys roughly insertion-ordered."""
    ms = int(time.time() * 1000)
    rand = os.urandom(10)
    b = bytearray(struct.pack(">Q", ms)[2:] + rand)
    b[6] = (b[6] & 0x0F) | 0x70
    b[8] = (b[8] & 0x3F) | 0x80
    return uuid.UUID(bytes=bytes(b))


def short_id(value: uuid.UUID | str) -> str:
    """Stable 4-hex handle used in context packs ([F:8c1e])."""
    s = str(value).replace("-", "")
    return s[:4]


_DURATION = re.compile(r"(?P<n>\d+(?:\.\d+)?)\s*(?P<unit>s|sec|secs|m|min|mins|h|hr|hrs|d|w)\b")
_UNIT_SECONDS = {
    "s": 1, "sec": 1, "secs": 1,
    "m": 60, "min": 60, "mins": 60,
    "h": 3600, "hr": 3600, "hrs": 3600,
    "d": 86400, "w": 604800,
}


def parse_duration(text: str) -> timedelta | None:
    """'10m', 'in 2 hours', '1h30m' -> timedelta. Returns None when nothing parses."""
    total = 0.0
    found = False
    for m in _DURATION.finditer(text.lower().replace("hours", "h").replace("minutes", "m")):
        total += float(m.group("n")) * _UNIT_SECONDS[m.group("unit")]
        found = True
    return timedelta(seconds=total) if found else None


def parse_when(text: str, *, now: datetime | None = None) -> datetime | None:
    """Accept an ISO timestamp or a relative duration ('in 10m')."""
    now = now or utcnow()
    text = text.strip()
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(text)
        return dt if dt.tzinfo else dt.replace(tzinfo=UTC)
    except ValueError:
        pass
    delta = parse_duration(text)
    return now + delta if delta else None


def estimate_tokens(text: str) -> int:
    """Cheap, deterministic token estimate used for budget packing."""
    return max(1, int(len(text) / 3.2))
