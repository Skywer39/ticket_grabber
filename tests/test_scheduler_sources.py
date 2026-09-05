"""Two source-level scheduling behaviours added for the O2 arena feed.

The date rotation in ``_poll_inner`` exists because a cinema costs one request per
date. A venue that answers its whole programme in a single uncacheable document needs
neither the rotation nor the cadence, and forcing it through both is not merely
wasteful — it quietly disables removal detection, because ``covered`` ends up as the
intersection of a few rotating dates with the scattered days that happen to have
events.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlmodel import select

from tg.config import AppConfig
from tg.core.adapter import Capability
from tg.core.normalize import EventKind, NormEvent, NormScreening, NormVenue
from tg.core.scheduler import Engine, SourceRunner
from tg.core.timeutil import utcnow_aware
from tg.models import PollState

TZ = "Europe/Prague"


def _config(**options) -> AppConfig:
    return AppConfig.model_validate(
        {
            "sources": {
                "o2arena_cz": {
                    "adapter": "o2arena",
                    "base_url": "https://www.o2arena.cz",
                    "options": {"timezone": TZ, "days_ahead": 400, **options},
                }
            },
            "watches": [],
        }
    )


def _screening(external_id: str, days_ahead: int) -> NormScreening:
    return NormScreening(
        source="o2arena_cz",
        external_id=external_id,
        event_external_id="e1",
        venue_external_id="2",
        starts_at=utcnow_aware() + timedelta(days=days_ahead),
        venue_name="O2 arena",
    )


class _WholeHorizonAdapter:
    """Answers the entire programme in one call, ignoring any date narrowing."""

    capabilities = {
        Capability.VENUES,
        Capability.EVENTS,
        Capability.SCREENINGS,
        Capability.WHOLE_HORIZON,
    }

    def __init__(self, programme):
        #: Deliberately not named ``screenings`` — that is the method name.
        self.programme = programme
        self.calls: list[dict] = []

    async def screenings(self, since, until, dates=None):
        self.calls.append({"since": since, "until": until, "dates": dates})
        event = NormEvent(
            source="o2arena_cz", external_id="e1", title="A show", kind=EventKind.CONCERT
        )
        return [event], list(self.programme)

    async def venues(self):
        return [NormVenue(source="o2arena_cz", external_id="2", name="O2 arena")]

    async def aclose(self):
        return None

    async def calendar(self, since, until):  # pragma: no cover - never consulted
        raise AssertionError("a whole-horizon source must not be asked for a calendar")


@pytest.fixture
def runner(session):
    config = _config()
    adapter = _WholeHorizonAdapter([_screening("p1", 30), _screening("p2", 60)])
    session.commit()
    r = SourceRunner("o2arena_cz", config, adapter)  # type: ignore[arg-type]
    r.cycle = 1  # skip the venue refresh
    return r


# ------------------------------------------------------- the whole-horizon path


async def test_fetches_once_with_no_date_narrowing(runner):
    await runner.poll()
    assert len(runner.adapter.calls) == 1
    assert runner.adapter.calls[0]["dates"] is None


async def test_reports_the_window_rather_than_a_rotation(runner):
    """`tg status` should describe the request that was actually made. Under the
    rotation this read '8/401 dates' for a single call covering all 401."""
    report = await runner.poll()
    assert report.dates_probed == report.dates_fetched == 401
    assert report.screenings_seen == 2


async def test_absence_within_the_window_is_a_real_removal(runner):
    """The whole point of the branch: one request covered every date, so a screening
    that stopped appearing genuinely went away. Under the rotation `covered` was almost
    always empty and this never fired."""
    await runner.poll()

    runner.adapter.programme = [_screening("p1", 30)]
    report = await runner.poll()

    removed = [c for c in report.changes if str(c.change_type) == "SCREENING_REMOVED"]
    assert [c.screening_key for c in removed] == ["o2arena_cz:p2"]


async def test_an_empty_feed_removes_nothing(runner):
    """A feed that answers with nothing is a site change, not a cancelled programme."""
    await runner.poll()

    runner.adapter.programme = []
    report = await runner.poll()

    assert not [c for c in report.changes if str(c.change_type) == "SCREENING_REMOVED"]


async def test_screenings_outside_the_window_are_ignored(runner):
    """days_ahead is 400, so a show in three years was not asked for and must not be
    treated as covered — otherwise it would look withdrawn on the very next poll."""
    runner.adapter.programme = [_screening("p1", 30), _screening("far", 1200)]
    report = await runner.poll()

    assert report.screenings_seen == 1
    # The NEW_EVENT change carries no screening key; only the screening-level ones matter.
    keys = {c.screening_key for c in report.changes if c.screening_key}
    assert keys == {"o2arena_cz:p1"}


# ------------------------------------------------------- the per-source interval


def test_no_interval_means_always_due(session):
    runner = SourceRunner("o2arena_cz", _config(), _WholeHorizonAdapter([]))  # type: ignore[arg-type]
    assert runner.due()
    runner._last_polled_at = utcnow_aware()
    assert runner.due(), "a source without a floor follows the global cadence"


def test_interval_holds_a_source_back_until_it_elapses(session):
    config = _config(min_interval_seconds=1800)
    runner = SourceRunner("o2arena_cz", config, _WholeHorizonAdapter([]))  # type: ignore[arg-type]

    assert runner.due(), "never polled — due immediately"

    now = utcnow_aware()
    runner._last_polled_at = now
    assert not runner.due(now + timedelta(minutes=10))
    assert not runner.due(now + timedelta(minutes=29))
    assert runner.due(now + timedelta(minutes=30))


async def test_a_skipped_source_is_not_a_blind_source(session):
    """The health machinery counts polls that came back empty. A source that was never
    asked must not land in that count, or it gets reported broken while working."""
    config = _config(min_interval_seconds=1800)
    engine = Engine(config)
    runner = SourceRunner("o2arena_cz", config, _WholeHorizonAdapter([_screening("p1", 30)]))  # type: ignore[arg-type]
    runner.cycle = 1
    engine.runners = [runner]
    session.commit()

    first = await engine.poll_once()
    assert first[0].skipped is False
    assert runner.adapter.calls, "the first tick polls"

    second = await engine.poll_once()
    assert second[0].skipped is True
    assert second[0].summary() == "o2arena_cz: skipped (not due)"
    assert len(runner.adapter.calls) == 1, "the second tick made no request"

    with __import__("tg.db", fromlist=["session_scope"]).session_scope() as s:
        state = s.exec(
            select(PollState).where(PollState.cache_key == "o2arena_cz:health")
        ).first()
        assert state.consecutive_empty == 0
    await engine.aclose()
