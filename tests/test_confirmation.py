"""Looking again before saying that seats freed up.

``availabilityRatio`` counts seats that are not sitting in somebody's open checkout, so
on a house that is 98% sold most of its movement is a cart expiring and being re-taken
rather than a cancellation. One screening was measured going 5 -> 7 -> 5 seats inside
four minutes, and the alert in between sent someone to a full seat map.

The wait is modelled by patching ``asyncio.sleep`` with a function that changes the
seats, which is what actually happens during those ninety seconds.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest

from tg.core.adapter import Capability
from tg.core.scheduler import SourceRunner
from tg.core.timeutil import to_local, utcnow_aware

TZ = "Europe/Prague"


class _Stub:
    """Serves one captured day, and counts how often it was asked."""

    capabilities = {Capability.SCREENINGS, Capability.CALENDAR}

    def __init__(self, day, events, screenings):
        self.day = day
        self.events = events
        self.listing = screenings
        self.reads = 0

    async def calendar(self, since, until):
        return [self.day]

    async def screenings(self, since, until, dates=None):
        self.reads += 1
        return list(self.events.values()), self.listing

    async def venues(self):
        return []


@pytest.fixture
def imax(mapped):
    """The Aug 4 payload, moved ahead so its showtimes are still to come."""
    events, screenings = mapped("film-events-1052-2026-08-04.json")
    offset = (utcnow_aware() - screenings[0].starts_at).days + 30
    for s in screenings:
        s.starts_at = s.starts_at + timedelta(days=offset)
    target = next(s for s in screenings if s.auditorium == "IMAX VOLVO")
    return events, screenings, target


@pytest.fixture
def runner(config, session, imax):
    from tg.core.diff import sync_venues
    from tg.core.normalize import NormVenue

    sync_venues(
        session, [NormVenue(source="cinemacity_cz", external_id="1052", name="Praha Flora")]
    )
    session.commit()
    events, screenings, _ = imax
    day = to_local(screenings[0].starts_at, TZ).date()
    r = SourceRunner("cinemacity_cz", config, _Stub(day, events, screenings))  # type: ignore[arg-type]
    r.cycle = 1
    return r


@pytest.fixture
def during_the_wait(monkeypatch):
    """Replace the confirmation delay with whatever the checkout would have done."""

    def _install(action):
        async def _fake_sleep(_seconds):
            action()

        monkeypatch.setattr(asyncio, "sleep", _fake_sleep)

    return _install


async def _seed(runner):
    """Cold start: the baseline is seeded silently, so nothing is confirmed."""
    await runner.poll()
    assert runner.adapter.reads == 1, "a cold start must not spend a confirmation read"


async def test_a_rise_that_reverts_is_never_announced(runner, imax, during_the_wait):
    """The exact production case: two seats appear, and a checkout takes them straight
    back. The first reading was true and the notification would still have been useless."""
    _, _, target = imax
    await _seed(runner)

    floor = target.availability_ratio
    target.availability_ratio = floor + 0.02
    during_the_wait(lambda: setattr(target, "availability_ratio", floor))

    report = await runner.poll()
    assert runner.adapter.reads == 3, "the rise should have been re-read"
    assert report.alerts == []


async def test_a_rise_that_holds_is_announced_once(runner, imax, during_the_wait):
    """A block that genuinely came back is still there ninety seconds later."""
    _, _, target = imax
    await _seed(runner)

    target.availability_ratio = target.availability_ratio + 0.02
    during_the_wait(lambda: None)

    report = await runner.poll()
    assert len(report.alerts) == 1
    assert "still free 90s later" in report.alerts[0].body


async def test_a_partial_revert_is_judged_on_what_is_left(runner, imax, during_the_wait):
    """Not a yes/no re-check: the confirmed reading replaces the first one, so the
    ordinary threshold decides on the seats that actually remain."""
    _, _, target = imax
    await _seed(runner)

    floor = target.availability_ratio
    target.availability_ratio = floor + 0.05
    # Most of it goes back, leaving less than the threshold asks for.
    during_the_wait(lambda: setattr(target, "availability_ratio", floor + 0.002))

    assert (await runner.poll()).alerts == []


async def test_the_alert_quotes_the_confirmed_number_not_the_first_one(
    runner, imax, during_the_wait
):
    """Reporting the first reading would describe seats that are already gone."""
    _, _, target = imax
    await _seed(runner)

    floor = target.availability_ratio
    target.availability_ratio = floor + 0.05
    during_the_wait(lambda: setattr(target, "availability_ratio", floor + 0.03))

    report = await runner.poll()
    assert len(report.alerts) == 1
    confirmed = report.alerts[0].payload["new"]["availability_ratio"]
    assert confirmed == pytest.approx(floor + 0.03)


async def test_confirm_seconds_zero_restores_immediate_alerting(runner, imax):
    """The escape hatch, and proof the second request is skipped entirely."""
    _, _, target = imax
    runner.config.poll.confirm_seconds = 0
    await _seed(runner)

    target.availability_ratio = target.availability_ratio + 0.02
    report = await runner.poll()

    assert len(report.alerts) == 1
    assert runner.adapter.reads == 2, "no confirmation read should have been made"
    assert "still free" not in report.alerts[0].body


async def test_a_failed_re_read_falls_back_to_the_first_reading(
    runner, imax, during_the_wait, monkeypatch
):
    """A confirmation that cannot be made must not silently swallow the alert — the
    first reading was real, and losing it is worse than announcing it unconfirmed."""
    _, _, target = imax
    await _seed(runner)
    target.availability_ratio = target.availability_ratio + 0.02
    during_the_wait(lambda: None)

    calls = {"n": 0}
    original = runner.adapter.screenings

    async def _fail_on_confirm(*a, **kw):
        calls["n"] += 1
        if calls["n"] > 1:
            raise RuntimeError("upstream hiccup")
        return await original(*a, **kw)

    monkeypatch.setattr(runner.adapter, "screenings", _fail_on_confirm)

    report = await runner.poll()
    assert len(report.alerts) == 1
    assert "still free" not in report.alerts[0].body
