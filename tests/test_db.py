"""Schema upkeep.

The poller's database outlives the code that wrote it: the GitHub Actions runner
restores it from a branch on every run, so it is always a file written by the previous
version. ``create_all`` adds missing tables and stops there, which means a model that
gains a column silently produces ``no such column`` on the next query.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from sqlmodel import select

from tg import db
from tg.models import Screening


def _columns(engine, table: str) -> set[str]:
    with engine.begin() as conn:
        return {r[1] for r in conn.exec_driver_sql(f"PRAGMA table_info('{table}')")}


@pytest.fixture
def aged_database(tmp_path):
    """A database written by an older version: same file, one column short."""
    db.reset_engine()
    engine = db.init_engine(f"sqlite:///{tmp_path / 'old.db'}")
    db.create_all()

    with db.session_scope() as s:
        s.add(
            Screening(
                key="cinemacity_cz:220716",
                source="cinemacity_cz",
                external_id="220716",
                event_key="cinemacity_cz:7268s2r",
                venue_key="cinemacity_cz:1052",
                starts_at=datetime(2026, 8, 4, 7, 0, tzinfo=UTC).replace(tzinfo=None),
                auditorium="IMAX VOLVO",
                availability_ratio=0.0156,
            )
        )

    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE screening DROP COLUMN info_url"))
    assert "info_url" not in _columns(engine, "screening")

    db.reset_engine()
    yield f"sqlite:///{tmp_path / 'old.db'}"
    db.reset_engine()


def test_startup_adds_the_missing_column_and_keeps_the_rows(aged_database):
    engine = db.init_engine(aged_database)
    db.create_all()

    assert "info_url" in _columns(engine, "screening")
    with db.session_scope() as s:
        row = s.exec(select(Screening)).one()
        assert row.auditorium == "IMAX VOLVO"      # the data survived the migration
        assert row.availability_ratio == 0.0156
        assert row.info_url is None                # and the new column reads as empty


def test_running_it_again_changes_nothing(aged_database):
    engine = db.init_engine(aged_database)
    db.create_all()
    before = _columns(engine, "screening")

    db.ensure_columns(engine)
    db.ensure_columns(engine)

    assert _columns(engine, "screening") == before


def test_it_never_touches_a_table_that_matches_its_model(tmp_path):
    db.reset_engine()
    engine = db.init_engine(f"sqlite:///{tmp_path / 'fresh.db'}")
    db.create_all()
    before = {t: _columns(engine, t) for t in ("screening", "alert", "venue", "event")}

    db.ensure_columns(engine)

    assert {t: _columns(engine, t) for t in before} == before
    db.reset_engine()
