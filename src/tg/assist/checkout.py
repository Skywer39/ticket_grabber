"""Human-in-the-loop checkout assistance.

Two modes, both of which stop well short of buying anything.

``open``
    Hand the deep link to your real desktop browser. You are already logged in
    there, you hold a normal session, and no automated request ever reaches the
    booking host. This is the default because it is the one that reliably works:
    the host blocks automated sessions, and the seconds you lose clicking a link
    are seconds you were going to spend authenticating anyway.

``drive``
    Additionally steer a Playwright browser — using *your own* persistent profile —
    as far as the seat picker, and pre-select seats matching your venue profile.
    Best-effort: the same bot protection applies.

Non-negotiable in both modes: no challenge is ever solved programmatically, and
nothing past seat selection is ever touched. If a human check appears, the browser
is handed over and you are notified.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import subprocess
import sys
import webbrowser
from dataclasses import dataclass, field

from tg.browser import AccessBlocked, BrowserSession, BrowserUnavailable, assert_not_blocked
from tg.config import AssistConfig, SeatPreference
from tg.core.normalize import Seat, SeatStatus, parse_row_index, parse_seat_index
from tg.core.watches import SeatRun, seat_runs

log = logging.getLogger(__name__)

#: Anything matching these is never clicked, in any mode. The point of the tool is
#: to get a human to the seat picker faster, not to transact on their behalf.
FORBIDDEN_PATTERNS = (
    "pay",
    "platba",
    "zaplatit",
    "purchase",
    "checkout",
    "objednat",
    "confirm",
    "potvrdit",
    "card",
    "karta",
    "continue",
    "pokracovat",
    "pokračovat",
)


@dataclass
class AssistResult:
    screening_key: str
    mode: str
    opened: bool = False
    seats_selected: list[str] = field(default_factory=list)
    handed_over: bool = False
    message: str = ""

    def summary(self) -> str:
        if self.seats_selected:
            return (
                f"{self.mode}: pre-selected {len(self.seats_selected)} seat(s) "
                f"({', '.join(self.seats_selected)}) — complete the purchase yourself"
            )
        return f"{self.mode}: {self.message or ('opened' if self.opened else 'no action')}"


class CheckoutAssistant:
    """Opens or drives a browser to the seat picker. Never buys."""

    def __init__(self, config: AssistConfig) -> None:
        self.config = config
        self._semaphore = asyncio.Semaphore(max(1, config.max_concurrent_sessions))

    async def assist(
        self,
        booking_url: str,
        screening_key: str,
        preference: SeatPreference | None = None,
        min_contiguous: int = 1,
    ) -> AssistResult:
        if not self.config.enabled:
            return AssistResult(
                screening_key, self.config.mode, message="assist disabled in config"
            )
        if not booking_url:
            return AssistResult(screening_key, self.config.mode, message="no booking URL")

        async with self._semaphore:
            if self.config.mode == "open":
                return self._open_externally(booking_url, screening_key)
            return await self._drive(booking_url, screening_key, preference, min_contiguous)

    # ------------------------------------------------------------------ open

    def _open_externally(self, url: str, screening_key: str) -> AssistResult:
        """Hand the URL to the user's real browser."""
        result = AssistResult(screening_key, "open")

        opener = None
        if sys.platform == "darwin":
            opener = "open"
        elif sys.platform.startswith("linux"):
            opener = "xdg-open"

        if opener and shutil.which(opener):
            try:
                subprocess.Popen(
                    [opener, url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
                )
                result.opened = True
                result.message = f"opened in your browser via {opener}"
                return result
            except OSError as exc:
                log.debug("%s failed (%s), falling back to webbrowser", opener, exc)

        try:
            result.opened = webbrowser.open(url)
        except Exception as exc:  # noqa: BLE001 — headless servers have no browser
            log.debug("webbrowser.open failed: %s", exc)
            result.opened = False

        result.message = (
            "opened in your browser"
            if result.opened
            else "no desktop browser here — use the link in the notification"
        )
        return result

    # ----------------------------------------------------------------- drive

    async def _drive(
        self,
        url: str,
        screening_key: str,
        preference: SeatPreference | None,
        min_contiguous: int,
    ) -> AssistResult:
        result = AssistResult(screening_key, "drive")
        try:
            session = BrowserSession(self.config.browser)
            await session.start()
        except BrowserUnavailable as exc:
            result.message = f"browser unavailable: {exc}"
            return result

        assert session.context is not None
        pages = session.context.pages
        page = pages[0] if pages else await session.context.new_page()

        try:
            await page.goto(url, wait_until="domcontentloaded")
            await assert_not_blocked(page)
            await page.wait_for_selector("[class*=choose-seats]", timeout=30000)
            await assert_not_blocked(page)

            seats = await self._read_seats(page)
            runs = seat_runs(seats, preference, min_contiguous)
            if not runs:
                result.message = "reached the seat picker; no seats matched your profile"
            else:
                chosen = runs[0].seats[:min_contiguous] if min_contiguous > 1 else runs[0].seats[:1]
                result.seats_selected = await self._select(page, chosen)
                result.message = f"pre-selected {runs[0].describe()}"
            result.opened = True

        except AccessBlocked as exc:
            # Hand over rather than work around. In a visible browser the user can
            # finish by hand; headless, there is nothing for them to see.
            result.handed_over = True
            result.message = f"handed over — {exc}"
            if self.config.browser.headless:
                await session.aclose()
            return result
        except Exception as exc:  # noqa: BLE001 — assistance is best-effort
            result.message = f"could not reach the seat picker: {exc}"
            await session.aclose()
            return result

        # Deliberately left open: the human finishes in this window.
        log.info("assist: browser left open for %s — %s", screening_key, result.message)
        return result

    async def _read_seats(self, page) -> list[Seat]:  # type: ignore[no-untyped-def]
        from tg.adapters.cinemacity_seats import DEFAULT_SELECTORS, classify

        raw = await page.eval_on_selector_all(
            DEFAULT_SELECTORS["seat"],
            """els => els.map(e => ({
                cls: (e.className && e.className.baseVal) || e.className || '',
                row: e.getAttribute('data-row') || e.getAttribute('data-row-label') || '',
                seat: e.getAttribute('data-seat-number') || e.getAttribute('data-seat') || '',
                text: (e.textContent || '').trim().slice(0, 8)
            }))""",
        )
        seats: list[Seat] = []
        for item in raw:
            row, num = item["row"], item["seat"] or item["text"]
            if not row or not num:
                continue
            seats.append(
                Seat(
                    row_label=str(row),
                    seat_label=str(num),
                    status=classify(item["cls"]),
                    row_index=parse_row_index(str(row)),
                    seat_index=parse_seat_index(str(num)),
                )
            )
        return seats

    async def _select(self, page, seats: list[Seat]) -> list[str]:  # type: ignore[no-untyped-def]
        """Click seat elements only, and only after checking they are safe to click."""
        selected: list[str] = []
        for seat in seats:
            locator = page.locator(
                f"[data-row='{seat.row_label}'][data-seat-number='{seat.seat_label}']"
            )
            if not await locator.count():
                continue
            if not await self._is_safe_to_click(locator.first):
                log.warning("refusing to click element for %s — not a plain seat", seat.id)
                continue
            await locator.first.click()
            selected.append(f"{seat.row_label}-{seat.seat_label}")
            await page.wait_for_timeout(250)
        return selected

    async def _is_safe_to_click(self, locator) -> bool:  # type: ignore[no-untyped-def]
        """Refuse anything whose text or attributes suggest it advances the purchase."""
        try:
            text = ((await locator.inner_text(timeout=1000)) or "").lower()
            cls = ((await locator.get_attribute("class")) or "").lower()
            label = ((await locator.get_attribute("aria-label")) or "").lower()
        except Exception:  # noqa: BLE001 — unreadable means unverified, so refuse
            return False
        haystack = f"{text} {cls} {label}"
        return not any(pattern in haystack for pattern in FORBIDDEN_PATTERNS)


def describe_runs(runs: list[SeatRun], limit: int = 3) -> str:
    return "; ".join(r.describe() for r in runs[:limit]) or "none"


__all__ = ["AssistResult", "CheckoutAssistant", "SeatStatus", "describe_runs"]
