"""Cinema City mapping, checked against payloads captured from the live API.

These are the golden-data tests: the numbers below are what the site actually
served for Praha Flora on 2026-08-04.
"""

from __future__ import annotations

from datetime import date

from tg.core.normalize import FormatTag
from tg.core.timeutil import to_local


def test_maps_venues_with_city_and_coordinates(adapter, fixture_body):
    venues = [adapter._to_venue(c) for c in fixture_body("cinemas.json")["cinemas"]]
    assert len(venues) == 13

    flora = next(v for v in venues if v.external_id == "1052")
    assert flora.name == "Praha Flora, OC FLORA"
    assert flora.city == "Praha 3 - Žižkov"
    assert flora.key == "cinemacity_cz:1052"
    assert flora.latitude == 50.079


def test_maps_the_odyssea_imax_screenings(adapter, fixture_body):
    """The exact case that motivated the project: 70mm IMAX at ~98.5% sold, while
    the same film in an ordinary hall sits nearly empty."""
    body = fixture_body("film-events-1052-2026-08-04.json")
    titles = {f["id"]: f["name"] for f in body["films"]}
    screenings = [adapter._to_screening(e, {}) for e in body["events"]]

    imax = [
        s
        for s in screenings
        if s.auditorium == "IMAX VOLVO" and titles[s.event_external_id] == "Odyssea"
    ]
    assert len(imax) == 4
    for s in imax:
        assert FormatTag.FILM_70MM in s.formats
        assert s.availability_ratio is not None and s.availability_ratio < 0.05
        assert s.sold_out is False  # the site never flags these as sold out

    # Exact values as captured in the fixture.
    by_time = {to_local(s.starts_at, "Europe/Prague").strftime("%H:%M"): s for s in imax}
    assert by_time["09:00"].availability_ratio == 0.0156
    assert by_time["12:50"].availability_ratio == 0.0156
    assert by_time["16:40"].availability_ratio == 0.0208
    assert by_time["20:30"].availability_ratio == 0.0104

    ordinary = next(
        s
        for s in screenings
        if s.auditorium == "Sál 04"
        and to_local(s.starts_at, "Europe/Prague").strftime("%H:%M") == "12:30"
    )
    assert ordinary.availability_ratio == 0.9688
    assert FormatTag.FILM_70MM not in ordinary.formats


def test_screening_times_are_stored_as_utc(adapter, fixture_body):
    """The API publishes naive venue-local wall time; CEST is UTC+2 in August."""
    body = fixture_body("film-events-1052-2026-08-04.json")
    s = adapter._to_screening(body["events"][0], {})
    assert s.starts_at.tzinfo is not None
    local = to_local(s.starts_at, "Europe/Prague")
    assert local.strftime("%Y-%m-%dT%H:%M") == body["events"][0]["eventDateTime"][:16]


def test_booking_link_is_the_router_not_the_api_endpoint(adapter, fixture_body):
    """`bookingLink` points at tickets.cinemacity.cz/api/order/{id}, which the site's
    own payload labels `obsoleteBookingUrl` and which answers 404 "Error Occurred" —
    a blank page. The router link is the entry point a human can open.

    The earlier version of this test asserted the URL merely started with
    tickets.cinemacity.cz, so it passed while every alert shipped a dead link."""
    body = fixture_body("film-events-1052-2026-08-04.json")
    event = body["events"][0]
    s = adapter._to_screening(event, {})

    assert s.booking_url == event["bookingRouterLaunchLink"]
    assert "/cz/booking-router/launch/" in s.booking_url
    assert "/api/order/" not in s.booking_url


def test_router_link_wins_even_though_booking_link_is_present(adapter, fixture_body):
    body = fixture_body("film-events-1052-2026-08-04.json")
    event = body["events"][0]
    assert event.get("bookingLink")  # both fields exist; the chain must prefer one
    assert adapter._to_screening(event, {}).booking_url != event["bookingLink"]


def test_every_fixture_event_maps_to_an_openable_link(adapter, fixture_body):
    """No sparse-field surprises: this must hold for the whole payload, not one row."""
    for name in ("film-events-1052-2026-08-04.json", "film-events-1052-2026-08-06.json"):
        for event in fixture_body(name)["events"]:
            url = adapter._to_screening(event, {}).booking_url
            assert url and "/api/order/" not in url


# ------------------------------------------------------------------ deep links


