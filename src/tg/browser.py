"""Shared browser plumbing for the two features that need a real browser.

Kept deliberately small and honest about its limits. The booking host these features
target sits behind Cloudflare bot management, so a headless session may simply be
refused. When that happens the correct behaviour is to stop and say so — not to
retry, rotate, or otherwise try to look like someone else.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from tg.config import BrowserConfig

if TYPE_CHECKING:
    from playwright.async_api import BrowserContext, Page

log = logging.getLogger(__name__)


class BrowserUnavailable(RuntimeError):
    """Playwright or a usable Chromium is not installed."""


class AccessBlocked(RuntimeError):
    """The site refused the automated session, or presented a human check.

    Raised so callers stop cleanly. Working around it is out of scope by design:
    circumventing a ticketing site's access controls is exactly what this project
    does not do.
    """


#: Markers of a Cloudflare block page or an interactive challenge.
_BLOCK_PATTERNS = re.compile(
    r"(you have been blocked|attention required|cf-error|checking your browser|"
    r"enable cookies and reload|just a moment)",
    re.IGNORECASE,
)
_CHALLENGE_SELECTORS = (
    "iframe[src*='challenges.cloudflare.com']",
    "[class*='cf-turnstile']",
    "#challenge-form",
)


@dataclass
class BrowserSession:
    """A persistent-profile Chromium context.

    The profile is persistent on purpose: it is meant to be one *you* logged into
    yourself, so any automation acts within your own established session rather than
    manufacturing a new anonymous one.
    """

    config: BrowserConfig
    _pw: Any = None
    context: BrowserContext | None = None

    async def start(self) -> BrowserContext:
        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:  # pragma: no cover - depends on optional extra
            raise BrowserUnavailable(
                "playwright is not installed — `pip install 'ticket-grabber[browser]'` "
                "and then `playwright install chromium`"
            ) from exc

        profile_dir = Path(os.path.expanduser(self.config.browser_profile_dir))
        profile_dir.mkdir(parents=True, exist_ok=True)

        proxy = self.config.proxy or os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
        launch: dict[str, Any] = {
            "user_data_dir": str(profile_dir),
            "headless": self.config.headless,
            "locale": "cs-CZ",
            "timezone_id": "Europe/Prague",
            "args": list(self.config.browser_args),
        }
        if proxy:
            launch["proxy"] = {"server": proxy}
        if self.config.executable_path:
            launch["executable_path"] = self.config.executable_path

        self._pw = await async_playwright().start()
        try:
            self.context = await self._pw.chromium.launch_persistent_context(**launch)
        except Exception as exc:
            await self.aclose()
            raise BrowserUnavailable(f"could not launch Chromium: {exc}") from exc

        self.context.set_default_timeout(self.config.timeout_seconds * 1000)
        return self.context

    async def aclose(self) -> None:
        if self.context is not None:
            try:
                await self.context.close()
            except Exception:  # noqa: BLE001 — teardown must not mask the real error
                pass
            self.context = None
        if self._pw is not None:
            try:
                await self._pw.stop()
            except Exception:  # noqa: BLE001
                pass
            self._pw = None

    async def __aenter__(self) -> BrowserSession:
        await self.start()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()


async def assert_not_blocked(page: Page) -> None:
    """Raise :class:`AccessBlocked` if the page is a block page or a human check."""
    for selector in _CHALLENGE_SELECTORS:
        if await page.locator(selector).count():
            raise AccessBlocked(
                "the site presented a human verification challenge — "
                "solve it yourself in a visible browser; this tool will not"
            )

    title = (await page.title()) or ""
    body = ""
    try:
        body = await page.locator("body").inner_text(timeout=3000)
    except Exception:  # noqa: BLE001 — a body we cannot read is not proof of a block
        pass

    if _BLOCK_PATTERNS.search(title) or _BLOCK_PATTERNS.search(body[:2000]):
        raise AccessBlocked(
            "the booking host refused this automated session (Cloudflare block page)"
        )
