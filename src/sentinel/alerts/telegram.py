"""The Telegram bot: link chats to channels, list and cancel subscriptions.

Polls ``getUpdates`` (GET, no webhook: self-hosted installs often have no public
inbound URL) and keeps the update offset in Redis. Telegram itself forgets updates once
a poll passes their offset, so losing Redis replays at most the last batch. Understands
three commands: ``/start <token>`` (from a one-time link made in the panel),
``/abonelikler`` and ``/iptal``. The bot token is part of every Telegram URL, so neither
URLs nor response bodies are ever logged.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

from sentinel.alerts.channels import DeliveryError, Senders
from sentinel.alerts.subscriptions import SubscriptionService
from sentinel.security.errors import OutboundError
from sentinel.security.http import FetchPolicy, OutboundClient, Timeouts
from sentinel.web.i18n import t

log = logging.getLogger("sentinel.alerts")
OFFSET_KEY = "alerts:telegram:offset"
_POLICY = FetchPolicy(
    max_redirects=0,
    max_bytes=1024 * 1024,
    timeouts=Timeouts(connect_s=5.0, read_s=10.0, total_s=15.0),
)


@dataclass
class PollReport:
    updates: int = 0
    replies: int = 0


def parse_command(text: str) -> tuple[str, str]:
    """``"/start@SomeBot abc"`` → ``("/start", "abc")``."""
    head, _, rest = text.strip().partition(" ")
    return head.split("@", 1)[0].lower(), rest.strip()


class TelegramBot:
    def __init__(
        self,
        http: OutboundClient,
        senders: Senders,
        subscriptions: SubscriptionService,
        redis: Any,
        *,
        token: str,
    ) -> None:
        self._http = http
        self._senders = senders
        self._subscriptions = subscriptions
        self._redis = redis
        self._token = token

    async def poll(self) -> PollReport:
        report = PollReport()
        offset = int(await self._redis.get(OFFSET_KEY) or 0)
        query = urlencode({"offset": offset, "timeout": 0, "allowed_updates": '["message"]'})
        url = f"https://api.telegram.org/bot{self._token}/getUpdates?{query}"
        try:
            result = await self._http.fetch(url, _POLICY)
        except OutboundError as exc:
            log.warning("telegram poll failed: %s", exc.kind)
            return report
        if result.status_code != 200:
            log.warning("telegram poll failed: http_%d", result.status_code)
            return report
        try:
            updates = json.loads(result.body).get("result") or []
        except (ValueError, AttributeError):
            log.warning("telegram poll failed: parse_error")
            return report
        for update in updates:
            if not isinstance(update, dict) or not isinstance(update.get("update_id"), int):
                continue
            offset = max(offset, update["update_id"] + 1)
            report.updates += 1
            message = update.get("message") or {}
            chat = message.get("chat") or {}
            text = message.get("text")
            if not isinstance(text, str) or not isinstance(chat.get("id"), int):
                continue
            reply = await self.answer(str(chat["id"]), text)
            try:
                await self._senders.telegram(str(chat["id"]), reply)
                report.replies += 1
            except DeliveryError as exc:
                log.warning("telegram reply failed: %s", exc)
        # the offset moves even when a reply failed: a broken update must not block the bot
        await self._redis.set(OFFSET_KEY, offset)
        return report

    async def answer(self, chat_id: str, text: str) -> str:
        command, argument = parse_command(text)
        if command == "/start" and argument:
            linked = await self._subscriptions.link_telegram(argument, chat_id)
            if linked is None:
                return t("alerts.telegram.invalid")
            return t("alerts.telegram.linked").format(org=linked.org, channel=linked.channel)
        if command == "/abonelikler":
            found = await self._subscriptions.telegram_subscriptions(chat_id)
            if not found:
                return t("alerts.telegram.none")
            lines = [f"- {s.org} / {s.channel}" for s in found]
            return t("alerts.telegram.list") + "\n" + "\n".join(lines)
        if command == "/iptal":
            count = await self._subscriptions.telegram_stop(chat_id)
            return t("alerts.telegram.stopped").format(count=count)
        return t("alerts.telegram.help")
