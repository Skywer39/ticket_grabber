"""The alert that fires when nobody was polling.

What takes this project down is not the poller but its CI: on 2026-08-06 an Actions
outage refused to allocate a runner for two consecutive scheduled runs, and the only
notice of it was a workflow-failure email. These cover the replacement signal — a
resumed session naming the hole on the channel that is actually read.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta

import httpx
import pytest
from sqlmodel import select

from tg.core.scheduler import Engine
from tg.models import Alert, PollState, utcnow
from tg.notify.base import Dispatcher, Notifier


class _Recorder(Notifier):
    name = "discord"

    def __init__(self) -> None:
        self.sent: list[Alert] = []

    async def send(self, alert: Alert, client: httpx.AsyncClient) -> None:
        self.sent.append(alert)


@pytest.fixture
def engine(config):
    recorder = _Recorder()
    eng = Engine(config, Dispatcher({"discord": recorder}))
    eng.recorder = recorder  # type: ignore[attr-defined]
    yield eng


def _health(session, minutes_ago: float, *, suffix: str = "health") -> None:
    session.add(
        PollState(
            cache_key=f"cinemacity_cz:{suffix}",
            source="cinemacity_cz",
            last_polled_at=utcnow() - timedelta(minutes=minutes_ago),
        )
    )
    session.commit()


async def test_no_state_is_a_seeded_baseline_not_a_gap(engine, session):
    """A database with no health row has no earlier poll to have been late for.

    Without this the alert would fire on every genuinely first run, and — worse — on
    every run after `reset_state`, which is exactly when the operator already knows.
    """
    assert await engine.coverage_gap_alert() is None
    assert engine.recorder.sent == []


async def test_recent_poll_is_silent(engine, session):
    _health(session, minutes_ago=10)

    assert await engine.coverage_gap_alert() is None
    assert engine.recorder.sent == []


async def test_gap_alerts_and_is_persisted(engine, session):
    _health(session, minutes_ago=245)
    # A fresh non-health row must not mask the stale health one: rotation bookkeeping
    # says nothing about whether the site was polled.
    _health(session, minutes_ago=0, suffix="rotation")

    alert = await engine.coverage_gap_alert()

    assert alert is not None
    assert "4h 05m" in alert.title
    assert [a.title for a in engine.recorder.sent] == [alert.title]

    stored = session.exec(select(Alert).where(Alert.screening_key == "monitor:coverage")).all()
    assert len(stored) == 1
    assert stored[0].change_type == "COVERAGE_GAP"
    assert stored[0].delivered is True


async def test_threshold_zero_disables(engine, session):
    engine.config.poll.gap_alert_minutes = 0
    _health(session, minutes_ago=600)

    assert await engine.coverage_gap_alert() is None
    assert engine.recorder.sent == []


async def test_run_forever_reports_the_gap_before_polling(engine, session):
    """Wiring check: the alert hangs off the long-running loop, not off a poll.

    It has to be `run_forever` specifically — `poll_once` also backs `tg run --once`,
    which is a hand-run probe and would announce a gap every time it was used.
    """
    _health(session, minutes_ago=200)
    stop = asyncio.Event()
    stop.set()  # no poll cycles: the gap report is all this exercises

    await engine.run_forever(stop)

    assert [a.screening_key for a in engine.recorder.sent] == ["monitor:coverage"]


async def test_no_dispatcher_is_silent(config, session):
    """`--dry-run` builds no dispatcher; it must not blow up on the way past."""
    _health(session, minutes_ago=600)

    assert await Engine(config).coverage_gap_alert() is None
