"""Politeness: per-host rate limiting and robots.txt enforcement.

The monitor's whole value is polling often, which makes being well-behaved a
correctness requirement rather than a nicety — an impolite poller gets blocked and
then detects nothing at all.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from dataclasses import dataclass, field
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

import httpx

log = logging.getLogger(__name__)

ROBOTS_TTL_SECONDS = 24 * 3600


class TokenBucket:
    """Classic token bucket, one per host."""

    def __init__(self, rate_per_minute: int, burst: int | None = None) -> None:
        self.capacity = float(burst if burst is not None else max(1, rate_per_minute // 4))
        self.tokens = self.capacity
        self.refill_per_second = rate_per_minute / 60.0
        self.updated_at = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self) -> float:
        """Block until a token is free. Returns how long it waited, for logging."""
        waited = 0.0
        async with self._lock:
            while True:
                now = time.monotonic()
                elapsed = now - self.updated_at
                self.updated_at = now
                self.tokens = min(self.capacity, self.tokens + elapsed * self.refill_per_second)
                if self.tokens >= 1.0:
                    self.tokens -= 1.0
                    return waited
                deficit = (1.0 - self.tokens) / self.refill_per_second
                waited += deficit
                await asyncio.sleep(deficit)


@dataclass
class _RobotsEntry:
    parser: RobotFileParser | None
    fetched_at: float
    #: True when robots.txt could not be fetched at all (404 etc.) — we then allow,
    #: matching the convention that "no robots.txt" means "no restrictions".
    missing: bool = False


@dataclass
class RobotsCache:
    """Fetches and caches robots.txt per origin."""

    user_agent: str
    timeout: float = 10.0
    _entries: dict[str, _RobotsEntry] = field(default_factory=dict)
    _locks: dict[str, asyncio.Lock] = field(default_factory=dict)

    def _lock_for(self, origin: str) -> asyncio.Lock:
        return self._locks.setdefault(origin, asyncio.Lock())

    async def allowed(self, url: str, client: httpx.AsyncClient | None = None) -> bool:
        parsed = urlparse(url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        entry = self._entries.get(origin)
        if entry is None or (time.time() - entry.fetched_at) > ROBOTS_TTL_SECONDS:
            async with self._lock_for(origin):
                entry = self._entries.get(origin)
                if entry is None or (time.time() - entry.fetched_at) > ROBOTS_TTL_SECONDS:
                    entry = await self._fetch(origin, client)
                    self._entries[origin] = entry
        if entry.missing or entry.parser is None:
            return True
        return entry.parser.can_fetch(self.user_agent, url)

    async def _fetch(
        self, origin: str, client: httpx.AsyncClient | None
    ) -> _RobotsEntry:
        url = f"{origin}/robots.txt"
        owns_client = client is None
        client = client or httpx.AsyncClient(timeout=self.timeout, follow_redirects=True)
        try:
            resp = await client.get(url, headers={"User-Agent": self.user_agent})
            if resp.status_code >= 400:
                log.debug(
                    "robots.txt %s -> HTTP %s, treating as unrestricted", url, resp.status_code
                )
                return _RobotsEntry(parser=None, fetched_at=time.time(), missing=True)
            parser = RobotFileParser()
            parser.parse(resp.text.splitlines())
            return _RobotsEntry(parser=parser, fetched_at=time.time())
        except httpx.HTTPError as exc:
            log.warning("could not fetch %s (%s); treating as unrestricted", url, exc)
            return _RobotsEntry(parser=None, fetched_at=time.time(), missing=True)
        finally:
            if owns_client:
                await client.aclose()

    def seed(self, origin: str, robots_txt: str) -> None:
        """Inject a robots.txt directly. Used by tests."""
        parser = RobotFileParser()
        parser.parse(robots_txt.splitlines())
        self._entries[origin] = _RobotsEntry(parser=parser, fetched_at=time.time())


class RobotsDisallowed(PermissionError):
    """Raised instead of silently skipping, so a misconfigured watch is visible."""


def jittered(seconds: float, ratio: float) -> float:
    """Spread scheduled polls so many watches do not stampede the same second."""
    if ratio <= 0:
        return seconds
    delta = seconds * ratio
    return max(0.0, seconds + random.uniform(-delta, delta))
