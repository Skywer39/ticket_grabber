"""Robots handling and rate limiting.

Being well-behaved is a correctness requirement here, not a nicety: the value of
this tool comes from polling often, and an impolite poller gets blocked and then
detects nothing at all.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from tg.core.ratelimit import RobotsCache, TokenBucket, jittered

ORIGIN = "https://www.cinemacity.cz"


@pytest.fixture
def robots(robots_txt) -> RobotsCache:
    cache = RobotsCache(user_agent="ticket-grabber/0.1")
    cache.seed(ORIGIN, robots_txt)
    return cache


async def test_data_api_is_permitted(robots):
    """The whole tier-1 design depends on this path being allowed."""
    assert await robots.allowed(
        f"{ORIGIN}/cz/data-api-service/v1/quickbook/10101/films/until/2026-09-30"
    )


async def test_booking_path_is_disallowed(robots):
    """The site's robots.txt blocks /booking; the client must refuse to poll it."""
    assert not await robots.allowed(f"{ORIGIN}/booking")
    assert not await robots.allowed(f"{ORIGIN}/booking/seats/224366")


async def test_other_disallowed_paths(robots):
    assert not await robots.allowed(f"{ORIGIN}/tsr/assets/x.png")
    assert await robots.allowed(f"{ORIGIN}/films/odyssea/7268s2r")


async def test_missing_robots_means_unrestricted(robots):
    """A 404 robots.txt is the convention for 'no restrictions stated'."""
    cache = RobotsCache(user_agent="ticket-grabber/0.1")
    cache._entries["https://example.test"] = type(
        "E", (), {"parser": None, "fetched_at": time.time(), "missing": True}
    )()
    assert await cache.allowed("https://example.test/anything")


async def test_token_bucket_limits_sustained_rate():
    """Six requests against a 60/min bucket with burst 1 must take ~5 seconds of
    simulated refill, not zero."""
    bucket = TokenBucket(rate_per_minute=600, burst=1)  # 10/s
    start = time.monotonic()
    for _ in range(6):
        await bucket.acquire()
    assert time.monotonic() - start >= 0.4


async def test_token_bucket_allows_a_burst():
    bucket = TokenBucket(rate_per_minute=60, burst=5)
    start = time.monotonic()
    await asyncio.gather(*(bucket.acquire() for _ in range(5)))
    assert time.monotonic() - start < 0.3


def test_jitter_stays_within_bounds():
    for _ in range(200):
        value = jittered(100, 0.15)
        assert 85 <= value <= 115


def test_zero_jitter_is_exact():
    assert jittered(100, 0) == 100
