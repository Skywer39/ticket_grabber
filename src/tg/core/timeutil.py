"""Time handling.

Rule for the whole codebase: the domain and the database speak UTC; venue-local time
exists only at the edges (parsing a site's response, rendering a notification).
Screening times are the one thing users read directly, so getting this wrong is very
visible.
"""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

DEFAULT_TZ = "Europe/Prague"


def zone(name: str | None = None) -> ZoneInfo:
    return ZoneInfo(name or DEFAULT_TZ)


def utcnow_aware() -> datetime:
    return datetime.now(UTC)


def local_to_utc(naive_local: datetime, tz_name: str | None = None) -> datetime:
    """Interpret a site's naive timestamp as venue-local, return UTC-aware."""
    if naive_local.tzinfo is not None:
        return naive_local.astimezone(UTC)
    return naive_local.replace(tzinfo=zone(tz_name)).astimezone(UTC)


def to_utc(value: datetime) -> datetime:
    """Normalize any datetime to UTC-aware. Naive values are assumed to be UTC."""
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def for_db(value: datetime) -> datetime:
    """SQLite drops tzinfo, so store naive UTC and be explicit about it."""
    return to_utc(value).replace(tzinfo=None)


def from_db(value: datetime) -> datetime:
    """Re-attach UTC to a value read back from the database."""
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def to_local(value: datetime, tz_name: str | None = None) -> datetime:
    return to_utc(value).astimezone(zone(tz_name))


def format_local(value: datetime, tz_name: str | None = None) -> str:
    """Human-facing rendering, e.g. ``Tue 04 Aug 2026, 16:40``."""
    return to_local(value, tz_name).strftime("%a %d %b %Y, %H:%M")
