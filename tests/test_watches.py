"""Watch matching, seat preferences, thresholds, cooldown and digesting."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from tg.config import SeatPreference, WatchConfig, WatchMatch
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
def seeded(session, mapped):
    events, screenings = mapped("film-events-1052-2026-08-04.json")
    sync_events(session, list(events.values()))
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

    # Links to a page, not into the booking flow. On this site the booking link is
    # POST-only and the launcher posts to a 403 host, so neither is clickable.
    #
    # And to the page *on the screening's date*: an undated link opens on today, which
    # is never the day the alert is about.
    assert alerts[0].url == (
        "https://www.cinemacity.cz/films/odyssea/7268s2r"
        "#/buy-tickets-by-film"
        "?in-cinema=prague&at=2026-08-04&for-movie=7268s2r&view-mode=list"
    )
    assert "tickets." not in alerts[0].url
    assert "/api/order/" not in alerts[0].url
    assert "booking-router" not in alerts[0].url


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


def _blank_page_urls(session, screening, *, event: bool, venue: bool) -> None:
    """Clear the page URLs for exactly the sources this screening resolves through.

    Both the database rows and the carried normalized object: a site that publishes no
    film page also yields no film deep link, so blanking one without the other would
    describe a state that cannot occur.
    """
    from sqlmodel import select as _select

    from tg.models import Event as _Event
    from tg.models import Venue as _Venue

    if event:
        ev = session.exec(_select(_Event).where(_Event.key == screening.event_key)).first()
        ev.url = None
        session.add(ev)
        screening.info_url = None
    v = session.exec(_select(_Venue).where(_Venue.key == screening.venue_key)).first()
    v.url = None if venue else "https://www.cinemacity.cz/cinemas/flora"
    session.add(v)
    if venue:
        screening.venue_info_url = None


def test_alert_falls_back_to_the_dated_cinema_page_when_the_film_has_none(
    session, config, seeded
):
    """The venue's programme is the next-best document — still on the right date."""
    imax = next(s for s in seeded if s.auditorium == "IMAX VOLVO")
    _blank_page_urls(session, imax, event=True, venue=False)

    alerts = evaluate(session, config, [_rise(imax, 0.02)])
    assert alerts[0].url == (
        "https://www.cinemacity.cz/cinemas/flora"
        "#/buy-tickets-by-cinema?in-cinema=1052&at=2026-08-04&view-mode=list"
    )


def test_alert_falls_back_to_the_plain_cinema_page_when_there_is_no_dated_one(
    session, config, seeded
):
    imax = next(s for s in seeded if s.auditorium == "IMAX VOLVO")
    _blank_page_urls(session, imax, event=True, venue=False)
    imax.venue_info_url = None

    alerts = evaluate(session, config, [_rise(imax, 0.02)])
    assert alerts[0].url == "https://www.cinemacity.cz/cinemas/flora"


def test_booking_url_is_used_only_when_no_page_exists(session, config, seeded):
    imax = next(s for s in seeded if s.auditorium == "IMAX VOLVO")
    _blank_page_urls(session, imax, event=True, venue=True)

    alerts = evaluate(session, config, [_rise(imax, 0.02)])
    assert alerts[0].url == imax.booking_url


def test_body_carries_the_dated_cinema_programme_as_a_second_link(session, config, seeded):
    imax = next(s for s in seeded if s.auditorium == "IMAX VOLVO")
    body = evaluate(session, config, [_rise(imax, 0.02)])[0].body
    assert (
        "cinema programme: https://www.cinemacity.cz/cinemas/flora"
        "#/buy-tickets-by-cinema?in-cinema=1052&at=2026-08-04&view-mode=list" in body
    )


# ------------------------------------------------- seat counts and the floor
#
# The change these tests pin down: five days in production produced 63 "Seats freed up"
# alerts, every one of them a two-seat move on a 385-seat house that already rested at
# six permanently-unsold seats. All 63 were true and none was actionable.


@pytest.fixture
def hall(session, mapped):
    """Enough of the IMAX hall's history for its 385 seats to be recoverable.

    One date does not carry enough distinct ratios to pin a denominator, which is the
    estimator refusing to guess rather than a gap — so seed the three captured days.
    """
    from tg.core.diff import sync_venues
    from tg.core.normalize import NormVenue

    flora = NormVenue(source="cinemacity_cz", external_id="1052", name="Praha Flora")
    sync_venues(session, [flora])
    seeded = []
    for name in (
        "film-events-1052-2026-08-04.json",
        "film-events-1052-2026-08-06.json",
        "film-events-1052-2026-08-27.json",
    ):
        events, screenings = mapped(name)
        sync_events(session, list(events.values()))
        sync_screenings(session, "cinemacity_cz", screenings, covered_dates=set())
        seeded.extend(screenings)
    return seeded


