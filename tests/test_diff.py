"""Change detection, driven by two real snapshots of the same cinema."""

from __future__ import annotations

from datetime import UTC, date, datetime

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
    # The captured payload is a past date by now, and removals are only reported for
    # screenings still to come, so judge it from the morning it was captured.
    before_showtime = datetime(2026, 8, 4, 0, 0, tzinfo=UTC)
    sync_events(session, events)
    sync_screenings(
        session, "cinemacity_cz", screenings, covered_dates={AUG_4}, now=before_showtime
    )

    # Poll a different day and see nothing: the 4th's screenings are still fine.
    changes = sync_screenings(
        session, "cinemacity_cz", [], covered_dates={AUG_6}, now=before_showtime
    )
    assert changes == []

    # Poll the 4th and see nothing: now they really are gone.
    changes = sync_screenings(
        session, "cinemacity_cz", [], covered_dates={AUG_4}, now=before_showtime
    )
    assert {c.change_type for c in changes} == {ChangeType.SCREENING_REMOVED}
    assert len(changes) == len(screenings)


def test_links_appear_on_existing_rows_without_reporting_a_change(session, aug4, mapped):
    """The exact shape of the upgrade: a database seeded by the previous version, then
    polled by this one. The dated links must land on rows that already exist — they sit
    outside the content hash, so nothing else would ever write them — and doing so must
    not read as a change, or the first poll after a deploy alerts on every screening."""
    from sqlmodel import select

    from tg.models import Screening

    events, bare = aug4                       # mapped the old way: no deep links
    sync_events(session, events)
    sync_screenings(session, "cinemacity_cz", bare, covered_dates={AUG_4})
    assert session.exec(select(Screening)).first().info_url is None

    _, linked = mapped("film-events-1052-2026-08-04.json")
    assert sync_screenings(session, "cinemacity_cz", linked, covered_dates={AUG_4}) == []

    rows = session.exec(select(Screening)).all()
    assert all(r.info_url and "#/buy-tickets-by-film" in r.info_url for r in rows)
    assert all("at=2026-08-04" in r.info_url for r in rows)


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


def test_a_date_that_answered_with_nothing_is_not_coverage(session, aug4):
    """The flapping this fixes: five days in production marked future IMAX screenings
    removed and un-removed them on the next poll. A date the site answered emptily is
    indistinguishable from a date whose whole slate was cancelled, so the scheduler now
    only counts dates that came back with something — and if it ever passes one anyway,
    an empty screening list must not be read as proof."""
    events, screenings = aug4
    before_showtime = datetime(2026, 8, 4, 0, 0, tzinfo=UTC)
    sync_events(session, events)
    sync_screenings(
        session, "cinemacity_cz", screenings, covered_dates={AUG_4}, now=before_showtime
    )

    # Nothing came back at all. Absence of evidence, not evidence of absence.
    changes = sync_screenings(
        session, "cinemacity_cz", [], covered_dates=set(), now=before_showtime
    )
    assert changes == []


def test_a_screening_that_has_already_started_is_not_a_removal(session, aug4):
    """The site drops showtimes once they begin. That is the film starting, not a
    cancellation, and reporting it churned the change log every single day."""
    events, screenings = aug4
    sync_events(session, events)
    sync_screenings(
        session,
        "cinemacity_cz",
        screenings,
        covered_dates={AUG_4},
        now=datetime(2026, 8, 4, 0, 0, tzinfo=UTC),
    )

    # Same day, but after every showtime has begun.
    changes = sync_screenings(
        session,
        "cinemacity_cz",
        [],
        covered_dates={AUG_4},
        now=datetime(2026, 8, 5, 0, 0, tzinfo=UTC),
    )
    assert changes == []


def test_late_night_screenings_are_dated_the_way_the_cinema_dates_them(session, adapter):
    """A 00:30 Prague show is the *previous* day in UTC. Comparing the UTC date against
    the local-dated days the poller fetched would make it look unpolled forever."""
    from tg.core.normalize import NormScreening

    late = NormScreening(
        source="cinemacity_cz",
        external_id="999999",
        event_external_id="7268s2r",
        venue_external_id="1052",
        starts_at=datetime(2026, 8, 9, 22, 30, tzinfo=UTC),  # 00:30 on the 10th, Prague
        auditorium="IMAX VOLVO",
    )
    sync_screenings(
        session,
        "cinemacity_cz",
        [late],
        covered_dates={date(2026, 8, 10)},
        now=datetime(2026, 8, 1, tzinfo=UTC),
    )

    changes = sync_screenings(
        session,
        "cinemacity_cz",
        [],
        covered_dates={date(2026, 8, 10)},
        now=datetime(2026, 8, 1, tzinfo=UTC),
    )
    assert {c.change_type for c in changes} == {ChangeType.SCREENING_REMOVED}
