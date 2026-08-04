"""Discord webhook notifier."""

from __future__ import annotations

import httpx

from tg.models import Alert
from tg.notify.base import Notifier

#: Colour-code the embed so the important cases are recognisable at a glance.
_COLOURS = {
    "NEW_SCREENING": 0x2ECC71,
    "NEW_DATE": 0x1ABC9C,
    "SEAT_FREED": 0x3498DB,
    "AVAILABILITY_RISE": 0x3498DB,
    "AVAILABILITY_DROP": 0xE67E22,
    "SOLD_OUT": 0xE74C3C,
    "BACK_ON_SALE": 0x9B59B6,
}


class DiscordNotifier(Notifier):
    name = "discord"

    def __init__(self, webhook_url: str) -> None:
        self.webhook_url = webhook_url

    async def send(self, alert: Alert, client: httpx.AsyncClient) -> None:
        embed: dict = {
            "title": alert.title[:256],
            "description": alert.body[:4096],
            "color": _COLOURS.get(alert.change_type, 0x95A5A6),
            "footer": {"text": f"watch: {alert.watch_name}"},
        }
        if alert.url:
            embed["url"] = alert.url

        payload = {"embeds": [embed]}
        # A bare link under the embed makes the deep link tappable on mobile, where
        # embed titles are easy to miss.
        if alert.url:
            payload["content"] = alert.url

        resp = await client.post(self.webhook_url, json=payload)
        resp.raise_for_status()

    def secret_values(self) -> tuple[str | None, ...]:
        return (self.webhook_url,)