def _floored(screening, floor: float, now_ratio: float) -> Change:
    """A rise stated the way the diff engine states it: with the screening's floor."""
    return Change(
        change_type=ChangeType.AVAILABILITY_RISE,
        source="cinemacity_cz",
        screening_key=screening.key,
        event_key=screening.event_key,
        old={"availability_ratio": floor},
        new={"availability_ratio": now_ratio, "availability_floor": floor},
        screening=screening,
    )


def _seat_watch(config, **trigger):
    config.watches[0].trigger = config.watches[0].trigger.model_copy(update=trigger)
    return config


def test_hall_capacity_is_recovered_from_the_seeded_history(session, hall):
    from tg.core.watches import hall_capacity

    assert hall_capacity(session, "cinemacity_cz", "cinemacity_cz:1052", "IMAX VOLVO") == 385


def test_two_seat_cart_jitter_no_longer_alerts(session, config, hall):
    """6 -> 8 free. Real, and the exact shape of all 63 production alerts: a cart timed
    out, two seats came back, and they were gone again inside half an hour."""
    imax = next(s for s in hall if s.auditorium == "IMAX VOLVO")
    _seat_watch(config, min_seats_above_floor=4, max_availability=0.10)
    assert evaluate(session, config, [_floored(imax, 0.0156, 0.0208)]) == []


def test_a_real_block_coming_back_still_alerts(session, config, hall):
    """6 -> 12 free. Six seats above the floor is a cancelled group booking, not jitter."""
    imax = next(s for s in hall if s.auditorium == "IMAX VOLVO")
    _seat_watch(config, min_seats_above_floor=4, max_availability=0.10)
    alerts = evaluate(session, config, [_floored(imax, 0.0156, 0.0312)])
    assert len(alerts) == 1
    assert "12 of 385 seats free" in alerts[0].body
    assert "rests at 6 free, so 6 genuinely came back" in alerts[0].body


def test_a_rise_off_the_floor_is_measured_from_the_floor_not_the_last_reading(
    session, config, hall
):
    """8 -> 12 is only four seats of movement, but ten above the floor of 6... no: the
    floor is what the watch is asked about, and 12 - 6 = 6 clears a threshold of 4 even
    though the step from the previous reading was smaller."""
    imax = next(s for s in hall if s.auditorium == "IMAX VOLVO")
    _seat_watch(config, min_seats_above_floor=6, max_availability=0.10)
    change = Change(
        change_type=ChangeType.AVAILABILITY_RISE,
        source="cinemacity_cz",
        screening_key=imax.key,
        event_key=imax.event_key,
        old={"availability_ratio": 0.0208},
        new={"availability_ratio": 0.0312, "availability_floor": 0.0156},
        screening=imax,
    )
    assert len(evaluate(session, config, [change])) == 1


def test_a_house_with_plenty_free_is_not_news(session, config, hall):
    """max_availability keeps the watch on the sold-out case it exists for."""
    imax = next(s for s in hall if s.auditorium == "IMAX VOLVO")
    _seat_watch(config, min_seats_above_floor=4, max_availability=0.10)
    assert evaluate(session, config, [_floored(imax, 0.60, 0.6597)]) == []


def test_ratio_threshold_still_applies_when_capacity_is_unknown(session, config, seeded):
    """One seeded date is too thin to estimate a capacity, so the seats threshold cannot
    be evaluated and the fractional one must still guard the channel."""
    imax = next(s for s in seeded if s.auditorium == "IMAX VOLVO")
    _seat_watch(config, min_seats_above_floor=4, availability_rise_min=0.005)
    assert evaluate(session, config, [_rise(imax, 0.001)]) == []
    assert len(evaluate(session, config, [_rise(imax, 0.02)])) == 1


# --------------------------------------------------- the IMAX catch-all watch
#
# Never fired in production: every IMAX screening was seeded by the silent cold start,
# so the rule meant to catch the next program drop had never delivered a message.


@pytest.fixture
def catch_all(config):
    config.watches = [
        WatchConfig.model_validate(
            {
                "name": "Anything new in the IMAX hall",
                "source": "cinemacity_cz",
                "match": {"auditorium_regex": "(?i)imax", "cinemas": ["1052"]},
                "trigger": {"on": ["NEW_SCREENING"]},
                "notify": ["discord"],
                "digest_threshold": 100,
                "cooldown": "1h",
            }
        )
    ]
    return config


