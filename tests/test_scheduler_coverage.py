"""What a poll cycle is allowed to conclude from a date it fetched.

The diff engine has always refused to report removals for days it did not look at. The
flapping seen in production came from one step earlier: the scheduler told it that every
date it *asked about* had been covered, including the ones the site answered emptily. A
single truncated response then marked a whole day as cancelled and the next poll put it
straight back — 10 spurious removals across five days, on screenings that were on sale
the whole time.

The captured payloads are replayed at future dates, because removals only apply to
screenings that have not started yet and the fixtures are now historical.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlmodel import select

from tg import db
from tg.core.adapter import Capability
from tg.core.normalize import NormScreening
from tg.core.scheduler import SourceRunner
from tg.core.timeutil import to_local, utcnow_aware
from tg.models import Screening

TZ = "Europe/Prague"


def _shift(screenings: list[NormScreening], days: int) -> list[NormScreening]:
    for s in screenings:
        s.starts_at = s.starts_at + timedelta(days=days)
    return screenings


class _StubAdapter:
    """Replays captured payloads for chosen dates and nothing for the rest."""

    capabilities = {Capability.SCREENINGS, Capability.CALENDAR}

    def __init__(self, by_date):
        self.by_date = by_date

    async def calendar(self, since, until):
        return sorted(self.by_date)

    async def screenings(self, since, until, dates=None):
        events, screenings = {}, []
        for day in dates or sorted(self.by_date):
            evs, scrs = self.by_date.get(day, ({}, []))
            events.update(evs)
            screenings.extend(scrs)
        return list(events.values()), screenings

    async def venues(self):
        return []


@pytest.fixture
def days(mapped):
    """Two captured days, moved far enough ahead that their showtimes are still to come."""
    offset = (utcnow_aware() - mapped("film-events-1052-2026-08-04.json")[1][0].starts_at).days + 30
    out = {}
    for name in ("film-events-1052-2026-08-04.json", "film-events-1052-2026-08-06.json"):
        events, screenings = mapped(name)
        _shift(screenings, offset)
        out[to_local(screenings[0].starts_at, TZ).date()] = (events, screenings)
    assert len(out) == 2
    return out


@pytest.fixture
def runner(config, session, days):
    from tg.core.diff import sync_venues
    from tg.core.normalize import NormVenue

    sync_venues(
        session, [NormVenue(source="cinemacity_cz", external_id="1052", name="Praha Flora")]
    )
    # The poller opens its own sessions per phase; leaving writes pending on this one
    # would deadlock SQLite against them.
    session.commit()
    r = SourceRunner("cinemacity_cz", config, _StubAdapter(days))  # type: ignore[arg-type]
    r.cycle = 1  # skip the venue refresh, which the stub does not model
    return r


def _live_screenings() -> list[Screening]:
    with db.session_scope() as s:
        return list(
            s.exec(
                select(Screening).where(Screening.disappeared_at.is_(None))  # type: ignore[union-attr]
            ).all()
        )


def _removed(report) -> set[str | None]:
    return {c.screening_key for c in report.changes if str(c.change_type) == "SCREENING_REMOVED"}


async def test_an_empty_answer_for_a_fetched_date_is_not_a_removal(runner, days):
    """Both dates seed, then the site stops answering for the second one. Nothing was
    cancelled — the response was simply empty — so nothing may be reported."""
    await runner.poll()  # cold start: seeds the baseline silently
    assert _live_screenings()

    second = sorted(days)[1]
    runner.adapter.by_date[second] = ({}, [])
    report = await runner.poll()

    assert _removed(report) == set()
    assert any(to_local(s.starts_at, TZ).date() == second for s in _live_screenings())


async def test_a_date_that_answers_normally_still_reports_real_removals(runner, days):
    """The guard must not blunt the real signal: a day that comes back with *some* of its
    screenings really has lost the ones now missing."""
    await runner.poll()

    second = sorted(days)[1]
    events, screenings = days[second]
    kept, dropped = screenings[:1], screenings[1:]
    assert dropped, "sanity: the fixture has more than one screening that day"

    runner.adapter.by_date[second] = (events, kept)
    report = await runner.poll()

    assert _removed(report) == {s.key for s in dropped}
