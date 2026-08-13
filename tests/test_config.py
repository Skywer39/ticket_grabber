"""Config parsing, including the YAML footguns this schema has to survive."""

from __future__ import annotations

import pytest
import yaml
from pydantic import ValidationError

from tg.config import AppConfig, parse_duration

BASE = """
sources:
  cinemacity_cz:
    adapter: cinemacity
    base_url: "https://www.cinemacity.cz"
watches:
  - name: "w"
    source: cinemacity_cz
    trigger:
      on: [NEW_SCREENING, AVAILABILITY_RISE]
    assist: off
    match:
      time_between: ["16:00", "23:00"]
"""


def test_yaml_on_key_and_off_value_survive_round_trip():
    """YAML 1.1 resolves a bare ``on:`` key to boolean True and ``off`` to False.
    Both appear naturally in this config, so both must be handled rather than
    silently ignored."""
    cfg = AppConfig.model_validate(yaml.safe_load(BASE))
    watch = cfg.watches[0]
    assert watch.trigger.events == ["NEW_SCREENING", "AVAILABILITY_RISE"]
    assert watch.assist == "off"


def test_events_key_also_works():
    raw = yaml.safe_load(BASE.replace("      on:", "      events:"))
    assert AppConfig.model_validate(raw).watches[0].trigger.events == [
        "NEW_SCREENING",
        "AVAILABILITY_RISE",
    ]


def test_time_window_parses_as_times_not_sexagesimal():
    cfg = AppConfig.model_validate(yaml.safe_load(BASE))
    start, end = cfg.watches[0].match.time_between
    assert (start.hour, start.minute) == (16, 0)
    assert (end.hour, end.minute) == (23, 0)


def test_unknown_source_reference_is_rejected():
    raw = yaml.safe_load(BASE)
    raw["watches"][0]["source"] = "nope"
    with pytest.raises(ValidationError, match="unknown source"):
        AppConfig.model_validate(raw)


def test_unknown_seat_profile_is_rejected():
    raw = yaml.safe_load(BASE)
    raw["watches"][0]["seats"] = {"profile": "missing"}
    with pytest.raises(ValidationError, match="unknown seat profile"):
        AppConfig.model_validate(raw)


def test_invalid_regex_is_rejected_at_load_time():
    """Better to fail on startup than to throw mid-poll at 3am."""
    raw = yaml.safe_load(BASE)
    raw["watches"][0]["match"]["title_regex"] = "([unclosed"
    with pytest.raises(ValidationError, match="invalid regex"):
        AppConfig.model_validate(raw)


def test_invalid_weekday_is_rejected():
    raw = yaml.safe_load(BASE)
    raw["watches"][0]["match"]["weekdays"] = ["funday"]
    with pytest.raises(ValidationError, match="unknown weekday"):
        AppConfig.model_validate(raw)


@pytest.mark.parametrize(
    ("text", "seconds"), [("30s", 30), ("10m", 600), ("2h", 7200), ("1d", 86400), (45, 45)]
)
def test_duration_parsing(text, seconds):
    assert parse_duration(text) == seconds


def test_bad_duration_is_rejected():
    with pytest.raises(ValueError, match="invalid duration"):
        parse_duration("soon")


def test_example_config_is_valid():
    """The shipped example must actually load — it is the first thing a user runs."""
    from pathlib import Path

    raw = yaml.safe_load(Path("config.example.yaml").read_text(encoding="utf-8"))
    cfg = AppConfig.model_validate(raw)
    assert "cinemacity_cz" in cfg.sources
    assert cfg.sources["cinemacity_cz"].options["tenant_id"] == 10101
    assert any(w.seats.profile == "flora_imax" for w in cfg.watches)


def test_a_watch_whose_window_has_passed_is_flagged(caplog):
    """`date_to` scopes a watch to one film's run and then ages out silently: enabled,
    matching nothing, indistinguishable from a quiet period."""
    import logging

    with caplog.at_level(logging.WARNING):
        AppConfig.model_validate(
            {
                "sources": {"s": {"adapter": "cinemacity", "base_url": "https://x"}},
                "watches": [
                    {
                        "name": "last summer's run",
                        "source": "s",
                        "match": {"date_to": "2020-01-01"},
                        "notify": ["discord"],
                    }
                ],
            }
        )
    assert "in the past" in caplog.text
    assert "last summer's run" in caplog.text


def test_a_current_window_is_not_flagged(caplog):
    import logging
    from datetime import date, timedelta

    future = (date.today() + timedelta(days=30)).isoformat()
    with caplog.at_level(logging.WARNING):
        AppConfig.model_validate(
            {
                "sources": {"s": {"adapter": "cinemacity", "base_url": "https://x"}},
                "watches": [
                    {
                        "name": "current run",
                        "source": "s",
                        "match": {"date_to": future},
                        "notify": ["discord"],
                    }
                ],
            }
        )
    assert "in the past" not in caplog.text
