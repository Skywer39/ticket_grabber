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

from tg import db
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


# ------------------------------------------------- a source that has gone blind
#
# The gap alert above covers polling *stopping*. This covers the worse case: polling
# continues, the endpoint keeps answering 200, and it returns nothing — which from the
# alert channel is indistinguishable from a quiet fortnight, and is how a program release
# gets missed.


def _counters(session, *, empty: int = 0, errors: int = 0, data: dict | None = None) -> None:
    session.add(
        PollState(
            cache_key="cinemacity_cz:health",
            source="cinemacity_cz",
            last_polled_at=utcnow(),
            consecutive_empty=empty,
            consecutive_errors=errors,
            data=data or {},
        )
    )
    session.commit()


async def test_repeated_empty_polls_are_reported(engine, session):
    _counters(session, empty=3)

    sent = await engine.health_alert()

    assert [a.screening_key for a in sent] == ["monitor:health:cinemacity_cz"]
    assert "returns no rows" in sent[0].body
    assert engine.recorder.sent == sent


async def test_outright_failure_is_reported_differently(engine, session):
    """The two causes need different first moves, so the message must distinguish them."""
    _counters(session, errors=4)

    body = (await engine.health_alert())[0].body
    assert "failing outright" in body


async def test_a_continuing_outage_is_not_repeated(engine, session):
    """A fortnight of breakage is one message, not one per cycle."""
    _counters(session, empty=3)
    assert len(await engine.health_alert()) == 1
    assert await engine.health_alert() == []


async def test_recovery_rearms_the_alert(engine, session, config):
    """After the source produces data again, a *later* breakage must speak up rather than
    be swallowed as a continuation of the first."""
    from tg.core.scheduler import SourceRunner

    _counters(session, empty=3)
    await engine.health_alert()

    runner = SourceRunner("cinemacity_cz", config, adapter=None)  # type: ignore[arg-type]
    with db.session_scope() as s:
        runner._record_success(s, screening_count=12)  # a good poll
    with db.session_scope() as s:
        state = s.exec(
            select(PollState).where(PollState.cache_key == "cinemacity_cz:health")
        ).first()
        state.consecutive_empty = 3  # broken again, later
        s.add(state)

    assert len(await engine.health_alert()) == 1


async def test_healthy_counters_say_nothing(engine, session):
    _counters(session, empty=1)
    assert await engine.health_alert() == []


async def test_health_threshold_zero_disables(engine, session):
    _counters(session, empty=50)
    engine.config.poll.health_alert_after = 0
    assert await engine.health_alert() == []


async def test_no_dispatcher_is_silent_here_too(config, session):
    _counters(session, empty=9)
    assert await Engine(config).health_alert() == []
