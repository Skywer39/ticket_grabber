"""Shared HTTP client for adapters: rate-limited, robots-aware, conditional-GET aware."""

from __future__ import annotations

import asyncio
import logging
import random
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import httpx

from tg.config import HttpConfig
from tg.core.ratelimit import RobotsCache, RobotsDisallowed, TokenBucket

log = logging.getLogger(__name__)

RETRY_STATUSES = {429, 500, 502, 503, 504}


@dataclass(slots=True)
class FetchResult:
    url: str
    status_code: int
    #: True when the server answered 304 — the caller should keep its previous data.
    not_modified: bool = False
    text: str = ""
    etag: str | None = None
    last_modified: str | None = None
    _json: Any = None

    def json(self) -> Any:
        if self._json is None and self.text:
            import json as _j

            self._json = _j.loads(self.text)
        return self._json


class PoliteClient:
    """One instance per process; shared by every adapter.

    Enforces a per-host token bucket, honours robots.txt, and retries transient
    failures with exponential backoff and jitter.
    """

    def __init__(self, config: HttpConfig) -> None:
        self.config = config
        self._buckets: dict[str, TokenBucket] = {}
        self._robots = RobotsCache(user_agent=config.user_agent, timeout=config.timeout_seconds)
        self._client = httpx.AsyncClient(
            timeout=config.timeout_seconds,
            follow_redirects=True,
            headers={
                "User-Agent": config.user_agent,
                "Accept-Language": "cs-CZ,cs;q=0.9,en;q=0.8",
            },
        )

    @property
    def robots(self) -> RobotsCache:
        return self._robots

    def _bucket(self, url: str) -> TokenBucket:
        host = urlparse(url).netloc
        if host not in self._buckets:
            self._buckets[host] = TokenBucket(self.config.requests_per_minute)
        return self._buckets[host]

    async def check_allowed(self, url: str) -> bool:
        if not self.config.respect_robots:
            return True
        return await self._robots.allowed(url, self._client)

    async def get(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        etag: str | None = None,
        last_modified: str | None = None,
        accept: str = "application/json, text/plain, */*",
    ) -> FetchResult:
        if not await self.check_allowed(url):
            raise RobotsDisallowed(f"robots.txt disallows fetching {url}")

        req_headers = {"Accept": accept}
        if etag:
            req_headers["If-None-Match"] = etag
        if last_modified:
            req_headers["If-Modified-Since"] = last_modified
        if headers:
            req_headers.update(headers)

        last_exc: Exception | None = None
        for attempt in range(self.config.max_retries + 1):
            await self._bucket(url).acquire()
            try:
                resp = await self._client.get(url, headers=req_headers)
            except httpx.HTTPError as exc:
                last_exc = exc
                if attempt >= self.config.max_retries:
                    break
                await self._backoff(attempt, reason=str(exc))
                continue

            if resp.status_code == 304:
                return FetchResult(url=url, status_code=304, not_modified=True)

            if resp.status_code in RETRY_STATUSES and attempt < self.config.max_retries:
                await self._backoff(
                    attempt,
                    reason=f"HTTP {resp.status_code}",
                    retry_after=resp.headers.get("Retry-After"),
                )
                continue

            resp.raise_for_status()
            return FetchResult(
                url=url,
                status_code=resp.status_code,
                text=resp.text,
                etag=resp.headers.get("ETag"),
                last_modified=resp.headers.get("Last-Modified"),
            )

        raise httpx.HTTPError(
            f"GET {url} failed after {self.config.max_retries + 1} attempts: {last_exc}"
        )

    async def _backoff(self, attempt: int, reason: str, retry_after: str | None = None) -> None:
        if retry_after and retry_after.isdigit():
            delay = float(retry_after)
        else:
            delay = (2**attempt) + random.uniform(0, 1)
        log.warning("retrying in %.1fs (attempt %d) after %s", delay, attempt + 1, reason)
        await asyncio.sleep(delay)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> PoliteClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()
