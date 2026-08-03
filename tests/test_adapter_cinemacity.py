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


def test_maps_booking_deep_link(adapter, fixture_body):
    body = fixture_body("film-events-1052-2026-08-04.json")
    s = adapter._to_screening(body["events"][0], {})
    assert s.booking_url and s.booking_url.startswith("https://tickets.cinemacity.cz/")


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
