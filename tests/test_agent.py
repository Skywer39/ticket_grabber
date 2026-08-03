"""Endpoint discovery scoring and adapter-health diagnosis."""

from __future__ import annotations

import json

from tg.agent.discover import _walk_collections, score_payload
from tg.agent.scaffold import AdapterDraft, diagnose_health


def test_finds_the_events_collection_in_a_real_payload(fixture_body):
    """The scorer should pick `events` over `films` — showings carry the times and
    the availability, which is what an adapter actually needs."""
    payload = {"body": fixture_body("film-events-1052-2026-08-04.json")}
    score, path, reasons, sample = score_payload(payload)

    assert path == "body.events"
    assert score >= 8
    assert sample is not None and "availabilityRatio" in sample
    assert "availability field" in reasons
    assert "time field" in reasons


def test_scores_a_schedule_payload_above_an_unrelated_one(fixture_body):
    schedule = {"body": fixture_body("film-events-1052-2026-08-04.json")}
    unrelated = {"items": [{"id": 1, "colour": "red"}, {"id": 2, "colour": "blue"}]}

    assert score_payload(schedule)[0] > score_payload(unrelated)[0]


def test_cookie_banner_payloads_score_low():
    """Real capture noise: consent tooling returns plenty of JSON that is not a
    schedule, and it must not outrank the endpoint we want."""
    banner = {
        "DomainData": {
            "Groups": [
                {"GroupName": "Performance Cookies", "Description": "…"},
                {"GroupName": "Targeting Cookies", "Description": "…"},
            ]
        }
    }
    assert score_payload(banner)[0] < 5


def test_walks_nested_collections():
    payload = {"a": {"b": [{"x": 1}]}, "c": [{"y": 2}]}
    found = dict(_walk_collections(payload))
    assert set(found) == {"a.b", "c"}


def test_empty_and_scalar_payloads_are_handled():
    for payload in ({}, [], {"a": []}, {"a": [1, 2, 3]}, "text"):
        score, path, _, sample = score_payload(payload)
        assert score == 0.0 and path == "" and sample is None


def test_venue_and_title_signals_are_recognised():
    payload = {
        "showings": [
            {
                "id": "1",
                "title": "Odyssea",
                "cinemaId": "1052",
                "startTime": "2026-08-04T16:40:00",
                "seatsAvailable": 3,
            }
        ]
    }
    score, path, reasons, _ = score_payload(payload)
    assert path == "showings"
    assert {"title field", "venue field", "availability field", "time field"} <= set(reasons)
    assert score >= 8


# ------------------------------------------------------------------- health


def test_repeated_empty_polls_are_flagged_as_a_likely_site_change():
    """Zero rows looks exactly like a quiet week, which is how a silent breakage
    turns into a missed release."""
    verdict = diagnose_health(consecutive_empty=5, consecutive_errors=0)
    assert "empty polls" in verdict
    assert "renamed field" in verdict


def test_repeated_errors_are_distinguished_from_empty_results():
    verdict = diagnose_health(consecutive_empty=0, consecutive_errors=4)
    assert "consecutive errors" in verdict


def test_healthy_source_reports_healthy():
    assert diagnose_health(0, 0) == "healthy"
    assert diagnose_health(1, 1) == "healthy"  # below threshold, still noise


def test_draft_renders_commentable_yaml():
    draft = AdapterDraft(
        endpoint="https://example.test/api/events",
        collection_path="body.events",
        fields={"external_id": "id", "starts_at": "eventDateTime", "auditorium": None},
        notes="availabilityRatio is a fraction, not a count",
        confidence="high",
    )
    out = draft.to_yaml("example_source")
    assert "example_source:" in out
    assert "collection_path: body.events" in out
    assert "auditorium: null" in out
    assert "# confidence: high" in out
    assert json.dumps(draft.fields)  # fields stay serialisable
