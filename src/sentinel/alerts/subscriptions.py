"""Recipients opting in and out: e-mail double opt-in, Telegram linking, unsubscribing.

An external e-mail address receives nothing until its owner opens the confirmation
link (48 hours). A Telegram chat joins a channel by sending ``/start <token>`` to the
bot from a one-time link (1 hour). Both tokens are random and stored only as a keyed
digest. Unsubscribe links are an HMAC over the recipient id
(``channels.unsubscribe_token``): nothing to store, one click, idempotent.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select

from sentinel.accounts.mail import Mail, Mailer
from sentinel.accounts.tokens import digest, new_token
from sentinel.alerts.channels import check_unsubscribe
from sentinel.config import Settings
from sentinel.db.base import utcnow
from sentinel.db.session import Database
from sentinel.models import AlertChannel, AlertRecipient, ChannelKind, Organization
from sentinel.web.i18n import t

CONFIRM_TTL = timedelta(hours=48)
LINK_TTL = timedelta(hours=1)
PENDING = "pending:"  # address of a Telegram recipient whose chat is not linked yet
_CONFIRM = "alert-confirm"
_LINK = "alert-telegram"


@dataclass(frozen=True)
class Subscription:
    org: str
    channel: str
    address: str
    unsubscribed: bool = False


class SubscriptionService:
    def __init__(
        self,
        db: Database,
        settings: Settings,
        mailer: Mailer,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self._db = db
        self._mailer = mailer
        self._key = settings.token_hash_key.get_secret_value()
        self._base = settings.public_base
        self._bot = settings.telegram_bot_username
        self._clock = clock

    @property
    def unsubscribe_key(self) -> str:
        return self._key

    # -- e-mail --------------------------------------------------------------------------

    async def send_confirmation(self, recipient_id: uuid.UUID) -> None:
        """Mail a fresh confirmation link to an unconfirmed e-mail recipient."""
        token = new_token()
        async with self._db.system("alerts: confirmation for one recipient") as s:
            recipient = await s.get(AlertRecipient, recipient_id)
            if recipient is None or recipient.confirmed_at is not None:
                return
            channel = await s.get(AlertChannel, recipient.channel_id)
            org = await s.get(Organization, recipient.org_id)
            if channel is None or org is None or channel.kind != ChannelKind.EMAIL:
                return
            recipient.confirm_hash = digest(self._key, token, purpose=_CONFIRM)
            recipient.created_at = self._clock()  # the 48 hours start now
            address, org_name, channel_name = recipient.address, org.name, channel.name
        link = f"{self._base}/abone/onay/{token}"
        body = t("alerts.confirm_mail.body").format(org=org_name, channel=channel_name, link=link)
        subject = t("alerts.confirm_mail.subject").format(org=org_name)
        await self._mailer.send(Mail(to=address, subject=subject, text=body))

    async def _pending(self, token: str) -> AlertRecipient | None:
        async with self._db.system("alerts: look up a confirmation token") as s:
            recipient = await s.scalar(
                select(AlertRecipient).where(
                    AlertRecipient.confirm_hash == digest(self._key, token, purpose=_CONFIRM)
                )
            )
            if recipient is None or recipient.created_at < self._clock() - CONFIRM_TTL:
                return None
            return recipient

    async def preview_confirmation(self, token: str) -> Subscription | None:
        recipient = await self._pending(token)
        return await self._describe(recipient) if recipient else None

    async def confirm(self, token: str) -> Subscription | None:
        found = await self._pending(token)
        if found is None:
            return None
        async with self._db.system("alerts: confirm one recipient") as s:
            recipient = await s.get(AlertRecipient, found.id)
            if recipient is None:  # pragma: no cover - deleted in between
                return None
            recipient.confirmed_at, recipient.confirm_hash = self._clock(), None
            recipient.unsubscribed_at = None
        return await self._describe(found)

    # -- unsubscribing -------------------------------------------------------------------

    async def preview_unsubscribe(self, token: str) -> Subscription | None:
        recipient_id = check_unsubscribe(self._key, token)
        if recipient_id is None:
            return None
        async with self._db.system("alerts: look up an unsubscribe link") as s:
            recipient = await s.get(AlertRecipient, recipient_id)
        return await self._describe(recipient) if recipient else None

    async def unsubscribe(self, token: str) -> Subscription | None:
        """Idempotent: a second click (or a mail client's one-click POST) is still fine."""
        recipient_id = check_unsubscribe(self._key, token)
        if recipient_id is None:
            return None
        async with self._db.system("alerts: unsubscribe one recipient") as s:
            recipient = await s.get(AlertRecipient, recipient_id)
            if recipient is None:
                return None
            if recipient.unsubscribed_at is None:
                recipient.unsubscribed_at = self._clock()
        return await self._describe(recipient)

    async def _describe(self, recipient: AlertRecipient) -> Subscription | None:
        async with self._db.system("alerts: describe one subscription") as s:
            channel = await s.get(AlertChannel, recipient.channel_id)
            org = await s.get(Organization, recipient.org_id)
            row = await s.get(AlertRecipient, recipient.id)
        if channel is None or org is None or row is None:
            return None
        return Subscription(org.name, channel.name, row.address, row.unsubscribed_at is not None)

    # -- Telegram ------------------------------------------------------------------------

    async def telegram_link(self, org_id: uuid.UUID, channel_id: uuid.UUID) -> str | None:
        """A one-time ``t.me`` link that subscribes the chat it is opened in."""
        if not self._bot:
            return None
        token = new_token(24)
        async with self._db.system("alerts: Telegram link for one channel") as s:
            channel = await s.get(AlertChannel, channel_id)
            if channel is None or channel.org_id != org_id or channel.kind != ChannelKind.TELEGRAM:
                return None
            s.add(
                AlertRecipient(
                    org_id=org_id,
                    channel_id=channel_id,
                    address=PENDING + uuid.uuid4().hex,
                    confirm_hash=digest(self._key, token, purpose=_LINK),
                    created_at=self._clock(),
                )
            )
        return f"https://t.me/{self._bot}?start={token}"

    async def link_telegram(self, token: str, chat_id: str) -> Subscription | None:
        hashed = digest(self._key, token, purpose=_LINK)
        async with self._db.system("alerts: link one Telegram chat") as s:
            pending = await s.scalar(
                select(AlertRecipient).where(AlertRecipient.confirm_hash == hashed)
            )
            if pending is None or pending.created_at < self._clock() - LINK_TTL:
                return None
            existing = await s.scalar(
                select(AlertRecipient).where(
                    AlertRecipient.channel_id == pending.channel_id,
                    AlertRecipient.address == chat_id,
                )
            )
            if existing is not None:  # linked before: re-subscribe, drop the pending row
                await s.delete(pending)
                existing.unsubscribed_at = None
                existing.confirmed_at = existing.confirmed_at or self._clock()
                recipient = existing
            else:
                pending.address, pending.confirm_hash = chat_id, None
                pending.confirmed_at = self._clock()
                recipient = pending
            await s.flush()
        return await self._describe(recipient)

    async def telegram_subscriptions(self, chat_id: str) -> list[Subscription]:
        async with self._db.system("alerts: subscriptions of one Telegram chat") as s:
            rows = (
                await s.execute(
                    select(Organization.name, AlertChannel.name)
                    .join(AlertChannel, AlertChannel.org_id == Organization.id)
                    .join(AlertRecipient, AlertRecipient.channel_id == AlertChannel.id)
                    .where(
                        AlertChannel.kind == ChannelKind.TELEGRAM,
                        AlertRecipient.address == chat_id,
                        AlertRecipient.confirmed_at.is_not(None),
                        AlertRecipient.unsubscribed_at.is_(None),
                    )
                    .order_by(Organization.name, AlertChannel.name)
                )
            ).all()
        return [Subscription(org, channel, chat_id) for org, channel in rows]

    async def telegram_stop(self, chat_id: str) -> int:
        """Unsubscribe the chat from every channel; returns how many it left."""
        count = 0
        async with self._db.system("alerts: unsubscribe one Telegram chat") as s:
            for recipient in (
                await s.scalars(
                    select(AlertRecipient)
                    .join(AlertChannel, AlertChannel.id == AlertRecipient.channel_id)
                    .where(
                        AlertChannel.kind == ChannelKind.TELEGRAM,
                        AlertRecipient.address == chat_id,
                        AlertRecipient.unsubscribed_at.is_(None),
                    )
                )
            ).all():
                recipient.unsubscribed_at = self._clock()
                count += 1
        return count
