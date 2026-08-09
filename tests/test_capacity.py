"""Recovering a hall's seat count from the ratios the site publishes.

The ratio sets below are the distinct ``availabilityRatio`` values Cinema City actually
published for Praha Flora over five days of continuous polling — read back out of the
poller's own database, not invented. That matters: the estimator's whole job is to hold
up against the sparse, near-sold-out numbers a real IMAX run produces.
"""

from __future__ import annotations

from tg.core.capacity import estimate_capacity, seats_from_ratio

#: Every distinct ratio observed for IMAX VOLVO across 84 screenings. The two high
#: values are the one-off concert films that also run in the hall; they are what pins
#: the denominator exactly, since the near-sold-out values alone fit 384 just as well.
IMAX_VOLVO = [
    0.0052, 0.0078, 0.0104, 0.013, 0.0156, 0.0182, 0.0208, 0.0234, 0.026,
    0.0286, 0.0338, 0.0364, 0.0442, 0.0468, 0.0494, 0.6675, 0.8961,
]  # fmt: skip

#: An ordinary hall, which sells slowly and therefore reports from the other end.
SAL_03 = [
    0.7923, 0.8308, 0.8385, 0.8462, 0.8692, 0.9154, 0.9308, 0.9385,
    0.9462, 0.9538, 0.9615, 0.9692, 0.9769, 0.9846, 0.9923, 1.0,
]  # fmt: skip


def test_recovers_the_imax_hall_size():
    assert estimate_capacity(IMAX_VOLVO) == 385


def test_recovers_an_ordinary_hall_from_the_busy_end_of_the_scale():
    assert estimate_capacity(SAL_03) == 130


def test_a_thin_sample_is_refused_rather_than_guessed():
    """Two points fit dozens of denominators. Returning ``None`` makes the caller print
    a percentage, which is vague; returning a wrong seat count would be a lie."""
    assert estimate_capacity([0.0156, 0.0208]) is None


def test_trivial_ratios_carry_no_information():
    """An empty or full house is consistent with every possible capacity."""
    assert estimate_capacity([0.0, 1.0, 0.0, 1.0]) is None


def test_reads_the_ratio_out_as_whole_seats():
    assert seats_from_ratio(0.0156, 385) == 6
    assert seats_from_ratio(0.0208, 385) == 8
    # The move that produced five days of "Seats freed up" alerts: two seats.
    assert seats_from_ratio(0.0208 - 0.0156, 385) == 2


def test_unknown_inputs_stay_unknown():
    assert seats_from_ratio(None, 385) is None
    assert seats_from_ratio(0.0156, None) is None