@pytest.mark.parametrize(
    "fixture_name,expected_title",
    [
        # The 70 mm run itself.
        ("film-events-1052-2026-08-04.json", "Odyssea"),
        # A one-off concert film in the same hall, carrying `imax` and *not* `70-mm`.
        # A watch keyed on formats drops this silently; keying on the auditorium is the
        # whole reason the catch-all is written the way it is.
        ("film-events-1052-2026-08-27.json", "Ghost: 2 Big To Rig"),
    ],
)
def test_newly_published_imax_screenings_alert(
    session, catch_all, mapped, fixture_name, expected_title
):
    from tg.core.diff import sync_venues
    from tg.core.normalize import NormVenue

    flora = NormVenue(source="cinemacity_cz", external_id="1052", name="Praha Flora")
    sync_venues(session, [flora])
    events, screenings = mapped(fixture_name)
    sync_events(session, list(events.values()))
    changes = sync_screenings(session, "cinemacity_cz", screenings, covered_dates=set())

    alerts = evaluate(session, catch_all, changes)
    imax_alerts = [a for a in alerts if "IMAX VOLVO" in a.body]
    assert imax_alerts, "the catch-all must fire for a newly published IMAX screening"
    assert any(expected_title in a.title for a in imax_alerts)
    assert all("New screening on sale" in a.title for a in imax_alerts)


def test_catch_all_ignores_other_halls(session, catch_all, mapped):
    from tg.core.diff import sync_venues
    from tg.core.normalize import NormVenue

    flora = NormVenue(source="cinemacity_cz", external_id="1052", name="Praha Flora")
    sync_venues(session, [flora])
    events, screenings = mapped("film-events-1052-2026-08-04.json")
    sync_events(session, list(events.values()))
    changes = sync_screenings(session, "cinemacity_cz", screenings, covered_dates=set())

    alerts = evaluate(session, catch_all, changes)
    assert alerts and not any("Sál" in a.body for a in alerts)


# ------------------------------------------------------- NEW_DATE and dedupe


@pytest.fixture
def date_watch(config):
    config.watches = [
        WatchConfig.model_validate(
            {
                "name": "New dates published at Flora",
                "source": "cinemacity_cz",
                "trigger": {"on": ["NEW_DATE"]},
                "notify": ["discord"],
                "cooldown": "6h",
            }
        )
    ]
    return config


def _new_date(day: str) -> Change:
    return Change(change_type=ChangeType.NEW_DATE, source="cinemacity_cz", new={"date": day})


def test_a_newly_published_date_alerts(session, date_watch):
    """The cheapest signal there is, and until now it was wired to nothing."""
    alerts = evaluate(session, date_watch, [_new_date("2026-08-25")])
    assert len(alerts) == 1
    assert "New date published" in alerts[0].title
    assert "2026-08-25" in alerts[0].body


def test_new_date_respects_the_date_window(session, date_watch):
    """A bare date cannot answer 'which hall', but it can answer 'is it in my window'."""
    date_watch.watches[0].match = WatchMatch.model_validate(
        {"date_from": "2026-09-01", "date_to": "2026-09-30"}
    )
    assert evaluate(session, date_watch, [_new_date("2026-08-25")]) == []
    assert len(evaluate(session, date_watch, [_new_date("2026-09-05")])) == 1


def test_new_date_respects_weekdays(session, date_watch):
    date_watch.watches[0].match = WatchMatch.model_validate({"weekdays": ["tue"]})
    assert evaluate(session, date_watch, [_new_date("2026-08-26")]) == []  # Wednesday
    assert len(evaluate(session, date_watch, [_new_date("2026-08-25")])) == 1  # Tuesday


def test_unparseable_date_is_dropped_rather_than_alerted(session, date_watch):
    assert evaluate(session, date_watch, [_new_date("not-a-date")]) == []


def test_two_changes_on_one_screening_send_one_message(session, config, seeded):
    """The cooldown query reads the database, and candidates are not written until the
    whole cycle has been evaluated — so without an in-cycle guard a screening that both
    gained seats and came back on sale produced two notifications for one event."""
    imax = next(s for s in seeded if s.auditorium == "IMAX VOLVO")
    config.watches[0].trigger = config.watches[0].trigger.model_copy(
        update={"events": ["AVAILABILITY_RISE", "BACK_ON_SALE"]}
    )
    back_on_sale = Change(
        change_type=ChangeType.BACK_ON_SALE,
        source="cinemacity_cz",
        screening_key=imax.key,
        event_key=imax.event_key,
        old={"sold_out": True},
        new={"sold_out": False},
        screening=imax,
    )
    alerts = evaluate(session, config, [_rise(imax, 0.02), back_on_sale])
    assert len(alerts) == 1
