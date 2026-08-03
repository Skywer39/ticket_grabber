"""Change detection, driven by two real snapshots of the same cinema."""

from __future__ import annotations

from datetime import date

import pytest

from tg.core.diff import (
    ChangeType,
    sync_calendar,
    sync_events,
    sync_screenings,
)

AUG_4 = date(2026, 8, 4)
AUG_6 = date(2026, 8, 6)


@pytest.fixture
def aug4(adapter, fixture_body):
    body = fixture_body("film-events-1052-2026-08-04.json")
    return (
        [adapter._to_event(f) for f in body["films"]],
        [adapter._to_screening(e, {}) for e in body["events"]],
    )


def test_first_sync_reports_everything_as_new(session, aug4):
    events, screenings = aug4
    assert len(sync_events(session, events)) == len(events)

    changes = sync_screenings(session, "cinemacity_cz", screenings, covered_dates={AUG_4})
    assert len(changes) == len(screenings)
    assert {c.change_type for c in changes} == {ChangeType.NEW_SCREENING}


def test_resync_of_identical_data_is_silent(session, aug4):
    """The poller runs constantly; unchanged data must never produce noise."""
    events, screenings = aug4
    sync_events(session, events)
    sync_screenings(session, "cinemacity_cz", screenings, covered_dates={AUG_4})

    assert sync_screenings(session, "cinemacity_cz", screenings, covered_dates={AUG_4}) == []


def test_detects_seats_freeing_up(session, aug4):
    """Somebody cancels: the ratio rises. This is the alert worth waiting for."""
    events, screenings = aug4
    sync_events(session, events)
    sync_screenings(session, "cinemacity_cz", screenings, covered_dates={AUG_4})

    target = next(s for s in screenings if s.auditorium == "IMAX VOLVO")
    before = target.availability_ratio
    target.availability_ratio = before + 0.02

    changes = sync_screenings(session, "cinemacity_cz", screenings, covered_dates={AUG_4})
    assert len(changes) == 1
    assert changes[0].change_type is ChangeType.AVAILABILITY_RISE
    assert changes[0].delta == pytest.approx(0.02)
    assert changes[0].screening_key == target.key


def test_detects_selling_out(session, aug4):
    events, screenings = aug4
    sync_events(session, events)
    sync_screenings(session, "cinemacity_cz", screenings, covered_dates={AUG_4})

    target = next(s for s in screenings if s.auditorium == "IMAX VOLVO")
    target.availability_ratio = 0.0
    target.sold_out = True

    kinds = {
        c.change_type
        for c in sync_screenings(session, "cinemacity_cz", screenings, covered_dates={AUG_4})
    }
    assert kinds == {ChangeType.AVAILABILITY_DROP, ChangeType.SOLD_OUT}


def test_new_screenings_on_a_later_date_are_detected(session, adapter, fixture_body, aug4):
    """Feeding a second real snapshot mimics a program extension."""
    events, screenings = aug4
    sync_events(session, events)
    sync_screenings(session, "cinemacity_cz", screenings, covered_dates={AUG_4})

    body6 = fixture_body("film-events-1052-2026-08-06.json")
    later = [adapter._to_screening(e, {}) for e in body6["events"]]

    changes = sync_screenings(
        session, "cinemacity_cz", screenings + later, covered_dates={AUG_4, AUG_6}
    )
    assert {c.change_type for c in changes} == {ChangeType.NEW_SCREENING}
    assert len(changes) == len(later)


def test_absence_only_counts_on_dates_actually_polled(session, aug4):
    """A partial poll must not be mistaken for cancellations — this is the guard that
    stops a rotating fetch from inventing SCREENING_REMOVED for every other date."""
    events, screenings = aug4
    sync_events(session, events)
    sync_screenings(session, "cinemacity_cz", screenings, covered_dates={AUG_4})

    # Poll a different day and see nothing: the 4th's screenings are still fine.
    changes = sync_screenings(session, "cinemacity_cz", [], covered_dates={AUG_6})
    assert changes == []

    # Poll the 4th and see nothing: now they really are gone.
    changes = sync_screenings(session, "cinemacity_cz", [], covered_dates={AUG_4})
    assert {c.change_type for c in changes} == {ChangeType.SCREENING_REMOVED}
    assert len(changes) == len(screenings)


def test_calendar_diff_catches_a_newly_published_week(session):
    """The cheap probe that would have caught the early release."""
    known = [date(2026, 8, d) for d in range(3, 25)]
    assert sync_calendar(session, "cinemacity_cz", known) == []  # first sight seeds only

    published = known + [date(2026, 8, 25), date(2026, 8, 26)]
    changes = sync_calendar(session, "cinemacity_cz", published)

    assert [c.change_type for c in changes] == [ChangeType.NEW_DATE, ChangeType.NEW_DATE]
    assert [c.new["date"] for c in changes] == ["2026-08-25", "2026-08-26"]


def test_calendar_diff_is_quiet_when_nothing_is_published(session):
    dates = [date(2026, 8, d) for d in range(3, 25)]
    sync_calendar(session, "cinemacity_cz", dates)
    assert sync_calendar(session, "cinemacity_cz", dates) == []
