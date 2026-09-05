"""Mapping tests for the O2 arena adapter, against a captured slice of the live feed."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from tg.adapters.o2arena import OPERATOR_PREFERENCE, _booking_url
from tg.core.adapter import AdapterError, Capability
from tg.core.normalize import EventKind
from tg.core.timeutil import to_local

PUSSYCAT = "61870"
SPARTA = "67352"
YZO = "58163"
FMX = "56775"
#: The two guided-tour records that share slot ids 94999138 (13:00) and 82221889 (16:00).
TOUR_OCT_3 = "67079"
TOUR_OCT_31 = "67097"


def _event(feed: list[dict], event_id: str) -> dict:
    return next(e["Event"] for e in feed if str(e["Event"]["Event_id"]) == event_id)


# --------------------------------------------------------------- the timestamp trap


def test_epoch_is_prague_wall_time_not_utc(o2_adapter, o2_feed):
    """The feed's epochs are local wall time encoded as though they were UTC.

    Verified against the site's own rendering: o2arena.cz shows this show as
    "23.9.2026 19:00", and the feed carries 1790190000, which *is* 19:00 UTC. Taking
    the epoch at face value would put the alert at 21:00 Prague — two hours late, on
    every single screening, with nothing to make it obvious.
    """
    raw = _event(o2_feed, PUSSYCAT)
    assert raw["Performances"][0]["Date"] == 1790190000

    starts_at = o2_adapter._wall_time(1790190000)
    local = to_local(starts_at, "Europe/Prague")

    assert (local.year, local.month, local.day) == (2026, 9, 23)
    assert (local.hour, local.minute) == (19, 0)
    # And the stored value is the real UTC instant, two hours behind Prague in September.
    assert starts_at == datetime(2026, 9, 23, 17, 0, tzinfo=UTC)


def test_epoch_decoding_survives_the_dst_boundary(o2_adapter, o2_feed):
    """Prague is UTC+2 in September and UTC+1 in December.

    A fixed two-hour fudge would pass the September case above and quietly break every
    winter show, so pin a December one too: the evening FMX performance reads 19:00
    local, which is 18:00 UTC rather than 17:00.
    """
    evening = _event(o2_feed, FMX)["Performances"][1]
    starts_at = o2_adapter._wall_time(evening["Date"])
    local = to_local(starts_at, "Europe/Prague")

    assert (local.month, local.day, local.hour) == (12, 19, 19)
    assert starts_at == datetime(2026, 12, 19, 18, 0, tzinfo=UTC)


@pytest.mark.parametrize("value", [None, "", 0, "not-a-number", []])
def test_unusable_timestamps_are_skipped_not_guessed(o2_adapter, value):
    assert o2_adapter._wall_time(value) is None


# --------------------------------------------------------------------- event mapping


def test_maps_events_and_performances(o2_adapter, o2_feed):
    events, screenings = _map(o2_adapter, o2_feed)

    assert {e.external_id for e in events} == {
        PUSSYCAT, SPARTA, YZO, FMX, TOUR_OCT_3, TOUR_OCT_31
    }

    pussycat = next(e for e in events if e.external_id == PUSSYCAT)
    assert pussycat.title == "The Pussycat Dolls: PCD FOREVER TOUR"
    assert pussycat.kind is EventKind.CONCERT
    assert pussycat.url == (
        "https://www.o2arena.cz/events/the-pussycat-dolls-pcd-forever-tour/"
    )
    assert pussycat.poster_url

    # Every performance becomes its own screening, sharing the event key.
    yzo = [s for s in screenings if s.event_external_id == YZO]
    assert len(yzo) == 2
    assert len({s.external_id for s in yzo}) == 2
    assert {s.event_key for s in yzo} == {f"o2arena_cz:{YZO}"}


def test_categories_become_kinds(o2_adapter, o2_feed):
    events, _ = _map(o2_adapter, o2_feed)
    kinds = {e.external_id: e.kind for e in events}

    assert kinds[PUSSYCAT] is EventKind.CONCERT  # Hudba
    assert kinds[SPARTA] is EventKind.SPORT  # Sport
    # Filed under Sport *and* Rodinné — precedence has to give a stable answer.
    assert kinds[FMX] is EventKind.SPORT


def test_titles_are_unescaped(o2_adapter):
    """The feed serves WordPress-escaped titles. They end up in notification text and
    are what title_regex matches, so the entities have to go."""
    event = o2_adapter._to_event(
        {"Event_id": 1, "Title": "Pitbull &#8211; I&#8217;m Back!", "Categories": {}}
    )
    assert event.title == "Pitbull – I’m Back!"


def test_unknown_category_id_falls_back_to_its_title(o2_adapter):
    raw = {
        "Event_id": 1,
        "Title": "Something new",
        "Categories": {"99": {"title": "Hudba"}},
    }
    assert o2_adapter._to_event(raw).kind is EventKind.CONCERT


def test_unrecognisable_category_is_other_not_a_crash(o2_adapter):
    raw = {"Event_id": 1, "Title": "?", "Categories": {"99": {"title": "Přednáška"}}}
    assert o2_adapter._to_event(raw).kind is EventKind.OTHER


def test_venue_is_the_arena_itself(o2_adapter, o2_feed):
    _, screenings = _map(o2_adapter, o2_feed)
    assert {s.venue_external_id for s in screenings} == {"2"}
    assert {s.venue_name for s in screenings} == {"O2 arena"}
    # One hall, so nothing to filter on — and claiming otherwise would let an
    # auditorium_regex watch look meaningful when it cannot be.
    assert {s.auditorium for s in screenings} == {None}


def test_no_availability_is_reported(o2_adapter, o2_feed):
    """The feed publishes none, and isSoldout was null on every performance observed."""
    _, screenings = _map(o2_adapter, o2_feed)
    assert all(s.availability_ratio is None for s in screenings)
    assert all(s.sold_out is False for s in screenings)


def test_capabilities_claim_only_what_the_feed_supports(o2_adapter):
    caps = o2_adapter.capabilities
    assert Capability.WHOLE_HORIZON in caps
    assert Capability.CALENDAR not in caps
    assert Capability.AVAILABILITY_RATIO not in caps
    assert Capability.SEATMAP not in caps


# ------------------------------------------------------------------ screening identity


def test_performance_id_alone_is_not_unique(o2_feed):
    """The premise of the test below, asserted against the capture so it cannot rot.

    Two separate guided-tour events reuse the same Performance_id values — the field is
    a time-slot id, not a performance identity.
    """
    ids = [
        str(p["Performance_id"])
        for e in o2_feed
        for p in e["Event"]["Performances"]
    ]
    assert len(ids) != len(set(ids)), "the fixture must still contain the collision"


def test_screening_identity_survives_the_collision(o2_adapter, o2_feed):
    """Keying on Performance_id alone put two rows on one key and the first live poll
    died on a UNIQUE constraint. The identity is the event/performance pair."""
    _, screenings = _map(o2_adapter, o2_feed)

    keys = [s.key for s in screenings]
    assert len(keys) == len(set(keys))

    tours = {s.event_external_id: s for s in screenings if s.external_id.endswith("94999138")}
    assert set(tours) == {TOUR_OCT_3, TOUR_OCT_31}
    assert tours[TOUR_OCT_3].external_id == f"{TOUR_OCT_3}-94999138"
    assert tours[TOUR_OCT_3].key != tours[TOUR_OCT_31].key


async def test_a_duplicate_key_costs_one_screening_not_the_poll(o2_adapter, caplog):
    """If the feed ever does repeat a pair, drop that performance and say so — an
    IntegrityError would take the whole source down for the cycle."""
    import logging

    perf = {
        "Performance_id": "same",
        "Date": 1790190000,
        "Tickets": [{"ticket_operator": "Ticketmaster", "ticket_url": "https://x/y"}],
    }
    o2_adapter.client = _StubClient(
        [{"Event": {"Event_id": 1, "Title": "x", "Categories": {}, "Performances": [perf, perf]}}]
    )

    with caplog.at_level(logging.WARNING):
        _, screenings = await o2_adapter.screenings(
            datetime(2020, 1, 1).date(), datetime(2030, 1, 1).date()
        )

    assert len(screenings) == 1
    assert "duplicate screening id" in caplog.text


# ------------------------------------------------------------------- booking links


def test_booking_url_follows_a_fixed_operator_preference():
    perf = {
        "Tickets": [
            {"ticket_operator": "Ticketportal", "ticket_url": "https://portal/x"},
            {"ticket_operator": "Ticketmaster", "ticket_url": "https://master/x"},
        ]
    }
    assert _booking_url(perf) == "https://master/x"

    # Reordering upstream must not change the answer — the value is content-hashed.
    perf["Tickets"].reverse()
    assert _booking_url(perf) == "https://master/x"
    assert OPERATOR_PREFERENCE[0] == "Ticketmaster"


def test_booking_url_falls_back_to_whatever_is_there():
    perf = {"Tickets": [{"ticket_operator": "Someone Else", "ticket_url": "https://x/y"}]}
    assert _booking_url(perf) == "https://x/y"
    assert _booking_url({"Tickets": []}) is None
    assert _booking_url({}) is None


def test_every_captured_performance_has_a_booking_link(o2_adapter, o2_feed):
    _, screenings = _map(o2_adapter, o2_feed)
    assert all(s.booking_url for s in screenings)


def test_info_url_is_the_event_page(o2_adapter, o2_feed):
    """Already dated and GET-able, unlike the cinema's quickbook fragment."""
    _, screenings = _map(o2_adapter, o2_feed)
    pussycat = next(s for s in screenings if s.event_external_id == PUSSYCAT)
    assert pussycat.info_url == (
        "https://www.o2arena.cz/events/the-pussycat-dolls-pcd-forever-tour/"
    )
    assert pussycat.venue_info_url is None


