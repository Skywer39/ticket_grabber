"""ntfy notifier (ntfy.sh or self-hosted)."""

from __future__ import annotations

import httpx

from tg.models import Alert
from tg.notify.base import Notifier

#: Anything that means "act now" gets a priority that survives Do Not Disturb.
_HIGH_PRIORITY = {"NEW_SCREENING", "NEW_DATE", "SEAT_FREED", "BACK_ON_SALE"}

_TAGS = {
    "NEW_SCREENING": "ticket",
    "NEW_DATE": "calendar",
    "SEAT_FREED": "seat",
    "AVAILABILITY_RISE": "arrow_up",
    "AVAILABILITY_DROP": "chart_with_downwards_trend",
    "SOLD_OUT": "no_entry",
    "BACK_ON_SALE": "recycle",
}


class NtfyNotifier(Notifier):
    name = "ntfy"

    def __init__(self, server: str, topic: str, token: str | None = None) -> None:
        self.server = server.rstrip("/")
        self.topic = topic
        self.token = token

    async def send(self, alert: Alert, client: httpx.AsyncClient) -> None:
        headers = {
            "Title": alert.title.encode("utf-8").decode("latin-1", "replace"),
            "Priority": "high" if alert.change_type in _HIGH_PRIORITY else "default",
            "Tags": _TAGS.get(alert.change_type, "bell"),
        }
        if alert.url:
            headers["Click"] = alert.url
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"

        resp = await client.post(
            f"{self.server}/{self.topic}",
            content=alert.body.encode("utf-8"),
            headers=headers,
        )
        resp.raise_for_status()

    def secret_values(self) -> tuple[str | None, ...]:
        return (self.token, self.topic)
