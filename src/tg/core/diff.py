"""Change detection.

The diff engine reports *what changed*, never *whether it matters* — thresholds and
filtering belong to the watch layer. Keeping that split means one poll produces one
canonical change log, and several watches with different sensitivities can each
interpret it without re-polling.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime
from enum import StrEnum
from typing import Any

from sqlmodel import Session, col, select

from tg.core.normalize import NormEvent, NormScreening, NormVenue, SeatMap
from tg.core.timeutil import for_db, from_db, utcnow_aware
from tg.models import (
    Event,
    PollState,
    Screening,
    ScreeningChange,
    SeatSnapshot,
    Venue,
    utcnow,
)

log = logging.getLogger(__name__)


class ChangeType(StrEnum):
    NEW_EVENT = "NEW_EVENT"
    #: A date that previously had nothing on sale now has something — the earliest
    #: and cheapest signal that a new program was published.
    NEW_DATE = "NEW_DATE"
    NEW_SCREENING = "NEW_SCREENING"
    SCREENING_REMOVED = "SCREENING_REMOVED"
    AVAILABILITY_RISE = "AVAILABILITY_RISE"
    AVAILABILITY_DROP = "AVAILABILITY_DROP"
    SOLD_OUT = "SOLD_OUT"
    BACK_ON_SALE = "BACK_ON_SALE"
    PRICE_CHANGE = "PRICE_CHANGE"
    #: Tier 2 only: specific seats became free.
    SEAT_FREED = "SEAT_FREED"


@dataclass(slots=True)
class Change:
    change_type: ChangeType
    source: str
    screening_key: str | None = None
    event_key: str | None = None
    old: dict[str, Any] = field(default_factory=dict)
    new: dict[str, Any] = field(default_factory=dict)
    detected_at: datetime = field(default_factory=utcnow_aware)
    #: Carried through so downstream layers can render an alert without re-querying.
    screening: NormScreening | None = None
    event: NormEvent | None = None

    @property
    def delta(self) -> float | None:
        """Availability change, when this is an availability change."""
        o, n = self.old.get("availability_ratio"), self.new.get("availability_ratio")
        if o is None or n is None:
            return None
        return n - o


def sync_venues(session: Session, venues: list[NormVenue]) -> int:
    existing = {
        v.key: v
        for v in session.exec(
            select(Venue).where(col(Venue.key).in_([v.key for v in venues] or [""]))
        ).all()
    }
    written = 0
    for nv in venues:
        row = existing.get(nv.key)
        if row is None:
            session.add(
                Venue(
                    key=nv.key,
                    source=nv.source,
                    external_id=nv.external_id,
                    name=nv.name,
                    city=nv.city,
                    url=nv.url,
                )
            )
            written += 1
        else:
            row.name, row.city, row.url = nv.name, nv.city, nv.url
            row.last_seen_at = utcnow()
            session.add(row)
    return written


def sync_events(session: Session, events: list[NormEvent]) -> list[Change]:
    keys = [e.key for e in events] or [""]
    existing = {
        e.key: e for e in session.exec(select(Event).where(col(Event.key).in_(keys))).all()
    }
    changes: list[Change] = []
    for ne in events:
        row = existing.get(ne.key)
        if row is None:
            session.add(
                Event(
                    key=ne.key,
                    source=ne.source,
                    external_id=ne.external_id,
                    title=ne.title,
                    kind=str(ne.kind),
                    url=ne.url,
                    poster_url=ne.poster_url,
                    duration_minutes=ne.duration_minutes,
                    release_date=(
                        datetime.combine(ne.release_date, datetime.min.time())
                        if ne.release_date
                        else None
                    ),
                    genres=ne.genres,
                    formats=sorted(str(f) for f in ne.formats),
                    raw_attributes=ne.raw_attributes,
                )
            )
            changes.append(
                Change(
                    change_type=ChangeType.NEW_EVENT,
                    source=ne.source,
                    event_key=ne.key,
                    new={"title": ne.title, "url": ne.url},
                    event=ne,
                )
            )
        else:
            row.title = ne.title
            row.formats = sorted(str(f) for f in ne.formats)
            row.genres = ne.genres
            row.raw_attributes = ne.raw_attributes
            row.last_seen_at = utcnow()
            session.add(row)
    return changes


def sync_screenings(
    session: Session,
    source: str,
    screenings: list[NormScreening],
    *,
    covered_dates: set[date] | None = None,
    detect_removals: bool = True,
) -> list[Change]:
    """Upsert screenings and return everything that changed.

    ``covered_dates`` tells the engine which days this poll actually looked at, so a
    screening missing from a partial poll is not mistaken for a cancelled one.
    """
    keys = [s.key for s in screenings] or [""]
    existing = {
        s.key: s
        for s in session.exec(select(Screening).where(col(Screening.key).in_(keys))).all()
    }
    changes: list[Change] = []
    now = utcnow()

    for ns in screenings:
        row = existing.get(ns.key)
        new_hash = ns.content_hash()

        if row is None:
            session.add(_to_row(ns, new_hash, now))
            changes.append(
                Change(
                    change_type=ChangeType.NEW_SCREENING,
                    source=source,
                    screening_key=ns.key,
                    event_key=ns.event_key,
                    new=_snapshot(ns),
                    screening=ns,
                )
            )
            continue

        row.last_seen_at = now
        if row.disappeared_at is not None:
            row.disappeared_at = None

        if row.content_hash == new_hash:
            session.add(row)
            continue

        changes.extend(_compare(row, ns, source))
        _apply(row, ns, new_hash, now)
        session.add(row)

    if detect_removals and covered_dates:
        changes.extend(_detect_removals(session, source, screenings, covered_dates, now))

    for ch in changes:
        if ch.screening_key:
            session.add(
                ScreeningChange(
                    screening_key=ch.screening_key,
                    change_type=str(ch.change_type),
                    detected_at=for_db(ch.detected_at),
                    old_value=ch.old,
                    new_value=ch.new,
                )
            )
    return changes


def _compare(row: Screening, ns: NormScreening, source: str) -> list[Change]:
    """Turn a hash mismatch into specific, named changes."""
    out: list[Change] = []

    def change(kind: ChangeType, old: dict, new: dict) -> Change:
        return Change(
            change_type=kind,
            source=source,
            screening_key=ns.key,
            event_key=ns.event_key,
            old=old,
            new=new,
            screening=ns,
        )

    old_ratio, new_ratio = row.availability_ratio, ns.availability_ratio
    if old_ratio is not None and new_ratio is not None and old_ratio != new_ratio:
        kind = (
            ChangeType.AVAILABILITY_RISE if new_ratio > old_ratio else ChangeType.AVAILABILITY_DROP
        )
        out.append(
            change(
                kind,
                {"availability_ratio": old_ratio},
                {"availability_ratio": new_ratio},
            )
        )

    if row.sold_out != ns.sold_out:
        out.append(
            change(
                ChangeType.SOLD_OUT if ns.sold_out else ChangeType.BACK_ON_SALE,
                {"sold_out": row.sold_out},
                {"sold_out": ns.sold_out},
            )
        )

    if (row.price_min, row.price_max) != (ns.price_min, ns.price_max) and (
        ns.price_min is not None or ns.price_max is not None
    ):
        out.append(
            change(
                ChangeType.PRICE_CHANGE,
                {"price_min": row.price_min, "price_max": row.price_max},
                {"price_min": ns.price_min, "price_max": ns.price_max},
            )
        )

    return out


def _detect_removals(
    session: Session,
    source: str,
    screenings: list[NormScreening],
    covered_dates: set[date],
    now: datetime,
) -> list[Change]:
    """Flag screenings that vanished from days we fully polled."""
    seen_keys = {s.key for s in screenings}
    candidates = session.exec(
        select(Screening).where(
            Screening.source == source,
            col(Screening.disappeared_at).is_(None),
        )
    ).all()

    out: list[Change] = []
    for row in candidates:
        if row.key in seen_keys:
            continue
        if from_db(row.starts_at).date() not in covered_dates:
            continue  # we did not look at that day, so absence proves nothing
        row.disappeared_at = now
        session.add(row)
        out.append(
            Change(
                change_type=ChangeType.SCREENING_REMOVED,
                source=source,
                screening_key=row.key,
                event_key=row.event_key,
                old={"starts_at": from_db(row.starts_at).isoformat(), "auditorium": row.auditorium},
            )
        )
    return out


def sync_calendar(session: Session, source: str, dates: list[date]) -> list[Change]:
    """Diff the cheap calendar probe against what we saw last time.

    A new date appearing here is the earliest possible warning that a program was
    published — which is the exact failure this project exists to prevent.
    """
    cache_key = f"{source}:calendar"
    state = session.exec(select(PollState).where(PollState.cache_key == cache_key)).first()
    known: set[str] = set((state.data or {}).get("dates", [])) if state else set()
    current = {d.isoformat() for d in dates}

    changes: list[Change] = []
    if known:
        for added in sorted(current - known):
            changes.append(
                Change(
                    change_type=ChangeType.NEW_DATE,
                    source=source,
                    new={"date": added},
                )
            )

    if state is None:
        state = PollState(cache_key=cache_key, source=source)
    state.data = {"dates": sorted(current)}
    state.last_polled_at = utcnow()
    session.add(state)
    return changes


def record_seatmap(
    session: Session, screening_key: str, seatmap: SeatMap
) -> tuple[list[Change], SeatSnapshot]:
    """Store a tier-2 reading and report seats that became free since the last one."""
    previous = session.exec(
        select(SeatSnapshot)
        .where(SeatSnapshot.screening_key == screening_key)
        .order_by(col(SeatSnapshot.captured_at).desc())
    ).first()

    snapshot = SeatSnapshot(
        screening_key=screening_key,
        captured_at=for_db(seatmap.captured_at or utcnow_aware()),
        fingerprint=seatmap.fingerprint(),
        total_seats=len(seatmap.seats),
        available_seats=len(seatmap.available),
        seats=[
            {
                "row_label": s.row_label,
                "seat_label": s.seat_label,
                "status": str(s.status),
                "row_index": s.row_index,
                "seat_index": s.seat_index,
                "section": s.section,
                "price_class": s.price_class,
            }
            for s in seatmap.seats
        ],
    )
    session.add(snapshot)

    changes: list[Change] = []
    if previous is not None and previous.fingerprint != snapshot.fingerprint:
        before = {
            f"{s.get('section') or ''}|{s['row_label']}|{s['seat_label']}"
            for s in previous.seats
            if s.get("status") == "AVAILABLE"
        }
        freed = sorted(seatmap.available_ids() - before)
        if freed:
            changes.append(
                Change(
                    change_type=ChangeType.SEAT_FREED,
                    source=screening_key.split(":", 1)[0],
                    screening_key=screening_key,
                    old={"available": previous.available_seats},
                    new={"available": snapshot.available_seats, "freed_seats": freed},
                )
            )
            session.add(
                ScreeningChange(
                    screening_key=screening_key,
                    change_type=str(ChangeType.SEAT_FREED),
                    old_value={"available": previous.available_seats},
                    new_value={"available": snapshot.available_seats, "freed_seats": freed},
                )
            )
    return changes, snapshot


# ----------------------------------------------------------------- row helpers


def _snapshot(ns: NormScreening) -> dict[str, Any]:
    return {
        "starts_at": ns.starts_at.isoformat(),
        "auditorium": ns.auditorium,
        "availability_ratio": ns.availability_ratio,
        "sold_out": ns.sold_out,
        "formats": sorted(str(f) for f in ns.formats),
    }


def _to_row(ns: NormScreening, content_hash: str, now: datetime) -> Screening:
    return Screening(
        key=ns.key,
        source=ns.source,
        external_id=ns.external_id,
        event_key=ns.event_key,
        venue_key=ns.venue_key,
        starts_at=for_db(ns.starts_at),
        auditorium=ns.auditorium,
        booking_url=ns.booking_url,
        sold_out=ns.sold_out,
        availability_ratio=ns.availability_ratio,
        sales_blocked=ns.sales_blocked,
        formats=sorted(str(f) for f in ns.formats),
        languages=ns.languages,
        raw_attributes=ns.raw_attributes,
        price_min=ns.price_min,
        price_max=ns.price_max,
        content_hash=content_hash,
        first_seen_at=now,
        last_seen_at=now,
    )


def _apply(row: Screening, ns: NormScreening, content_hash: str, now: datetime) -> None:
    row.starts_at = for_db(ns.starts_at)
    row.auditorium = ns.auditorium
    row.booking_url = ns.booking_url
    row.sold_out = ns.sold_out
    row.availability_ratio = ns.availability_ratio
    row.sales_blocked = ns.sales_blocked
    row.formats = sorted(str(f) for f in ns.formats)
    row.languages = ns.languages
    row.raw_attributes = ns.raw_attributes
    row.price_min = ns.price_min
    row.price_max = ns.price_max
    row.content_hash = content_hash
    row.last_seen_at = now
