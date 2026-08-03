"""Watch matching, seat preferences, thresholds, cooldown and digesting."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from tg.config import SeatPreference, WatchMatch
from tg.core.diff import Change, ChangeType, sync_events, sync_screenings
from tg.core.normalize import Seat, SeatStatus
from tg.core.watches import evaluate, screening_matches, seat_runs

AUG_4_1640_UTC = datetime(2026, 8, 4, 14, 40, tzinfo=UTC)  # 16:40 Prague


def _match(**kw) -> WatchMatch:
    return WatchMatch.model_validate(kw)


def _check(match: WatchMatch, **kw) -> bool:
    defaults = dict(
        title="Odyssea",
        auditorium="IMAX VOLVO",
        venue_external_id="1052",
        starts_at=AUG_4_1640_UTC,
        formats=["2D", "FILM_70MM", "SUBTITLED"],
        tz_name="Europe/Prague",
    )
    return screening_matches(match, **(defaults | kw))


# ------------------------------------------------------------------ matching


def test_empty_match_accepts_everything():
    assert _check(_match())


def test_title_and_auditorium_regexes():
    assert _check(_match(title_regex="(?i)odyss"))
    assert not _check(_match(title_regex="(?i)dune"))
    assert _check(_match(auditorium_regex="(?i)imax"))
    assert not _check(_match(auditorium_regex="(?i)imax"), auditorium="Sál 04")


def test_format_filter_requires_all_listed_formats():
    assert _check(_match(formats=["FILM_70MM"]))
    assert not _check(_match(formats=["FILM_70MM"]), formats=["2D", "SUBTITLED"])
    assert not _check(_match(formats=["FILM_70MM", "IMAX"]))  # IMAX tag absent


def test_cinema_filter():
    assert _check(_match(cinemas=["1052"]))
    assert not _check(_match(cinemas=["1052"]), venue_external_id="1034")


def test_date_window_uses_local_time():
    assert _check(_match(date_from="2026-08-01", date_to="2026-08-31"))
    assert not _check(_match(date_from="2026-09-01"))


def test_time_window_uses_local_time():
    """16:40 Prague is 14:40 UTC — filtering on the UTC value would wrongly exclude it."""
    assert _check(_match(time_between=["16:00", "23:00"]))
    assert not _check(_match(time_between=["09:00", "12:00"]))


def test_time_window_can_wrap_past_midnight():
    late = datetime(2026, 8, 4, 23, 30, tzinfo=UTC)
    assert _check(_match(time_between=["22:00", "02:00"]), starts_at=late)


def test_weekday_filter():
    assert _check(_match(weekdays=["tue"]))  # 2026-08-04 is a Tuesday
    assert not _check(_match(weekdays=["sat", "sun"]))


# --------------------------------------------------------------- seat runs


def _row(row: str, seats: range, taken: set[int] = frozenset()) -> list[Seat]:
    return [
        Seat(row, str(n), SeatStatus.TAKEN if n in taken else SeatStatus.AVAILABLE)
        for n in seats
    ]


def test_finds_adjacent_pairs():
    runs = seat_runs(_row("10", range(10, 15)), None, min_contiguous=2)
    assert runs and runs[0].size == 5


def test_gap_breaks_a_run():
    """Seats 12 and 14 free with 13 taken is not a pair — the whole point of asking
    for contiguity."""
    seats = _row("10", range(10, 16), taken={10, 11, 13, 15})
    assert seat_runs(seats, None, min_contiguous=2) == []
    assert len(seat_runs(seats, None, min_contiguous=1)) == 2


def test_preference_restricts_rows_and_seat_numbers():
    pref = SeatPreference(rows=[8, 14], seat_range=[10, 20], avoid_rows=[1, 2, 3])
    seats = _row("2", range(10, 20)) + _row("9", range(10, 20)) + _row("20", range(10, 20))
    runs = seat_runs(seats, pref, min_contiguous=2)
    assert [r.row_label for r in runs] == ["9"]


def test_preference_excludes_out_of_range_seat_numbers():
    pref = SeatPreference(rows=[8, 14], seat_range=[10, 20])
    runs = seat_runs(_row("9", range(1, 9)), pref, min_contiguous=2)
    assert runs == []


def test_runs_are_ranked_largest_first():
    seats = _row("9", range(10, 13)) + _row("10", range(10, 18))
    runs = seat_runs(seats, None, min_contiguous=2)
    assert [r.size for r in runs] == [8, 3]
    assert "row 10" in runs[0].describe()


# ------------------------------------------------------------- evaluation


@pytest.fixture
def seeded(session, adapter, fixture_body):
    body = fixture_body("film-events-1052-2026-08-04.json")
    events = [adapter._to_event(f) for f in body["films"]]
    screenings = [adapter._to_screening(e, {}) for e in body["events"]]
    sync_events(session, events)
    sync_screenings(session, "cinemacity_cz", screenings, covered_dates=set())
    from tg.core.diff import sync_venues
    from tg.core.normalize import NormVenue

    flora = NormVenue(source="cinemacity_cz", external_id="1052", name="Praha Flora")
    sync_venues(session, [flora])
    return screenings


def _rise(screening, delta: float) -> Change:
    before = screening.availability_ratio
    return Change(
        change_type=ChangeType.AVAILABILITY_RISE,
        source="cinemacity_cz",
        screening_key=screening.key,
        event_key=screening.event_key,
        old={"availability_ratio": before},
        new={"availability_ratio": before + delta},
        screening=screening,
    )


def test_alert_fires_for_a_matching_screening(session, config, seeded):
    imax = next(s for s in seeded if s.auditorium == "IMAX VOLVO")
    alerts = evaluate(session, config, [_rise(imax, 0.02)])

    assert len(alerts) == 1
    assert alerts[0].watch_name == "Odyssea IMAX 70mm"
    assert "Seats freed up" in alerts[0].title
    assert "IMAX VOLVO" in alerts[0].body
    assert alerts[0].channels == ["discord"]
    assert alerts[0].url and "tickets.cinemacity.cz" in alerts[0].url


def test_non_matching_screening_is_ignored(session, config, seeded):
    ordinary = next(s for s in seeded if s.auditorium == "Sál 04")
    assert evaluate(session, config, [_rise(ordinary, 0.02)]) == []


def test_sub_threshold_movement_does_not_alert(session, config, seeded):
    """Availability jitters constantly; only a meaningful rise should wake anyone."""
    imax = next(s for s in seeded if s.auditorium == "IMAX VOLVO")
    assert evaluate(session, config, [_rise(imax, 0.001)]) == []
    assert evaluate(session, config, [_rise(imax, 0.02)])


def test_cooldown_suppresses_a_repeat(session, config, seeded):
    imax = next(s for s in seeded if s.auditorium == "IMAX VOLVO")
    assert len(evaluate(session, config, [_rise(imax, 0.02)])) == 1
    assert evaluate(session, config, [_rise(imax, 0.02)]) == []


def test_cooldown_expires(session, config, seeded):
    imax = next(s for s in seeded if s.auditorium == "IMAX VOLVO")
    evaluate(session, config, [_rise(imax, 0.02)])
    later = datetime.utcnow() + timedelta(seconds=config.watches[0].cooldown_seconds + 60)
    assert len(evaluate(session, config, [_rise(imax, 0.02)], now=later)) == 1


def test_bursts_collapse_into_one_digest(session, config, seeded):
    """A newly published week is one event to a human, not eighty messages."""
    config.watches[0].digest_threshold = 3
    imax = [s for s in seeded if s.auditorium == "IMAX VOLVO"]
    alerts = evaluate(session, config, [_rise(s, 0.02) for s in imax])

    assert len(alerts) == 1
    assert alerts[0].payload["digest"] is True
    assert alerts[0].payload["count"] == len(imax)
    assert f"{len(imax)} ×" in alerts[0].title


def test_small_bursts_are_not_digested(session, config, seeded):
    config.watches[0].digest_threshold = 10
    imax = [s for s in seeded if s.auditorium == "IMAX VOLVO"]
    alerts = evaluate(session, config, [_rise(s, 0.02) for s in imax])
    assert len(alerts) == len(imax)
    assert all(not a.payload.get("digest") for a in alerts)


def test_watch_ignores_change_types_it_did_not_subscribe_to(session, config, seeded):
    imax = next(s for s in seeded if s.auditorium == "IMAX VOLVO")
    drop = Change(
        change_type=ChangeType.AVAILABILITY_DROP,
        source="cinemacity_cz",
        screening_key=imax.key,
        old={"availability_ratio": 0.5},
        new={"availability_ratio": 0.1},
        screening=imax,
    )
    assert evaluate(session, config, [drop]) == []
