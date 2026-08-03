"""Endpoint discovery: find the JSON a ticketing site feeds its own front end.

This is the deterministic half of "works for any site". Load a page in a browser,
record every JSON response, and score each one on how much it looks like a schedule:
lists of objects carrying dates, times, titles, venue ids, availability.

Scoring is plain heuristics, and it is useful on its own — the ranked endpoint list
is usually enough to write an adapter by hand. :mod:`tg.agent.scaffold` layers a
model on top to draft the field mapping, but that step is optional.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any

from tg.browser import BrowserSession
from tg.config import BrowserConfig

log = logging.getLogger(__name__)

#: Field names that suggest an object describes a showing.
_TIME_KEYS = re.compile(
    r"(date|time|start|begin|when|showtime|session|eventdate)", re.IGNORECASE
)
_TITLE_KEYS = re.compile(r"(title|name|film|movie|event|show|production)", re.IGNORECASE)
_VENUE_KEYS = re.compile(r"(venue|cinema|theat|hall|screen|auditor|location|site)", re.IGNORECASE)
_AVAIL_KEYS = re.compile(
    r"(avail|seat|capacity|sold|remaining|free|occupanc|ticket)", re.IGNORECASE
)
_ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


@dataclass
class EndpointCandidate:
    url: str
    status: int
    size: int
    score: float = 0.0
    reasons: list[str] = field(default_factory=list)
    #: The most list-like path inside the payload, e.g. ``body.events``.
    collection_path: str = ""
    sample: dict[str, Any] | None = None

    def describe(self) -> str:
        return (
            f"[{self.score:5.1f}] {self.url[:110]}\n"
            f"         {self.collection_path or '(no collection found)'} · "
            f"{self.size}b · {', '.join(self.reasons) or 'no signals'}"
        )


def _walk_collections(node: Any, path: str = "", depth: int = 0):
    """Yield ``(path, list)`` for every non-trivial list of objects in a payload."""
    if depth > 6:
        return
    if isinstance(node, list):
        if node and isinstance(node[0], dict):
            yield path, node
        return
    if isinstance(node, dict):
        for key, value in node.items():
            yield from _walk_collections(value, f"{path}.{key}" if path else key, depth + 1)


def score_payload(payload: Any) -> tuple[float, str, list[str], dict | None]:
    """Score a decoded JSON body on how schedule-like its best collection is."""
    best_score, best_path, best_reasons, best_sample = 0.0, "", [], None

    for path, items in _walk_collections(payload):
        sample = items[0]
        keys = " ".join(sample.keys())
        blob = json.dumps(sample, default=str)[:4000]

        score, reasons = 0.0, []
        if _TIME_KEYS.search(keys):
            score += 3
            reasons.append("time field")
        if _ISO_DATE.search(blob):
            score += 2
            reasons.append("ISO date")
        if _TITLE_KEYS.search(keys):
            score += 2
            reasons.append("title field")
        if _VENUE_KEYS.search(keys):
            score += 2
            reasons.append("venue field")
        if _AVAIL_KEYS.search(keys):
            score += 3
            reasons.append("availability field")
        if len(items) >= 3:
            score += 1
            reasons.append(f"{len(items)} items")
        if "id" in (k.lower() for k in sample):
            score += 1
            reasons.append("stable id")

        if score > best_score:
            best_score, best_path, best_reasons, best_sample = score, path, reasons, sample

    return best_score, best_path, best_reasons, best_sample


async def discover_endpoints(
    url: str,
    browser: BrowserConfig | None = None,
    settle_seconds: float = 12.0,
    max_body: int = 400_000,
) -> list[EndpointCandidate]:
    """Load ``url`` and rank the JSON endpoints its front end calls.

    Read-only: navigates and observes, never clicks or submits.
    """
    browser = browser or BrowserConfig(headless=True)
    captured: dict[str, EndpointCandidate] = {}

    async with BrowserSession(browser) as session:
        assert session.context is not None
        page = await session.context.new_page()

        async def on_response(response: Any) -> None:
            ctype = response.headers.get("content-type", "")
            if "json" not in ctype.lower():
                return
            try:
                body = await response.text()
            except Exception:  # noqa: BLE001 — bodies vanish on redirect/abort
                return
            if not body or len(body) > max_body:
                return
            try:
                payload = json.loads(body)
            except ValueError:
                return

            score, path, reasons, sample = score_payload(payload)
            captured[response.url] = EndpointCandidate(
                url=response.url,
                status=response.status,
                size=len(body),
                score=score,
                reasons=reasons,
                collection_path=path,
                sample=sample,
            )

        page.on("response", on_response)
        await page.goto(url, wait_until="domcontentloaded")
        await page.wait_for_timeout(int(settle_seconds * 1000))

    ranked = sorted(captured.values(), key=lambda c: -c.score)
    log.info("captured %d JSON endpoints, %d look schedule-like", len(ranked),
             len([c for c in ranked if c.score >= 5]))
    return ranked
