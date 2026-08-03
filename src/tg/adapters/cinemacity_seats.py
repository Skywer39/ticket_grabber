"""Tier 2: exact seat availability, read from the booking flow with a real browser.

Why this is separate from :mod:`tg.adapters.cinemacity`: tier 1 reads a public JSON
API that is fast, permitted and reliable, and it already yields how *many* seats are
free. This module answers *which* seats, which the API does not publish — it exists
only inside the booking flow on ``tickets.cinemacity.cz``.

That host is behind Cloudflare bot management and, in testing from a datacenter
address, refused an automated session outright with a block page. Consequences baked
into this design:

* Off by default, and gated behind a tier-1 availability change so it runs rarely.
* Uses a *persistent profile you logged in with yourself*, not a fresh anonymous one.
* On a block or challenge it raises, trips a circuit breaker and stops. It does not
  retry, rotate identity, or attempt to look like a different client.
* Selectors are configurable, because they were derived from the site's stylesheet
  rather than from a live seat page (the block prevented that), and will need
  confirming on your own machine with ``tg seatmap probe``.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from tg.browser import AccessBlocked, BrowserSession, assert_not_blocked
from tg.config import SeatMapConfig
from tg.core.normalize import (
    NormScreening,
    Seat,
    SeatMap,
    SeatStatus,
    parse_row_index,
    parse_seat_index,
)
from tg.core.timeutil import utcnow_aware

if TYPE_CHECKING:
    from playwright.async_api import Page

log = logging.getLogger(__name__)

#: Defaults derived from the booking bundle's CSS (``.choose-seats-root``, seat state
#: classes such as ``...seating_NA``). Override in config if the site moves.
DEFAULT_SELECTORS = {
    "root": "[class*=choose-seats]",
    "seat": "[class*=seat][data-row], [data-seat], [class*=seat-item]",
    "row_attr": "data-row",
    "seat_attr": "data-seat-number",
}

#: Class-name fragments mapped to normalized seat states.
_STATUS_HINTS: tuple[tuple[str, SeatStatus], ...] = (
    ("_na", SeatStatus.UNAVAILABLE),
    ("unavailable", SeatStatus.UNAVAILABLE),
    ("occupied", SeatStatus.TAKEN),
    ("taken", SeatStatus.TAKEN),
    ("sold", SeatStatus.TAKEN),
    ("reserved", SeatStatus.HELD),
    ("held", SeatStatus.HELD),
    ("selected", SeatStatus.HELD),
    ("broken", SeatStatus.UNAVAILABLE),
    ("available", SeatStatus.AVAILABLE),
    ("free", SeatStatus.AVAILABLE),
)


def classify(class_name: str) -> SeatStatus:
    """Map a seat element's classes onto a normalized status.

    Order matters: 'unavailable' contains 'available', so negatives are checked first.
    """
    lowered = (class_name or "").lower()
    for fragment, status in _STATUS_HINTS:
        if fragment in lowered:
            return status
    return SeatStatus.UNKNOWN


class CinemaCitySeatReader:
    """Reads one screening's seat map. Long-lived; reuses a single browser context."""

    def __init__(self, config: SeatMapConfig) -> None:
        self.config = config
        self.selectors = DEFAULT_SELECTORS | dict(config.selectors)
        self._session: BrowserSession | None = None
        #: Set when the site blocks us; suppresses further attempts until it expires.
        self.blocked_until: float | None = None

    def is_blocked(self) -> bool:
        import time

        return self.blocked_until is not None and time.time() < self.blocked_until

    def _trip_breaker(self) -> None:
        import time

        self.blocked_until = time.time() + self.config.backoff_after_block_seconds
        log.warning(
            "seat map reading disabled for %ds — the booking host refused the session. "
            "Availability alerting from tier 1 is unaffected.",
            self.config.backoff_after_block_seconds,
        )

    async def _page(self) -> Page:
        if self._session is None:
            self._session = BrowserSession(self.config)
            await self._session.start()
        assert self._session.context is not None
        pages = self._session.context.pages
        return pages[0] if pages else await self._session.context.new_page()

    async def read(self, screening: NormScreening) -> SeatMap | None:
        if self.is_blocked():
            log.debug("skipping seat map for %s — circuit breaker open", screening.key)
            return None
        if not screening.booking_url:
            return None

        page = await self._page()
        try:
            await page.goto(screening.booking_url, wait_until="domcontentloaded")
            await assert_not_blocked(page)
            await page.wait_for_selector(
                self.selectors["root"], timeout=self.config.timeout_seconds * 1000
            )
            # Re-check: challenges frequently appear after the first paint.
            await assert_not_blocked(page)
            seats = await self._extract(page)
        except AccessBlocked as exc:
            log.warning("seat map blocked for %s: %s", screening.key, exc)
            self._trip_breaker()
            raise
        except Exception as exc:  # noqa: BLE001 — tier 2 is best-effort by contract
            log.warning("seat map unavailable for %s: %s", screening.key, exc)
            return None

        if not seats:
            log.warning(
                "no seats parsed for %s — selectors likely need updating; "
                "run `tg seatmap probe` to inspect the page",
                screening.key,
            )
            return None

        return SeatMap(screening_key=screening.key, seats=seats, captured_at=utcnow_aware())

    async def _extract(self, page: Page) -> list[Seat]:
        raw = await page.eval_on_selector_all(
            self.selectors["seat"],
            """(els, cfg) => els.map(e => ({
                cls: (e.className && e.className.baseVal) || e.className || '',
                row: e.getAttribute(cfg.row_attr) || e.getAttribute('data-row-label') || '',
                seat: e.getAttribute(cfg.seat_attr) || e.getAttribute('data-seat') || '',
                label: (e.getAttribute('aria-label') || e.getAttribute('title') || '').trim(),
                text: (e.textContent || '').trim().slice(0, 8),
                section: e.closest('[data-section]')?.getAttribute('data-section') || null
            }))""",
            self.selectors,
        )

        seats: list[Seat] = []
        for item in raw:
            row_label = item["row"] or _from_label(item["label"], "row")
            seat_label = item["seat"] or item["text"] or _from_label(item["label"], "seat")
            if not row_label or not seat_label:
                continue
            seats.append(
                Seat(
                    row_label=str(row_label),
                    seat_label=str(seat_label),
                    status=classify(item["cls"]),
                    row_index=parse_row_index(str(row_label)),
                    seat_index=parse_seat_index(str(seat_label)),
                    section=item["section"],
                )
            )
        return seats

    async def aclose(self) -> None:
        if self._session is not None:
            await self._session.aclose()
            self._session = None


def _from_label(label: str, want: str) -> str | None:
    """Pull row/seat out of an accessible label like ``Řada 8, sedadlo 12``."""
    import re

    if not label:
        return None
    patterns = {
        "row": r"(?:row|řada|rada|rz?ada)\s*([A-Za-z0-9]+)",
        "seat": r"(?:seat|sedadlo|místo|misto)\s*([A-Za-z0-9]+)",
    }
    if m := re.search(patterns[want], label, re.IGNORECASE):
        return m.group(1)
    return None
