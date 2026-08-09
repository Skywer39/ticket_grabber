"""Recovering a hall's seat count from the availability ratio.

Cinema City publishes ``availabilityRatio`` and never a seat count, so an alert can
only say "2.1% of seats free" — a number nobody can act on. In a near-sold-out IMAX
hall the difference between 1.56% and 2.08% is the difference between *nothing* and
*two seats*, and the percentage hides that completely.

The ratio is ``free / capacity`` rounded to four decimals, so every value the site has
ever published for one hall is a multiple of ``1 / capacity``. Recovering that
denominator turns the percentage back into the number a human wants: "8 of 385 seats
free (+2)". Measured against live data, Praha Flora's IMAX VOLVO resolves to 385.

Capacity is a property of the *auditorium*, not of a screening, so callers should pool
the ratios of every screening in a hall. A large pool is also what makes the estimate
safe: the denominator is found by taking the smallest one that fits, and a small sample
whose numerators happen to share a factor would fit a proper divisor of the true
capacity. A few dozen screenings make that vanishingly unlikely, and the estimate is
recomputed from scratch as the pool grows, so it self-corrects.
"""

from __future__ import annotations

from collections.abc import Iterable

#: Plausible single-auditorium sizes. Below the floor there is too little signal to
#: separate candidate denominators; above the ceiling is larger than any one screen at
#: the venues this targets.
MIN_CAPACITY = 40
MAX_CAPACITY = 800

#: Distinct informative ratios required before an estimate is offered at all. Two or
#: three points fit far too many denominators.
MIN_SAMPLES = 4

#: The published ratio is rounded to four decimals, so ``ratio * capacity`` can sit up
#: to ``5e-5 * capacity`` away from a whole number even when the capacity is exact.
_ROUNDING_HALF_STEP = 5e-5


def estimate_capacity(
    ratios: Iterable[float | None],
    *,
    min_samples: int = MIN_SAMPLES,
    lo: int = MIN_CAPACITY,
    hi: int = MAX_CAPACITY,
) -> int | None:
    """Smallest seat count consistent with every ratio observed for one hall.

    Returns ``None`` when the sample is too thin to be trusted — callers then fall back
    to reporting the raw percentage rather than inventing a seat count.
    """
    # 0.0 and 1.0 are consistent with every denominator, so they carry no information.
    sample = {round(r, 4) for r in ratios if r is not None and 0.0 < r < 1.0}
    if len(sample) < min_samples:
        return None

    for n in range(lo, hi + 1):
        tol = _ROUNDING_HALF_STEP * n + 1e-9
        if all(abs(r * n - round(r * n)) <= tol for r in sample):
            return n
    return None


def seats_from_ratio(ratio: float | None, capacity: int | None) -> int | None:
    """The ratio expressed as whole seats, or ``None`` when capacity is unknown."""
    if ratio is None or capacity is None:
        return None
    return round(ratio * capacity)
