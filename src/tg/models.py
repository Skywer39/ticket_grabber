"""Persistence model.

Entities are keyed by ``"{source}:{external_id}"`` rather than a surrogate id, because
adapters only ever know the site's own identifiers and every cross-reference in the
diff engine is by that natural key.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import JSON, Column, Index, UniqueConstraint
from sqlmodel import Field, SQLModel


def utcnow() -> datetime:
    return datetime.now(UTC)


class Venue(SQLModel, table=True):
    __tablename__ = "venue"
    __table_args__ = (UniqueConstraint("key", name="uq_venue_key"),)

    id: int | None = Field(default=None, primary_key=True)
    key: str = Field(index=True)
    source: str = Field(index=True)
    external_id: str
    name: str
    city: str | None = None
    url: str | None = None
    first_seen_at: datetime = Field(default_factory=utcnow)
    last_seen_at: datetime = Field(default_factory=utcnow)


class Event(SQLModel, table=True):
    __tablename__ = "event"
    __table_args__ = (UniqueConstraint("key", name="uq_event_key"),)

    id: int | None = Field(default=None, primary_key=True)
    key: str = Field(index=True)
    source: str = Field(index=True)
    external_id: str
    title: str = Field(index=True)
    kind: str = "FILM"
    url: str | None = None
    poster_url: str | None = None
    duration_minutes: int | None = None
    release_date: datetime | None = None
    genres: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    formats: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    raw_attributes: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    first_seen_at: datetime = Field(default_factory=utcnow)
    last_seen_at: datetime = Field(default_factory=utcnow)


class Screening(SQLModel, table=True):
    """Current known state of one showing. Updated in place; history lives in
    :class:`ScreeningChange`."""

    __tablename__ = "screening"
    __table_args__ = (
        UniqueConstraint("key", name="uq_screening_key"),
        Index("ix_screening_lookup", "source", "venue_key", "starts_at"),
    )

    id: int | None = Field(default=None, primary_key=True)
    key: str = Field(index=True)
    source: str = Field(index=True)
    external_id: str
    event_key: str = Field(index=True)
    venue_key: str = Field(index=True)
    starts_at: datetime = Field(index=True)
    auditorium: str | None = None
    booking_url: str | None = None
    #: Pages that open on this screening's date — the film's, and the venue's programme.
    info_url: str | None = None
    venue_info_url: str | None = None
    sold_out: bool = False
    availability_ratio: float | None = None
    #: Lowest ratio ever observed for this screening — its "sold out" resting level.
    #:
    #: Near-sold-out halls do not settle at zero free seats. Praha Flora's IMAX sits at
    #: exactly 6 free across dozens of independent screenings, which is a structural
    #: floor (wheelchair spaces and their companion seats) rather than stock anyone can
    #: buy. Alerting on the ratio alone therefore fires on seats that were never really
    #: for sale; measuring the rise against this floor is what makes the number mean
    #: something.
    availability_floor: float | None = None
    sales_blocked: bool = False
    formats: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    languages: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    raw_attributes: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    price_min: float | None = None
    price_max: float | None = None

    content_hash: str = ""
    first_seen_at: datetime = Field(default_factory=utcnow)
    last_seen_at: datetime = Field(default_factory=utcnow)
    #: Set when the screening stops appearing in poll results.
    disappeared_at: datetime | None = None
    #: Last time tier 2 read the actual seat map for this screening.
    seatmap_checked_at: datetime | None = None


class ScreeningChange(SQLModel, table=True):
    """Append-only log of detected changes. Drives alerts and makes 'why did this
    fire?' answerable after the fact."""

    __tablename__ = "screening_change"
    __table_args__ = (Index("ix_change_lookup", "screening_key", "detected_at"),)

    id: int | None = Field(default=None, primary_key=True)
    screening_key: str = Field(index=True)
    change_type: str = Field(index=True)
    detected_at: datetime = Field(default_factory=utcnow, index=True)
    old_value: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    new_value: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))


class SeatSnapshot(SQLModel, table=True):
    """One tier-2 seat map reading."""

    __tablename__ = "seat_snapshot"
    __table_args__ = (Index("ix_seat_snapshot_lookup", "screening_key", "captured_at"),)

    id: int | None = Field(default=None, primary_key=True)
    screening_key: str = Field(index=True)
    captured_at: datetime = Field(default_factory=utcnow, index=True)
    fingerprint: str = ""
    total_seats: int = 0
    available_seats: int = 0
    #: Serialized ``Seat`` dicts. Kept whole so a later profile change can be
    #: re-evaluated against past snapshots without re-scraping.
    seats: list[dict[str, Any]] = Field(default_factory=list, sa_column=Column(JSON))


class Alert(SQLModel, table=True):
    __tablename__ = "alert"
    __table_args__ = (Index("ix_alert_lookup", "watch_name", "screening_key", "created_at"),)

    id: int | None = Field(default=None, primary_key=True)
    watch_name: str = Field(index=True)
    screening_key: str = Field(index=True)
    change_type: str
    created_at: datetime = Field(default_factory=utcnow, index=True)
    title: str = ""
    body: str = ""
    url: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    channels: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    delivered: bool = False
    delivery_error: str | None = None
    #: Recorded for history and cooldown, but rolled into a digest instead of sent.
    suppressed: bool = False


class PollState(SQLModel, table=True):
    """Per-source-per-request bookkeeping: conditional GET validators and health.

    ``consecutive_empty`` is what the adapter-heal agent watches — an adapter that
    suddenly returns nothing is usually a site change, not an empty schedule.
    """

    __tablename__ = "poll_state"
    __table_args__ = (UniqueConstraint("cache_key", name="uq_poll_state_key"),)

    id: int | None = Field(default=None, primary_key=True)
    cache_key: str = Field(index=True)
    source: str = Field(index=True)
    last_polled_at: datetime | None = None
    etag: str | None = None
    last_modified: str | None = None
    last_status: int | None = None
    consecutive_empty: int = 0
    consecutive_errors: int = 0
    last_error: str | None = None
    #: Free-form bookkeeping, e.g. the last known calendar of on-sale dates.
    data: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))


__all__ = [
    "Alert",
    "Event",
    "PollState",
    "SeatSnapshot",
    "Screening",
    "ScreeningChange",
    "SQLModel",
    "Venue",
    "utcnow",
]
