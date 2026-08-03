from __future__ import annotations

import json
from pathlib import Path

import pytest

from tg import db
from tg.config import AppConfig

FIXTURES = Path(__file__).parent / "fixtures" / "cinemacity"


@pytest.fixture
def fixture_body():
    """Load the ``body`` of a captured Cinema City API response."""

    def _load(name: str) -> dict:
        return json.loads((FIXTURES / name).read_text(encoding="utf-8"))["body"]

    return _load


@pytest.fixture
def robots_txt() -> str:
    return (FIXTURES / "robots.txt").read_text(encoding="utf-8")


@pytest.fixture
def session(tmp_path):
    """A fresh file-backed SQLite database per test.

    File-backed rather than ``:memory:`` because the code opens a new connection
    per session scope, and each in-memory connection would get its own database.
    """
    db.reset_engine()
    db.init_engine(f"sqlite:///{tmp_path / 'test.db'}")
    db.create_all()
    with db.session_scope() as s:
        yield s
    db.reset_engine()


@pytest.fixture
def config() -> AppConfig:
    return AppConfig.model_validate(
        {
            "sources": {
                "cinemacity_cz": {
                    "adapter": "cinemacity",
                    "base_url": "https://www.cinemacity.cz",
                    "options": {
                        "tenant_id": 10101,
                        "cinemas": ["1052"],
                        "timezone": "Europe/Prague",
                    },
                }
            },
            "profiles": {
                "flora_imax": {
                    "auditorium_regex": "(?i)imax",
                    "rows": [8, 14],
                    "seat_range": [10, 20],
                    "avoid_rows": [1, 2, 3],
                }
            },
            "watches": [
                {
                    "name": "Odyssea IMAX 70mm",
                    "source": "cinemacity_cz",
                    "match": {
                        "title_regex": "(?i)odyss",
                        "formats": ["FILM_70MM"],
                        "auditorium_regex": "(?i)imax",
                        "cinemas": ["1052"],
                    },
                    "seats": {"profile": "flora_imax", "min_contiguous": 2},
                    "trigger": {
                        "on": ["NEW_SCREENING", "AVAILABILITY_RISE", "SEAT_FREED"],
                        "availability_rise_min": 0.005,
                    },
                    "notify": ["discord"],
                    "digest_threshold": 100,
                    "cooldown": "10m",
                }
            ],
        }
    )


@pytest.fixture
def adapter():
    """Adapter instance for pure mapping tests — never makes a request."""
    from tg.adapters.cinemacity import CinemaCityAdapter
    from tg.config import SourceConfig

    return CinemaCityAdapter(
        "cinemacity_cz",
        SourceConfig(
            adapter="cinemacity",
            base_url="https://www.cinemacity.cz",
            options={"tenant_id": 10101, "cinemas": ["1052"]},
        ),
        client=None,
    )
