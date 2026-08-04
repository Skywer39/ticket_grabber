"""Notification dispatch, and making misconfiguration legible.

The bug these guard against: a missing webhook secret produced alerts with
``delivered=0`` and ``delivery_error=NULL`` — indistinguishable from "not sent yet".
Diagnosing it needed a read of the code rather than a read of the data.
"""

from __future__ import annotations

import httpx
import pytest

from tg.cli import _channel_report
from tg.config import AppConfig, Secrets
from tg.models import Alert
from tg.notify.base import Dispatcher, Notifier, build_notifiers


class _Recorder(Notifier):
    name = "recorder"

    def __init__(self, fail: bool = False) -> None:
        self.sent: list[Alert] = []
        self.fail = fail

    async def send(self, alert: Alert, client: httpx.AsyncClient) -> None:
        if self.fail:
            raise RuntimeError("webhook returned 401")
        self.sent.append(alert)


def _alert(**kw) -> Alert:
    base = dict(watch_name="w", screening_key="s:1", change_type="NEW_SCREENING",
                title="t", body="b", channels=["discord"])
    return Alert(**(base | kw))


# ------------------------------------------------------- missing credentials


async def test_unconfigured_channel_records_a_named_error():
    """The regression that mattered: an unwired channel must say so in the data."""
    alert = _alert()
    sent, failed = await Dispatcher({}).deliver([alert])

    assert (sent, failed) == (0, 1)
    assert alert.delivered is False
    assert alert.delivery_error is not None
    assert "discord" in alert.delivery_error
    assert "not configured" in alert.delivery_error


async def test_delivery_failure_is_recorded_with_its_cause():
    alert = _alert()
    sent, failed = await Dispatcher({"discord": _Recorder(fail=True)}).deliver([alert])

    assert (sent, failed) == (0, 1)
    assert alert.delivered is False
    assert "401" in alert.delivery_error


async def test_successful_delivery_clears_error_and_marks_delivered():
    recorder = _Recorder()
    alert = _alert()
    sent, failed = await Dispatcher({"discord": recorder}).deliver([alert])

    assert (sent, failed) == (1, 0)
    assert alert.delivered is True
    assert alert.delivery_error is None
    assert recorder.sent == [alert]


async def test_one_broken_channel_does_not_block_the_others():
    good = _Recorder()
    alert = _alert(channels=["discord", "ntfy"])
    sent, _ = await Dispatcher({"discord": _Recorder(fail=True), "ntfy": good}).deliver([alert])

    assert sent == 1
    assert alert.delivered is True          # at least one channel got through
    assert "401" in alert.delivery_error    # but the failure is still visible
    assert good.sent == [alert]


# ------------------------------------------------------------- digested alerts


async def test_digested_alerts_are_never_delivered():
    """Alerts rolled into a digest carry no channels. Now that the dispatcher always
    runs, they must still be skipped rather than broadcast."""
    recorder = _Recorder()
    suppressed = _alert(channels=[], suppressed=True)
    sent, failed = await Dispatcher({"discord": recorder}).deliver([suppressed])

    assert (sent, failed) == (0, 0)         # neither sent nor counted as a failure
    assert recorder.sent == []
    assert suppressed.delivered is False
    assert suppressed.delivery_error is None


async def test_empty_alert_list_is_a_no_op():
    assert await Dispatcher({}).deliver([]) == (0, 0)


# ------------------------------------------------------------ channel wiring


def _secrets(**kw) -> Secrets:
    """Secrets built in isolation.

    ``_env_file=None`` matters: without it pydantic-settings reads the developer's
    real .env, so a locally-configured webhook would leak into these assertions and
    make them pass or fail depending on whose machine they run on.
    """
    return Secrets(_env_file=None, **kw)


def test_build_notifiers_only_returns_channels_with_credentials():
    assert build_notifiers(_secrets()) == {}

    only_discord = build_notifiers(_secrets(discord_webhook_url="https://example.test/hook"))
    assert list(only_discord) == ["discord"]


def test_telegram_needs_both_token_and_chat_id():
    assert build_notifiers(_secrets(telegram_bot_token="t")) == {}
    both = build_notifiers(_secrets(telegram_bot_token="t", telegram_chat_id="1"))
    assert list(both) == ["telegram"]


@pytest.fixture
def cfg_with_discord_watch() -> AppConfig:
    return AppConfig.model_validate(
        {
            "sources": {"s": {"adapter": "cinemacity", "base_url": "https://example.test"}},
            "watches": [{"name": "w", "source": "s", "notify": ["discord"]}],
        }
    )


def test_channel_report_flags_a_watch_wanting_an_unwired_channel(cfg_with_discord_watch):
    summary, missing = _channel_report(cfg_with_discord_watch, {})
    assert summary == "none"
    assert missing == ["discord"]


def test_channel_report_is_quiet_when_everything_is_wired(cfg_with_discord_watch):
    summary, missing = _channel_report(cfg_with_discord_watch, {"discord": _Recorder()})
    assert summary == "discord"
    assert missing == []


def test_disabled_watches_do_not_raise_a_warning(cfg_with_discord_watch):
    cfg_with_discord_watch.watches[0].enabled = False
    assert _channel_report(cfg_with_discord_watch, {})[1] == []
