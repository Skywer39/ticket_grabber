"""Polling orchestration.

The cost model drives the design. Sweeping a 60-day horizon costs one request per
date; doing that every 45 seconds would be both slow and rude. So each cycle:

1. Ask the cheap calendar endpoint which dates have anything on sale (1 request).
   A date appearing here is the earliest possible sign a new program was published.
2. Fetch full detail for dates that are *new*, plus a rotating slice of the dates
   watches actually care about.
3. Only if tier 1 says a watched screening's availability moved, spend a browser on
   reading its exact seat map.

That keeps a hot-mode cycle at roughly nine requests while still noticing an early
release within one cycle.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from sqlmodel import Session, select

from tg.adapters.base_http import PoliteClient
from tg.assist.checkout import CheckoutAssistant
from tg.config import WEEKDAYS, AppConfig, HotWindow
from tg.core.adapter import Capability, SourceAdapter, build_adapter
from tg.core.diff import (
    Change,
    ChangeType,
    record_seatmap,
    sync_calendar,
    sync_events,
    sync_screenings,
    sync_venues,
)
from tg.core.normalize import NormScreening
from tg.core.ratelimit import jittered
from tg.core.timeutil import DEFAULT_TZ, from_db, to_local, utcnow_aware
from tg.core.watches import evaluate, screening_matches
from tg.db import session_scope
from tg.models import Alert, PollState, Screening, utcnow
from tg.notify.base import Dispatcher

log = logging.getLogger(__name__)

#: Refresh the venue list rarely — cinemas do not open every afternoon.
VENUE_REFRESH_CYCLES = 40


@dataclass
class PollReport:
    source: str
    dates_probed: int = 0
    dates_fetched: int = 0
    new_dates: list[str] = field(default_factory=list)
    screenings_seen: int = 0
    changes: list[Change] = field(default_factory=list)
    alerts: list[Alert] = field(default_factory=list)
    seatmaps_read: int = 0
    error: str | None = None

    def summary(self) -> str:
        if self.error:
            return f"{self.source}: ERROR {self.error}"
        bits = [
            f"{self.source}: {self.dates_fetched}/{self.dates_probed} dates",
            f"{self.screenings_seen} screenings",
            f"{len(self.changes)} changes",
            f"{len(self.alerts)} alerts",
        ]
        if self.new_dates:
            bits.append(f"NEW DATES {', '.join(self.new_dates)}")
        if self.seatmaps_read:
            bits.append(f"{self.seatmaps_read} seat maps")
        return ", ".join(bits)


def is_hot(now_local: datetime, windows: list[HotWindow]) -> bool:
    """Whether ``now`` falls inside any configured heightened-polling window."""
    for w in windows:
        if w.date_from and now_local.date() < w.date_from:
            continue
        if w.date_to and now_local.date() > w.date_to:
            continue
        if w.weekdays and now_local.weekday() not in {WEEKDAYS[d] for d in w.weekdays}:
            continue
        if w.start and w.end:
            t = now_local.time()
            inside = w.start <= t <= w.end if w.start <= w.end else (t >= w.start or t <= w.end)
            if not inside:
                continue
        return True
    return False


class SourceRunner:
    """Owns one configured source and everything needed to poll it once."""

    def __init__(
        self,
        key: str,
        config: AppConfig,
        adapter: SourceAdapter,
        assistant: CheckoutAssistant | None = None,
    ) -> None:
        self.key = key
        self.config = config
        self.adapter = adapter
        self.assistant = assistant
        self.cycle = 0

    @property
    def tz_name(self) -> str:
        return self.config.sources[self.key].options.get("timezone", DEFAULT_TZ)

    @property
    def horizon_days(self) -> int:
        return int(self.config.sources[self.key].options.get("days_ahead", 60))

    @property
    def dates_per_cycle(self) -> int:
        return int(self.config.sources[self.key].options.get("dates_per_cycle", 8))

    async def poll(self, dispatcher: Dispatcher | None = None) -> PollReport:
        report = PollReport(source=self.key)
        try:
            await self._poll_inner(report, dispatcher)
        except Exception as exc:  # noqa: BLE001 — one bad source must not stop the loop
            report.error = f"{type(exc).__name__}: {exc}"
            log.exception("poll of %s failed", self.key)
            with session_scope() as session:
                self._record_error(session, str(exc))
        self.cycle += 1
        return report

    def _is_cold_start(self) -> bool:
        """True before this source has ever completed a poll.

        On a cold start every screening is technically 'new', which would fire an
        alert per screening on first run. The first poll therefore seeds the baseline
        silently and only later differences are worth waking someone for.
        """
        with session_scope() as session:
            state = session.exec(
                select(PollState).where(PollState.cache_key == f"{self.key}:health")
            ).first()
            return state is None or state.last_polled_at is None

    async def _poll_inner(self, report: PollReport, dispatcher: Dispatcher | None) -> None:
        cold_start = self._is_cold_start()
        today = to_local(utcnow_aware(), self.tz_name).date()
        horizon = today + timedelta(days=self.horizon_days)

        refresh_venues = self.cycle % VENUE_REFRESH_CYCLES == 0
        if refresh_venues and Capability.VENUES in self.adapter.capabilities:
            venues = await self.adapter.venues()
            with session_scope() as session:
                sync_venues(session, venues)

        # --- tier 1a: the cheap calendar probe -----------------------------
        calendar = None
        calendar_changes: list[Change] = []
        if Capability.CALENDAR in self.adapter.capabilities:
            calendar = await self.adapter.calendar(today, horizon)
            if calendar is not None:
                report.dates_probed = len(calendar)
                with session_scope() as session:
                    calendar_changes = sync_calendar(session, self.key, calendar)
                report.new_dates = [c.new["date"] for c in calendar_changes]

        candidates = calendar if calendar is not None else self._dense_range(today, horizon)
        report.dates_probed = report.dates_probed or len(candidates)

        # --- tier 1b: fetch detail for new + rotating dates -----------------
        # A cold start sweeps every watched date in one go. Paying ~30 requests once
        # buys a complete baseline; rotating instead would make each later cycle
        # rediscover untouched dates and report them as newly published.
        if cold_start:
            targets = sorted(d for d in candidates if self._is_watched(d)) or sorted(candidates)
        else:
            targets = self._select_dates(candidates, report.new_dates)
        report.dates_fetched = len(targets)

        events, screenings = ([], [])
        if targets:
            events, screenings = await self.adapter.screenings(today, horizon, dates=targets)
        report.screenings_seen = len(screenings)

        with session_scope() as session:
            changes = list(calendar_changes)
            changes.extend(sync_events(session, events))
            changes.extend(
                sync_screenings(
                    session,
                    self.key,
                    screenings,
                    covered_dates=set(targets),
                    detect_removals=bool(targets),
                )
            )
            report.changes = changes
            self._record_success(session, len(screenings))

        # --- tier 2: exact seats, only where tier 1 says something moved ----
        if self.config.seatmap.enabled and Capability.SEATMAP in self.adapter.capabilities:
            titles = {e.external_id: e.title for e in events}
            seat_changes = await self._read_seatmaps(report.changes, screenings, titles)
            report.changes.extend(seat_changes)
            report.seatmaps_read = len({c.screening_key for c in seat_changes})

        # --- alerts ---------------------------------------------------------
        if cold_start:
            log.info(
                "%s: seeded baseline with %d screenings across %d dates — "
                "alerting starts from the next poll",
                self.key,
                len(screenings),
                len(targets),
            )
        else:
            with session_scope() as session:
                report.alerts = evaluate(session, self.config, report.changes)

        if dispatcher and report.alerts:
            sent, failed = await dispatcher.deliver(report.alerts)
            log.info("delivered %d alert(s), %d failed", sent, failed)
            with session_scope() as session:
                for alert in report.alerts:
                    session.merge(alert)

        if self.assistant and report.alerts:
            await self._maybe_assist(report.alerts)

    async def _maybe_assist(self, alerts: list[Alert]) -> None:
        """Open checkout for alerts belonging to a watch that armed it.

        Deliberately at most one per cycle: this puts a browser window in front of a
        human, and doing that repeatedly would be hostile.
        """
        assert self.assistant is not None
        for alert in alerts:
            try:
                watch = self.config.watch(alert.watch_name)
            except KeyError:
                continue
            if watch.assist != "arm" or not alert.url:
                continue

            profile = self.config.profiles.get(watch.seats.profile or "")
            result = await self.assistant.assist(
                alert.url,
                alert.screening_key,
                preference=profile,
                min_contiguous=watch.seats.min_contiguous,
            )
            log.info("assist for %s: %s", alert.screening_key, result.summary())
            return

    def _dense_range(self, start: date, end: date) -> list[date]:
        return [start + timedelta(days=i) for i in range((end - start).days + 1)]

    def _select_dates(self, candidates: list[date], new_dates: list[str]) -> list[date]:
        """New dates always; the rest by rotation so every date is refreshed in turn."""
        if not candidates:
            return []

        forced = {date.fromisoformat(d) for d in new_dates}
        watched = [d for d in candidates if self._is_watched(d)] or candidates

        budget = max(1, self.dates_per_cycle)
        rotation = [d for d in watched if d not in forced]

        with session_scope() as session:
            cursor = self._rotation_cursor(session)
            picked: list[date] = []
            if rotation:
                for i in range(min(budget, len(rotation))):
                    picked.append(rotation[(cursor + i) % len(rotation)])
                self._set_rotation_cursor(session, (cursor + len(picked)) % len(rotation))

        return sorted(forced | set(picked))

    def _is_watched(self, day: date) -> bool:
        """True when at least one enabled watch could care about this date."""
        relevant = [w for w in self.config.watches if w.enabled and w.source == self.key]
        if not relevant:
            return False
        for w in relevant:
            if w.match.date_from and day < w.match.date_from:
                continue
            if w.match.date_to and day > w.match.date_to:
                continue
            if w.match.weekdays and day.weekday() not in {WEEKDAYS[d] for d in w.match.weekdays}:
                continue
            return True
        return False

    async def _read_seatmaps(
        self, changes: list[Change], screenings: list[NormScreening], titles: dict[str, str]
    ) -> list[Change]:
        """Read exact seat maps for watched screenings whose availability moved."""
        by_key = {s.key: s for s in screenings}
        interesting = {
            c.screening_key
            for c in changes
            if c.change_type
            in (ChangeType.AVAILABILITY_RISE, ChangeType.NEW_SCREENING, ChangeType.BACK_ON_SALE)
            and c.screening_key
        }
        if not interesting:
            return []

        out: list[Change] = []
        min_interval = timedelta(seconds=self.config.seatmap.min_interval_seconds)
        now = utcnow_aware()

        for key in sorted(interesting):
            screening = by_key.get(key)
            if screening is None:
                continue
            if not self._wants_seatmap(screening, titles.get(screening.event_external_id)):
                continue
            with session_scope() as session:
                row = session.exec(select(Screening).where(Screening.key == key)).first()
                if row and row.seatmap_checked_at:
                    if now - from_db(row.seatmap_checked_at) < min_interval:
                        continue
            try:
                seatmap = await self.adapter.seatmap(screening)
            except Exception as exc:  # noqa: BLE001 — tier 2 is best-effort by design
                log.warning("seat map read failed for %s: %s", key, exc)
                continue
            if seatmap is None:
                continue
            with session_scope() as session:
                seat_changes, _ = record_seatmap(session, key, seatmap)
                row = session.exec(select(Screening).where(Screening.key == key)).first()
                if row:
                    row.seatmap_checked_at = utcnow()
                    session.add(row)
                out.extend(seat_changes)
        return out

    def _wants_seatmap(self, screening: NormScreening, title: str | None) -> bool:
        """Only screenings on a watch that actually asks for seat-level detail."""
        for w in self.config.watches:
            if not w.enabled or w.source != self.key or not w.seats.profile:
                continue
            if screening_matches(
                w.match,
                title=title,
                auditorium=screening.auditorium,
                venue_external_id=screening.venue_external_id,
                starts_at=screening.starts_at,
                formats=sorted(str(f) for f in screening.formats),
                tz_name=self.tz_name,
            ):
                return True
        return False

    # ------------------------------------------------------------- poll state

    def _state(self, session: Session, suffix: str) -> PollState:
        cache_key = f"{self.key}:{suffix}"
        state = session.exec(select(PollState).where(PollState.cache_key == cache_key)).first()
        if state is None:
            state = PollState(cache_key=cache_key, source=self.key)
            session.add(state)
        return state

    def _rotation_cursor(self, session: Session) -> int:
        return int((self._state(session, "rotation").data or {}).get("cursor", 0))

    def _set_rotation_cursor(self, session: Session, cursor: int) -> None:
        state = self._state(session, "rotation")
        state.data = {"cursor": cursor}
        session.add(state)

    def _record_success(self, session: Session, screening_count: int) -> None:
        state = self._state(session, "health")
        state.last_polled_at = utcnow()
        state.consecutive_errors = 0
        state.last_error = None
        # A source that suddenly returns nothing is usually a site change rather than
        # an empty schedule — this counter is what `tg adapter heal` looks at.
        state.consecutive_empty = 0 if screening_count else state.consecutive_empty + 1
        session.add(state)

    def _record_error(self, session: Session, message: str) -> None:
        state = self._state(session, "health")
        state.consecutive_errors += 1
        state.last_error = message[:500]
        state.last_polled_at = utcnow()
        session.add(state)


class Engine:
    """Builds runners from config and drives the polling loop."""

    def __init__(self, config: AppConfig, dispatcher: Dispatcher | None = None) -> None:
        self.config = config
        self.dispatcher = dispatcher
        self.client = PoliteClient(config.http)
        self.runners: list[SourceRunner] = []
        self.assistant = CheckoutAssistant(config.assist) if config.assist.enabled else None

    async def setup(self) -> None:
        for key, source in self.config.sources.items():
            if not source.enabled:
                continue
            adapter = build_adapter(key, source, self.client)
            await adapter.setup()
            if self.config.seatmap.enabled and Capability.SEATMAP in adapter.capabilities:
                if reader := type(adapter).make_seat_reader(self.config.seatmap):
                    adapter.attach_seat_reader(reader)
            self.runners.append(SourceRunner(key, self.config, adapter, self.assistant))
        if not self.runners:
            raise RuntimeError("no enabled sources in config")

    async def poll_once(self) -> list[PollReport]:
        sem = asyncio.Semaphore(self.config.poll.max_concurrency)

        async def run(runner: SourceRunner) -> PollReport:
            async with sem:
                return await runner.poll(self.dispatcher)

        return list(await asyncio.gather(*(run(r) for r in self.runners)))

    def next_delay(self) -> float:
        now_local = to_local(utcnow_aware(), self.runners[0].tz_name if self.runners else None)
        hot = is_hot(now_local, self.config.poll.hot_windows)
        base = self.config.poll.hot_seconds if hot else self.config.poll.baseline_seconds
        return jittered(base, self.config.poll.jitter_ratio)

    async def run_forever(self, stop: asyncio.Event | None = None) -> None:
        stop = stop or asyncio.Event()
        while not stop.is_set():
            for report in await self.poll_once():
                log.info("%s", report.summary())
            delay = self.next_delay()
            log.debug("sleeping %.0fs", delay)
            try:
                await asyncio.wait_for(stop.wait(), timeout=delay)
            except TimeoutError:
                pass

    async def aclose(self) -> None:
        for runner in self.runners:
            await runner.adapter.aclose()
        await self.client.aclose()