def test_deep_link_opens_on_the_screenings_own_date(mapped):
    """An undated link opens on today, which is the wrong day for every alert about a
    future screening — and once today's showtimes have passed, the film page shows
    "Bohužel tento film v kině ... nehrajeme" instead of anything useful."""
    _, screenings = mapped("film-events-1052-2026-08-04.json")
    s = next(x for x in screenings if x.auditorium == "IMAX VOLVO")

    assert s.info_url == (
        "https://www.cinemacity.cz/films/odyssea/7268s2r"
        "#/buy-tickets-by-film"
        "?in-cinema=prague&at=2026-08-04&for-movie=7268s2r&view-mode=list"
    )


def test_deep_link_uses_the_group_slug_not_the_cinema_id(mapped):
    """Passing an id makes the app rewrite the fragment to the group anyway."""
    _, screenings = mapped("film-events-1052-2026-08-04.json")
    assert "in-cinema=prague" in screenings[0].info_url
    assert "in-cinema=1052" not in screenings[0].info_url


def test_late_screening_links_to_its_local_date_not_the_utc_one(adapter, fixture_body):
    """20:30 Prague is 18:30 UTC the same day — but the naive trap is real for any
    venue whose late shows cross midnight in UTC, so pin the wall date."""
    adapter._to_venue(
        {"id": "1052", "groupId": "prague", "link": "https://www.cinemacity.cz/cinemas/flora"}
    )
    body = fixture_body("film-events-1052-2026-08-04.json")
    film = adapter._to_event(next(f for f in body["films"] if f["id"] == "7268s2r"))
    late = next(e for e in body["events"] if e["eventDateTime"].endswith("T20:30:00"))

    s = adapter._to_screening(late, {film.external_id: film})
    assert "at=2026-08-04" in s.info_url
    assert to_local(s.starts_at, "Europe/Prague").date().isoformat() == "2026-08-04"


def test_venue_deep_link_keeps_the_cinema_id(mapped):
    """The by-cinema route is the one place ``in-cinema`` really takes the venue id."""
    _, screenings = mapped("film-events-1052-2026-08-04.json")
    assert screenings[0].venue_info_url == (
        "https://www.cinemacity.cz/cinemas/flora"
        "#/buy-tickets-by-cinema?in-cinema=1052&at=2026-08-04&view-mode=list"
    )


def test_deep_link_falls_back_to_the_plain_film_page(adapter, fixture_body):
    """An unknown cinema means no group slug. The film page still opens; it just
    opens on today."""
    body = fixture_body("film-events-1052-2026-08-04.json")
    film = adapter._to_event(next(f for f in body["films"] if f["id"] == "7268s2r"))
    event = next(e for e in body["events"] if e["filmId"] == "7268s2r")

    s = adapter._to_screening(event, {film.external_id: film})
    assert s.info_url == "https://www.cinemacity.cz/films/odyssea/7268s2r"
    assert s.venue_info_url is None


def test_deep_link_is_absent_when_the_film_has_no_page(adapter, fixture_body):
    body = fixture_body("film-events-1052-2026-08-04.json")
    assert adapter._to_screening(body["events"][0], {}).info_url is None


def test_links_stay_out_of_the_content_hash(mapped, adapter, fixture_body):
    """Otherwise the day a link shape changes reads as every screening being new."""
    _, screenings = mapped("film-events-1052-2026-08-04.json")
    body = fixture_body("film-events-1052-2026-08-04.json")
    bare = [adapter._to_screening(e, {}) for e in body["events"]]

    assert [s.info_url for s in screenings] != [s.info_url for s in bare]
    assert [s.content_hash() for s in screenings] == [s.content_hash() for s in bare]


def test_maps_films_including_czech_title(adapter, fixture_body):
    body = fixture_body("films.json")
    events = [adapter._to_event(f) for f in body["films"]]
    odyssea = next(e for e in events if e.title == "Odyssea")
    assert odyssea.external_id == "7268s2r"
    assert odyssea.duration_minutes == 180
    assert odyssea.release_date == date(2026, 7, 16)
    assert "drama" in odyssea.genres


def test_url_construction_matches_the_live_api(adapter):
    url = adapter.url("dates/in-cinema/1052/until/2026-09-30")
    assert url == (
        "https://www.cinemacity.cz/cz/data-api-service/v1/quickbook/10101/"
        "dates/in-cinema/1052/until/2026-09-30?attr=&lang=cs_CZ"
    )
