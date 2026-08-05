"""Watch evaluation: decide which detected changes are worth waking someone for.

The diff engine says what changed; this module says whether *you* care. Splitting it
this way lets several watches with different sensitivities read one change log.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlmodel import Session, col, select

from tg.config import WEEKDAYS, AppConfig, SeatPreference, WatchConfig, WatchMatch
from tg.core.diff import Change, ChangeType
from tg.core.normalize import Seat, parse_row_index, parse_seat_index
from tg.core.timeutil import DEFAULT_TZ, format_local, from_db, to_local
from tg.models import Alert, Event, Screening, Venue, utcnow

log = logging.getLogger(__name__)

#: Change types that describe a specific screening. NEW_DATE and NEW_EVENT do not.
SCREENING_CHANGES = {
    ChangeType.NEW_SCREENING,
    ChangeType.SCREENING_REMOVED,
    ChangeType.AVAILABILITY_RISE,
    ChangeType.AVAILABILITY_DROP,
    ChangeType.SOLD_OUT,
    ChangeType.BACK_ON_SALE,
    ChangeType.PRICE_CHANGE,
    ChangeType.SEAT_FREED,
}


@dataclass(slots=True)
class SeatRun:
    """A block of adjacent free seats in one row."""

    row_label: str
    seats: list[Seat]
    section: str | None = None

    @property
    def size(self) -> int:
        return len(self.seats)

    def describe(self) -> str:
        labels = [s.seat_label for s in self.seats]
        where = f"{self.section} " if self.section else ""
        return f"{where}row {self.row_label} seats {', '.join(labels)}"


def seat_runs(
    seats: list[Seat], preference: SeatPreference | None, min_contiguous: int = 1
) -> list[SeatRun]:
    """Find blocks of ``min_contiguous`` adjacent free seats that satisfy ``preference``.

    Adjacency uses the numeric seat index, so a gap in numbering correctly breaks a
    run rather than silently pretending two seats are neighbours.
    """
    candidates = [s for s in seats if s.is_available and _seat_allowed(s, preference)]
    by_row: dict[tuple[str | None, str], list[Seat]] = {}
    for s in candidates:
        by_row.setdefault((s.section, s.row_label), []).append(s)

    runs: list[SeatRun] = []
    for (section, row_label), row_seats in by_row.items():
        indexed = [s for s in row_seats if _seat_index(s) is not None]
        indexed.sort(key=lambda s: _seat_index(s))  # type: ignore[arg-type,return-value]

        current: list[Seat] = []
        for seat in indexed:
            if current and _seat_index(seat) != _seat_index(current[-1]) + 1:
                if len(current) >= min_contiguous:
                    runs.append(SeatRun(row_label, current, section))
                current = []
            current.append(seat)
        if len(current) >= min_contiguous:
            runs.append(SeatRun(row_label, current, section))

    runs.sort(key=lambda r: (-r.size, r.row_label))
    return runs


def _seat_index(seat: Seat) -> int | None:
    return seat.seat_index if seat.seat_index is not None else parse_seat_index(seat.seat_label)


def _row_index(seat: Seat) -> int | None:
    return seat.row_index if seat.row_index is not None else parse_row_index(seat.row_label)


def _seat_allowed(seat: Seat, pref: SeatPreference | None) -> bool:
    if pref is None:
        return True
    row = _row_index(seat)
    if row is not None:
        if row in pref.avoid_rows:
            return False
        if pref.rows and not (pref.rows[0] <= row <= pref.rows[1]):
            return False
    idx = _seat_index(seat)
    if idx is not None and pref.seat_range:
        if not (pref.seat_range[0] <= idx <= pref.seat_range[1]):
            return False
    return True


def screening_matches(
    match: WatchMatch,
    *,
    title: str | None,
    auditorium: str | None,
    venue_external_id: str | None,
    starts_at: datetime | None,
    formats: list[str],
    tz_name: str = DEFAULT_TZ,
) -> bool:
    """All specified criteria must hold; unspecified criteria are ignored."""
    if match.title_regex and not (title and re.search(match.title_regex, title)):
        return False
    if match.auditorium_regex and not (
        auditorium and re.search(match.auditorium_regex, auditorium)
    ):
        return False
    if match.cinemas and (venue_external_id is None or venue_external_id not in match.cinemas):
        return False
    if match.formats and not set(match.formats).issubset(set(formats)):
        return False

    if starts_at is not None:
        local = to_local(starts_at, tz_name)
        if match.date_from and local.date() < match.date_from:
            return False
        if match.date_to and local.date() > match.date_to:
            return False
        if match.weekdays and local.weekday() not in {WEEKDAYS[d] for d in match.weekdays}:
            return False
        if match.time_between:
            start, end = match.time_between
            t = local.time()
            in_window = start <= t <= end if start <= end else (t >= start or t <= end)
            if not in_window:
                return False
    return True


def _threshold_ok(watch: WatchConfig, change: Change) -> bool:
    """Availability noise filter. The ratio jitters constantly as carts are held."""
    delta = change.delta
    if change.change_type is ChangeType.AVAILABILITY_RISE:
        if delta is None or delta < watch.trigger.availability_rise_min:
            return False
    elif change.change_type is ChangeType.AVAILABILITY_DROP:
        if delta is None or abs(delta) < watch.trigger.availability_drop_min:
            return False

    if watch.trigger.max_availability is not None:
        current = change.new.get("availability_ratio")
        if current is not None and current > watch.trigger.max_availability:
            return False
    return True


def _in_cooldown(session: Session, watch: WatchConfig, dedupe_key: str, now: datetime) -> bool:
    cutoff = now - timedelta(seconds=watch.cooldown_seconds)
    recent = session.exec(
        select(Alert)
        .where(
            Alert.watch_name == watch.name,
            Alert.screening_key == dedupe_key,
            col(Alert.created_at) >= cutoff,
        )
        .limit(1)
    ).first()
    return recent is not None


@dataclass(slots=True)
class _Resolved:
    title: str | None
    auditorium: str | None
    venue_name: str | None
    venue_external_id: str | None
    starts_at: datetime | None
    formats: list[str]
    booking_url: str | None
    availability_ratio: float | None
    #: Film page, e.g. /films/odyssea/7268s2r — a plain document listing showtimes.
    event_url: str | None = None
    #: Cinema programme page, e.g. /cinemas/flora. Same idea, scoped to the venue.
    venue_url: str | None = None
    #: The same two pages, but opened on the screening's own date.
    info_url: str | None = None
    venue_info_url: str | None = None


def _resolve(session: Session, change: Change) -> _Resolved:
    """Gather the display/matching context for a change from the carried
    normalized object where possible, falling back to the database."""
    if change.screening is not None:
        ns = change.screening
        title = None
        ev = session.exec(select(Event).where(Event.key == ns.event_key)).first()
        if ev:
            title = ev.title
        venue = session.exec(select(Venue).where(Venue.key == ns.venue_key)).first()
        return _Resolved(
            title=title,
            auditorium=ns.auditorium,
            venue_name=venue.name if venue else ns.venue_name,
            venue_external_id=ns.venue_external_id,
            starts_at=ns.starts_at,
            formats=sorted(str(f) for f in ns.formats),
            booking_url=ns.booking_url,
            availability_ratio=ns.availability_ratio,
            event_url=ev.url if ev else None,
            venue_url=venue.url if venue else None,
            info_url=ns.info_url,
            venue_info_url=ns.venue_info_url,
        )

    if change.screening_key:
        row = session.exec(select(Screening).where(Screening.key == change.screening_key)).first()
        if row:
            ev = session.exec(select(Event).where(Event.key == row.event_key)).first()
            venue = session.exec(select(Venue).where(Venue.key == row.venue_key)).first()
            return _Resolved(
                title=ev.title if ev else None,
                auditorium=row.auditorium,
                venue_name=venue.name if venue else None,
                venue_external_id=venue.external_id if venue else None,
                starts_at=from_db(row.starts_at),
                formats=list(row.formats or []),
                booking_url=row.booking_url,
                availability_ratio=row.availability_ratio,
                event_url=ev.url if ev else None,
                venue_url=venue.url if venue else None,
                info_url=row.info_url,
                venue_info_url=row.venue_info_url,
            )

    if change.event is not None:
        return _Resolved(
            title=change.event.title,
            auditorium=None,
            venue_name=None,
            venue_external_id=None,
            starts_at=None,
            formats=sorted(str(f) for f in change.event.formats),
            booking_url=change.event.url,
            availability_ratio=None,
        )

    return _Resolved(None, None, None, None, None, [], None, None)


def evaluate(
    session: Session,
    config: AppConfig,
    changes: list[Change],
    *,
    now: datetime | None = None,
    dry_run: bool = False,
) -> list[Alert]:
    """Turn changes into alerts, applying per-watch filters, thresholds and cooldown.

    Returns only the alerts that should actually be delivered. Alerts rolled into a
    digest are still persisted (they carry the cooldown and the history) but come back
    marked ``suppressed`` and are not returned for delivery.
    """
    now = now or utcnow()
    candidates = _candidate_alerts(session, config, changes, now, dry_run)
    deliverable = _rollup(config, candidates, now)

    if not dry_run:
        for alert in candidates:
            session.add(alert)
        for alert in deliverable:
            if alert.id is None:
                session.add(alert)

    return deliverable


def _candidate_alerts(
    session: Session,
    config: AppConfig,
    changes: list[Change],
    now: datetime,
    dry_run: bool,
) -> list[Alert]:
    alerts: list[Alert] = []
    for change in changes:
        for watch in config.watches:
            if not watch.enabled or watch.source != change.source:
                continue
            if str(change.change_type) not in watch.trigger.events:
                continue
            if not _threshold_ok(watch, change):
                continue

            tz_name = config.sources[watch.source].options.get("timezone", DEFAULT_TZ)
            ctx = _resolve(session, change)

            if change.change_type in SCREENING_CHANGES and not screening_matches(
                watch.match,
                title=ctx.title,
                auditorium=ctx.auditorium,
                venue_external_id=ctx.venue_external_id,
                starts_at=ctx.starts_at,
                formats=ctx.formats,
                tz_name=tz_name,
            ):
                continue

            dedupe_key = change.screening_key or f"{change.source}:{change.new.get('date', '-')}"
            if not dry_run and _in_cooldown(session, watch, dedupe_key, now):
                continue

            alerts.append(_build_alert(watch, change, ctx, dedupe_key, tz_name, now))

    return alerts


def _rollup(config: AppConfig, alerts: list[Alert], now: datetime) -> list[Alert]:
    """Collapse same-kind bursts into one digest per watch."""
    grouped: dict[tuple[str, str], list[Alert]] = {}
    for alert in alerts:
        grouped.setdefault((alert.watch_name, alert.change_type), []).append(alert)

    out: list[Alert] = []
    for (watch_name, change_type), group in grouped.items():
        watch = config.watch(watch_name)
        if len(group) <= watch.digest_threshold:
            out.extend(group)
            continue

        for alert in group:
            alert.suppressed = True
            alert.channels = []

        preview = "\n".join(f"• {a.title.split(' — ', 1)[-1]}: {_one_line(a)}" for a in group[:10])
        more = f"\n… and {len(group) - 10} more" if len(group) > 10 else ""
        out.append(
            Alert(
                watch_name=watch_name,
                screening_key=f"digest:{watch_name}:{change_type}",
                change_type=change_type,
                created_at=now,
                title=f"{len(group)} × {_headline(change_type)} — {watch_name}",
                body=f"{preview}{more}",
                url=next((a.url for a in group if a.url), None),
                payload={"count": len(group), "digest": True},
                channels=list(watch.notify),
            )
        )
    return out


def _one_line(alert: Alert) -> str:
    return " · ".join(part for part in alert.body.splitlines() if part)[:120]


def _headline(change_type: str) -> str:
    return change_type.replace("_", " ").lower()


def _build_alert(
    watch: WatchConfig,
    change: Change,
    ctx: _Resolved,
    dedupe_key: str,
    tz_name: str,
    now: datetime,
) -> Alert:
    headline = {
        ChangeType.NEW_SCREENING: "New screening on sale",
        ChangeType.NEW_DATE: "New date published",
        ChangeType.NEW_EVENT: "New title announced",
        ChangeType.AVAILABILITY_RISE: "Seats freed up",
        ChangeType.AVAILABILITY_DROP: "Selling fast",
        ChangeType.SOLD_OUT: "Sold out",
        ChangeType.BACK_ON_SALE: "Back on sale",
        ChangeType.SEAT_FREED: "Good seats available",
        ChangeType.PRICE_CHANGE: "Price changed",
        ChangeType.SCREENING_REMOVED: "Screening removed",
    }.get(change.change_type, str(change.change_type))

    title_bits = [headline]
    if ctx.title:
        title_bits.append(ctx.title)
    title = " — ".join(title_bits)

    lines: list[str] = []
    if ctx.venue_name or ctx.auditorium:
        lines.append(" / ".join(x for x in (ctx.venue_name, ctx.auditorium) if x))
    if ctx.starts_at:
        lines.append(format_local(ctx.starts_at, tz_name))
    if ctx.formats:
        lines.append(" ".join(ctx.formats))

    if change.change_type is ChangeType.NEW_DATE:
        lines.append(f"Date {change.new.get('date')} now has screenings on sale.")

    ratio = ctx.availability_ratio
    if ratio is not None:
        lines.append(f"{ratio * 100:.1f}% of seats free")
    if (delta := change.delta) is not None:
        lines.append(f"change {delta * 100:+.1f} pp")
    if freed := change.new.get("freed_seats"):
        preview = ", ".join(s.split("|")[-2] + "-" + s.split("|")[-1] for s in freed[:8])
        lines.append(f"freed: {preview}{' …' if len(freed) > 8 else ''}")

    # Link to pages, not to the booking flow.
    #
    # An event page is a document and is reliably GET-able. A booking URL is the
    # entrance to a stateful flow and frequently is not: on Cinema City the booking
    # link is POST-only (a GET returns 404 "Error Occurred") and the site's own
    # launcher auto-posts to a host that answers 403 to everyone. Neither survives
    # being clicked from a notification. Preferring the page is the right general
    # default; booking_url stays in the data for assist and the seat reader.
    #
    # Prefer the dated form of each page. An undated link opens on today, which is the
    # wrong day for every alert about a future screening — and once today's showtimes
    # for that film have passed, the page has nothing left to show at all.
    url = ctx.info_url or ctx.event_url or ctx.venue_info_url or ctx.venue_url or ctx.booking_url
    programme = ctx.venue_info_url or ctx.venue_url
    if programme and programme not in (url, ctx.booking_url):
        lines.append(f"cinema programme: {programme}")

    return Alert(
        watch_name=watch.name,
        screening_key=dedupe_key,
        change_type=str(change.change_type),
        created_at=now,
        title=title,
        body="\n".join(lines),
        url=url,
        payload={
            "old": change.old,
            "new": change.new,
            "formats": ctx.formats,
            "venue": ctx.venue_name,
            "auditorium": ctx.auditorium,
            "starts_at": ctx.starts_at.isoformat() if ctx.starts_at else None,
        },
        channels=list(watch.notify),
    )
