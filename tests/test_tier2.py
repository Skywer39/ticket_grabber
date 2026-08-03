"""Tier-2 seat reading and checkout assistance.

The behaviour that matters most here is what the code refuses to do.
"""

from __future__ import annotations

import pytest

from tg.adapters.cinemacity_seats import CinemaCitySeatReader, classify
from tg.assist.checkout import FORBIDDEN_PATTERNS, AssistResult, CheckoutAssistant
from tg.browser import _BLOCK_PATTERNS
from tg.config import AssistConfig, SeatMapConfig
from tg.core.normalize import SeatStatus

# --------------------------------------------------------------- seat status


@pytest.mark.parametrize(
    ("class_name", "expected"),
    [
        ("seat seat--available", SeatStatus.AVAILABLE),
        ("seat seat--occupied", SeatStatus.TAKEN),
        ("seat seating_NA", SeatStatus.UNAVAILABLE),
        ("seat seat--selected", SeatStatus.HELD),
        ("seat", SeatStatus.UNKNOWN),
        ("", SeatStatus.UNKNOWN),
    ],
)
def test_seat_status_classification(class_name, expected):
    assert classify(class_name) is expected


def test_unavailable_is_not_read_as_available():
    """'unavailable' contains 'available' — getting this backwards would report a
    sold-out house as wide open, which is the worst possible failure here."""
    assert classify("seat seat--unavailable") is SeatStatus.UNAVAILABLE
    assert classify("seat is-unavailable available-slot") is SeatStatus.UNAVAILABLE


# ------------------------------------------------------------ block detection


@pytest.mark.parametrize(
    "page_text",
    [
        "Attention Required! | Cloudflare",
        "Sorry, you have been blocked",
        "Checking your browser before accessing",
        "Just a moment...",
        "Please enable cookies and reload the page",
    ],
)
def test_block_pages_are_recognised(page_text):
    """Text captured from the real block page this project hit in testing."""
    assert _BLOCK_PATTERNS.search(page_text)


def test_normal_page_is_not_flagged_as_blocked():
    assert not _BLOCK_PATTERNS.search("Odyssea — IMAX VOLVO — vyberte si sedadlo")


# ------------------------------------------------------------ circuit breaker


def test_reader_trips_a_breaker_and_stops_trying():
    """After a block the reader must stop, not retry — retrying a refusal is the
    behaviour this project explicitly does not implement."""
    reader = CinemaCitySeatReader(SeatMapConfig(backoff_after_block_seconds=3600))
    assert not reader.is_blocked()

    reader._trip_breaker()
    assert reader.is_blocked()


def test_breaker_expires():
    reader = CinemaCitySeatReader(SeatMapConfig(backoff_after_block_seconds=0))
    reader._trip_breaker()
    assert not reader.is_blocked()


def test_selectors_are_overridable_from_config():
    """Defaults were derived from the stylesheet, not a live page, so overriding
    them must not require a code change."""
    reader = CinemaCitySeatReader(SeatMapConfig(selectors={"seat": ".my-seat"}))
    assert reader.selectors["seat"] == ".my-seat"
    assert reader.selectors["root"]  # untouched keys keep their defaults


async def test_reader_skips_work_while_blocked():
    from datetime import UTC, datetime

    from tg.core.normalize import NormScreening

    reader = CinemaCitySeatReader(SeatMapConfig(backoff_after_block_seconds=3600))
    reader._trip_breaker()
    screening = NormScreening(
        source="s", external_id="1", event_external_id="f", venue_external_id="v",
        starts_at=datetime(2026, 8, 4, tzinfo=UTC), booking_url="https://example.test/",
    )
    assert await reader.read(screening) is None


# ------------------------------------------------------------------- assist


async def test_assist_is_off_unless_enabled():
    result = await CheckoutAssistant(AssistConfig()).assist("https://example.test/", "s:1")
    assert not result.opened
    assert "disabled" in result.message


async def test_assist_needs_a_booking_url():
    result = await CheckoutAssistant(AssistConfig(enabled=True)).assist("", "s:1")
    assert "no booking URL" in result.message


def test_payment_related_controls_are_on_the_never_click_list():
    """The assistant exists to get a human to the seat picker, not to transact."""
    for word in ("pay", "zaplatit", "checkout", "confirm", "card"):
        assert word in FORBIDDEN_PATTERNS


def test_forbidden_patterns_cover_both_languages():
    """The site is Czech; an English-only list would miss the real buttons."""
    assert {"platba", "zaplatit", "objednat", "potvrdit"} <= set(FORBIDDEN_PATTERNS)


def test_result_summary_is_explicit_that_you_finish_the_purchase():
    result = AssistResult("s:1", "drive", seats_selected=["8-12", "8-13"])
    assert "complete the purchase yourself" in result.summary()