# ---------------------------------------------------------------- on-sale detection


def _perf(offset_days: float) -> dict:
    """A performance whose sale opens ``offset_days`` from now, as the feed encodes it.

    Built relative to the clock rather than taken from the capture: the captured
    Selling_since values are real dates, so an assertion pinned to them would pass this
    week and fail next.
    """
    when = datetime.now(UTC) + timedelta(days=offset_days)
    # The feed writes local wall time into a UTC epoch; mirror that, so the adapter's
    # own decoding is exercised rather than bypassed.
    local = to_local(when, "Europe/Prague").replace(tzinfo=UTC)
    return {
        "Performance_id": "1",
        "Date": 1790190000,
        "Selling_since": int(local.timestamp()),
        "Tickets": [{"ticket_operator": "Ticketmaster", "ticket_url": "https://x/y"}],
    }


def test_sales_blocked_until_selling_since_passes(o2_adapter):
    event = o2_adapter._to_event({"Event_id": 1, "Title": "x", "Categories": {}})

    future = o2_adapter._to_screening(_perf(+7), {}, event)
    assert future.sales_blocked is True, "announced but not yet buyable"

    past = o2_adapter._to_screening(_perf(-7), {}, event)
    assert past.sales_blocked is False, "on sale — this transition is what fires ON_SALE"


