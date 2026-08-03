"""Normalization: attribute vocabulary, seat labels, change fingerprinting."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from tg.core.normalize import (
    FormatTag,
    NormScreening,
    Seat,
    SeatMap,
    SeatStatus,
    parse_attributes,
    parse_row_index,
    parse_seat_index,
)


def test_parses_the_real_attribute_vocabulary():
    """Attributes taken verbatim from a live Odyssea IMAX screening."""
    parsed = parse_attributes(
        ["15-plus", "2d", "70-mm", "drama", "history", "original-lang-en", "first-subbed-lang-cs"]
    )
    assert FormatTag.FILM_70MM in parsed.formats
    assert FormatTag.TWO_D in parsed.formats
    assert FormatTag.SUBTITLED in parsed.formats
    assert sorted(parsed.genres) == ["drama", "history"]
    assert parsed.ratings == ["15-plus"]
    assert parsed.languages == {"original": ["en"], "first-subbed": ["cs"]}
    assert parsed.unmapped == []


def test_unknown_attributes_are_preserved_not_dropped():
    """A site adding a tag should surface, not vanish silently."""
    parsed = parse_attributes(["imax", "brand-new-format-2027"])
    assert FormatTag.IMAX in parsed.formats
    assert parsed.unmapped == ["brand-new-format-2027"]


def test_dubbed_and_subbed_imply_a_format():
    assert FormatTag.DUBBED in parse_attributes(["dubbed-lang-cs"]).formats
    assert FormatTag.SUBTITLED in parse_attributes(["subbed"]).formats


@pytest.mark.parametrize(
    ("label", "expected"),
    [("12", 12), ("R12", 12), ("A", 1), ("H", 8), ("Z", 26), ("řada 9", 9), ("", None)],
)
def test_row_labels_normalize_to_numbers(label, expected):
    """Profiles say ``rows: [8, 14]``; venues may letter or number their rows."""
    assert parse_row_index(label) == expected


def test_seat_index_parsing():
    assert parse_seat_index("14") == 14
    assert parse_seat_index("seat 7") == 7
    assert parse_seat_index("none") is None


def _screening(**kw) -> NormScreening:
    base = dict(
        source="s",
        external_id="1",
        event_external_id="f",
        venue_external_id="1052",
        starts_at=datetime(2026, 8, 4, 14, 40, tzinfo=UTC),
        availability_ratio=0.0156,
        auditorium="IMAX VOLVO",
    )
    return NormScreening(**(base | kw))


def test_content_hash_is_stable_for_identical_data():
    assert _screening().content_hash() == _screening().content_hash()


def test_content_hash_tracks_availability():
    assert _screening().content_hash() != _screening(availability_ratio=0.02).content_hash()


def test_content_hash_ignores_sub_basis_point_jitter():
    """The ratio wobbles in far decimals as carts are held and released; alerting on
    that would be pure noise."""
    assert _screening(availability_ratio=0.015600001).content_hash() == _screening().content_hash()


def test_seatmap_reports_availability_and_fingerprint():
    seats = [
        Seat("8", "10", SeatStatus.AVAILABLE),
        Seat("8", "11", SeatStatus.TAKEN),
        Seat("8", "12", SeatStatus.AVAILABLE),
    ]
    smap = SeatMap("s:1", seats)
    assert len(smap.available) == 2
    assert smap.availability_ratio == pytest.approx(2 / 3)

    freed = SeatMap("s:1", [Seat("8", "10", SeatStatus.AVAILABLE),
                            Seat("8", "11", SeatStatus.AVAILABLE),
                            Seat("8", "12", SeatStatus.AVAILABLE)])
    assert freed.fingerprint() != smap.fingerprint()
