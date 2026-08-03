"""Notification dispatch.

Three backends are built rather than one because the useful channel is whichever the
user will actually see within a minute, and that is a personal question. Swapping is a
config edit, not a code change.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod

import httpx

from tg.config import Secrets
from tg.models import Alert

log = logging.getLogger(__name__)


class Notifier(ABC):
    name: str

    @abstractmethod
    async def send(self, alert: Alert, client: httpx.AsyncClient) -> None:
        """Deliver one alert. Raise on failure; the dispatcher records it."""

    @staticmethod
    def _text(alert: Alert) -> str:
        parts = [alert.title, alert.body]
        if alert.url:
            parts.append(alert.url)
        return "\n".join(p for p in parts if p)


class NotConfigured(RuntimeError):
    """Channel was requested by a watch but its secrets are missing."""


class Dispatcher:
    """Fans alerts out to their configured channels and records the outcome."""

    def __init__(self, notifiers: dict[str, Notifier], timeout: float = 15.0) -> None:
        self.notifiers = notifiers
        self.timeout = timeout

    async def deliver(self, alerts: list[Alert]) -> tuple[int, int]:
        """Returns ``(sent, failed)``. Delivery state is written onto each alert."""
        if not alerts:
            return 0, 0

        sent = failed = 0
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            for alert in alerts:
                # No channels means "recorded, not for delivery" (a digested alert or
                # a watch with no notify list) — never a licence to broadcast.
                channels = alert.channels
                if not channels:
                    continue
                errors: list[str] = []
                delivered_any = False

                for channel in channels:
                    notifier = self.notifiers.get(channel)
                    if notifier is None:
                        errors.append(f"{channel}: not configured")
                        log.warning(
                            "watch %r wants channel %r but it has no credentials — "
                            "check .env",
                            alert.watch_name,
                            channel,
                        )
                        continue
                    try:
                        await notifier.send(alert, client)
                        delivered_any = True
                    except Exception as exc:  # noqa: BLE001 — one bad channel must not
                        # block the others; the error is recorded on the alert.
                        errors.append(f"{channel}: {exc}")
                        log.error("delivery to %s failed: %s", channel, exc)

                alert.delivered = delivered_any
                alert.delivery_error = "; ".join(errors) or None
                sent += int(delivered_any)
                failed += int(not delivered_any)

        return sent, failed


def build_notifiers(secrets: Secrets | None = None) -> dict[str, Notifier]:
    """Instantiate every channel that has credentials present."""
    from tg.notify.discord import DiscordNotifier
    from tg.notify.ntfy import NtfyNotifier
    from tg.notify.telegram import TelegramNotifier

    secrets = secrets or Secrets()
    out: dict[str, Notifier] = {}

    if secrets.discord_webhook_url:
        out["discord"] = DiscordNotifier(secrets.discord_webhook_url)
    if secrets.ntfy_topic:
        out["ntfy"] = NtfyNotifier(secrets.ntfy_server, secrets.ntfy_topic, secrets.ntfy_token)
    if secrets.telegram_bot_token and secrets.telegram_chat_id:
        out["telegram"] = TelegramNotifier(secrets.telegram_bot_token, secrets.telegram_chat_id)

    return out
