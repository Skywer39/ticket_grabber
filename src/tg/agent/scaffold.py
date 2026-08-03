"""Draft an adapter from a discovered endpoint, and repair one that has stopped working.

This is where an LLM genuinely earns its place in this project. Not for defeating
human checks — for the tedious, repetitive job of reading an unfamiliar JSON payload
and writing the field mapping, and for re-deriving that mapping when a site quietly
renames things at 3am.

Both commands are advisory: they emit a draft for you to read and commit. Nothing is
written into a running adapter automatically, because a wrong mapping fails silently
(zero results look exactly like a quiet week) and that is worth a human glance.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass

from tg.agent.discover import EndpointCandidate

log = logging.getLogger(__name__)

MODEL = "claude-opus-4-5"

_SYSTEM = """You map ticketing-site JSON onto a fixed schema. You are given one
sample record from a real endpoint. Reply with JSON only, no prose.

Target schema (use null when the source has no equivalent):
{
  "collection_path": "dotted path to the list of showings in the payload",
  "fields": {
    "external_id": "...", "event_external_id": "...", "venue_external_id": "...",
    "starts_at": "...", "auditorium": "...", "booking_url": "...",
    "sold_out": "...", "availability_ratio": "...", "attributes": "..."
  },
  "notes": "one sentence on anything ambiguous",
  "confidence": "high|medium|low"
}
Field values are dotted paths *within one record*. Prefer a ratio or count of free
seats over a boolean when both exist: it detects cancellations, a boolean does not."""


@dataclass
class AdapterDraft:
    endpoint: str
    collection_path: str
    fields: dict[str, str | None]
    notes: str = ""
    confidence: str = "unknown"

    def to_yaml(self, source_key: str = "new_source") -> str:
        lines = [
            f"# Draft adapter mapping for {self.endpoint}",
            f"# confidence: {self.confidence}",
        ]
        if self.notes:
            lines.append(f"# {self.notes}")
        lines += [
            f"{source_key}:",
            f"  endpoint: {self.endpoint}",
            f"  collection_path: {self.collection_path}",
            "  fields:",
        ]
        for key, value in self.fields.items():
            lines.append(f"    {key}: {value if value else 'null'}")
        return "\n".join(lines)


class ModelUnavailable(RuntimeError):
    """No API key, or the anthropic package is not installed."""


def draft_mapping(candidate: EndpointCandidate, api_key: str | None = None) -> AdapterDraft:
    """Ask a model to map one sample record onto the normalized schema."""
    api_key = api_key or os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise ModelUnavailable(
            "ANTHROPIC_API_KEY is not set — the ranked endpoint list from "
            "`tg adapter discover` is still usable for writing the adapter by hand"
        )
    try:
        from anthropic import Anthropic
    except ImportError as exc:
        raise ModelUnavailable(
            "anthropic is not installed — `pip install 'ticket-grabber[agent]'`"
        ) from exc

    if not candidate.sample:
        raise ModelUnavailable(f"no sample record captured for {candidate.url}")

    client = Anthropic(api_key=api_key)
    response = client.messages.create(
        model=MODEL,
        max_tokens=1200,
        system=_SYSTEM,
        messages=[
            {
                "role": "user",
                "content": (
                    f"Endpoint: {candidate.url}\n"
                    f"Collection path: {candidate.collection_path}\n"
                    f"Sample record:\n{json.dumps(candidate.sample, indent=2, default=str)[:6000]}"
                ),
            }
        ],
    )

    text = "".join(block.text for block in response.content if block.type == "text").strip()
    parsed = _parse_json(text)
    return AdapterDraft(
        endpoint=candidate.url,
        collection_path=parsed.get("collection_path") or candidate.collection_path,
        fields=parsed.get("fields") or {},
        notes=parsed.get("notes", ""),
        confidence=parsed.get("confidence", "unknown"),
    )


def _parse_json(text: str) -> dict:
    """Tolerate a fenced block, since models often wrap JSON in one."""
    if text.startswith("```"):
        text = text.split("```")[1]
        text = text.removeprefix("json").strip()
    try:
        return json.loads(text)
    except ValueError as exc:
        raise ModelUnavailable(f"model did not return usable JSON: {text[:200]}") from exc


def diagnose_health(consecutive_empty: int, consecutive_errors: int, threshold: int = 3) -> str:
    """Turn poll-health counters into a plain-language verdict.

    A source returning nothing repeatedly is the signature of a site change: an
    adapter that raises is obvious, one that quietly returns zero rows is not, and
    that silence is exactly how you miss a program release.
    """
    if consecutive_errors >= threshold:
        return (
            f"{consecutive_errors} consecutive errors — the endpoint is failing outright. "
            "Check the URL and any tenant/site id first."
        )
    if consecutive_empty >= threshold:
        return (
            f"{consecutive_empty} consecutive empty polls — the request succeeds but "
            "returns no rows. Usually a renamed field or moved collection path. "
            "Re-run `tg adapter discover <url>` and compare."
        )
    return "healthy"
