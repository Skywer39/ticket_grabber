"""Telegram bot notifier."""

from __future__ import annotations

import html

import httpx

from tg.models import Alert
from tg.notify.base import Notifier


class TelegramNotifier(Notifier):
    name = "telegram"

    def __init__(self, bot_token: str, chat_id: str) -> None:
        self.bot_token = bot_token
        self.chat_id = chat_id

    async def send(self, alert: Alert, client: httpx.AsyncClient) -> None:
        lines = [f"<b>{html.escape(alert.title)}</b>"]
        if alert.body:
            lines.append(html.escape(alert.body))
        if alert.url:
            lines.append(f'<a href="{html.escape(alert.url, quote=True)}">Open booking page</a>')
        lines.append(f"<i>watch: {html.escape(alert.watch_name)}</i>")

        resp = await client.post(
            f"https://api.telegram.org/bot{self.bot_token}/sendMessage",
            json={
                "chat_id": self.chat_id,
                "text": "\n".join(lines),
                "parse_mode": "HTML",
                # The deep link is the point of the message; a link preview card would
                # push it off screen.
                "disable_web_page_preview": True,
            },
        )
        resp.raise_for_status()