def test_missing_selling_since_means_not_blocked(o2_adapter):
    """No stated on-sale date is not evidence that sales are closed."""
    event = o2_adapter._to_event({"Event_id": 1, "Title": "x", "Categories": {}})
    perf = {"Performance_id": "1", "Date": 1790190000}
    assert o2_adapter._to_screening(perf, {}, event).sales_blocked is False


# -------------------------------------------------------------------- feed shape


async def test_rejects_a_payload_that_is_not_a_list(o2_adapter):
    o2_adapter.client = _StubClient({"body": {"events": []}})
    with pytest.raises(AdapterError, match="expected a JSON list"):
        await o2_adapter._feed()


async def test_rejects_a_list_that_carries_no_events(o2_adapter):
    """A WordPress site can serve something 200-shaped that is not the feed."""
    o2_adapter.client = _StubClient([{"Article": {"id": 1}}])
    with pytest.raises(AdapterError, match="shape has changed"):
        await o2_adapter._feed()


async def test_an_empty_feed_is_not_an_error(o2_adapter):
    o2_adapter.client = _StubClient([])
    assert await o2_adapter._feed() == []


async def test_screenings_filters_to_the_requested_window(o2_adapter, o2_feed):
    """The scheduler treats the whole window as covered, so anything outside it must
    not come back — an out-of-window screening would look withdrawn on the next poll."""
    o2_adapter.client = _StubClient(o2_feed)

    _, all_screenings = await o2_adapter.screenings(
        datetime(2020, 1, 1).date(), datetime(2030, 1, 1).date()
    )
    assert len(all_screenings) == 10

    events, narrow = await o2_adapter.screenings(
        datetime(2026, 9, 1).date(), datetime(2026, 9, 30).date()
    )
    # September holds the Pussycat Dolls show and both YZO nights; the October hockey
    # and the December FMX performances are outside it.
    assert sorted(s.event_external_id for s in narrow) == [YZO, YZO, PUSSYCAT]
    # And the events returned are only those with a screening in the window.
    assert sorted(e.external_id for e in events) == [YZO, PUSSYCAT]


# ------------------------------------------------------------------------- helpers


def _map(adapter, feed):
    """Map a captured feed the way ``screenings()`` does, without a request."""
    events, screenings = {}, []
    for entry in feed:
        raw = entry["Event"]
        event = adapter._to_event(raw)
        for perf in raw.get("Performances") or []:
            screening = adapter._to_screening(perf, raw, event)
            if screening is not None:
                screenings.append(screening)
                events.setdefault(event.external_id, event)
    return list(events.values()), screenings


class _StubClient:
    """Stands in for PoliteClient; returns one canned payload."""

    def __init__(self, payload):
        self.payload = payload
        self.calls = 0

    async def get(self, url, **kwargs):
        self.calls += 1
        return _StubResult(self.payload)


class _StubResult:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload
