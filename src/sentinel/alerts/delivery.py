"""Send queued deliveries: quiet hours, daily batches, retries, auto-disable.

The database is the queue: the engine runs this right after event generation, and
every run sweeps deliveries that are due (rows locked with ``SKIP LOCKED``, so two
engines never send the same delivery). Times of day (quiet hours, the daily batch and
the weekly digest at 09:00) are Turkey time. A failed send is retried after 1 min, 5 min,
30 min, 2 h and 6 h, then given up; ten failures in a row disable the channel.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sentinel.alerts.channels import (
    DeliveryError,
    Message,
    Senders,
    render,
    render_batch,
    render_digest,
    unsubscribe_token,
)
from sentinel.db.base import utcnow
from sentinel.db.session import Database
from sentinel.models import (
    AlertChannel,
    AlertDelivery,
    AlertEvent,
    AlertRecipient,
    ChannelKind,
    DeliveryStatus,
    JobCursor,
    Organization,
)
from sentinel.risk.digest import DigestViews
from sentinel.security.vault import VaultError
from sentinel.tenancy.credentials import CredentialService
from sentinel.tenancy.roles import Role
from sentinel.tenancy.scope import TenantScope

log = logging.getLogger("sentinel.alerts")
LOCAL = ZoneInfo("Europe/Istanbul")
BACKOFF = (
    timedelta(minutes=1),
    timedelta(minutes=5),
    timedelta(minutes=30),
    timedelta(hours=2),
    timedelta(hours=6),
)
DISABLE_AFTER = 10  # consecutive failed deliveries
DAILY_AT = time(9, 0)
WEEKLY = "alerts.weekly"
BATCH = 100


@dataclass
class DeliveryReport:
    sent: int = 0
    deferred: int = 0
    retried: int = 0
    failed: int = 0
    skipped: int = 0


def _clock_time(value: str | None) -> time | None:
    try:
        hour, minute = (int(x) for x in (value or "").split(":"))
        return time(hour, minute)
    except ValueError:
        return None


def quiet_until(channel: AlertChannel, now: datetime) -> datetime | None:
    """When the channel's quiet hours end, if ``now`` is inside them."""
    start, end = _clock_time(channel.quiet_start), _clock_time(channel.quiet_end)
    if start is None or end is None or start == end:
        return None
    local = now.astimezone(LOCAL)
    current = local.time()
    inside = start <= current < end if start < end else (current >= start or current < end)
    if not inside:
        return None
    day = local.date() if current < end else local.date() + timedelta(days=1)
    return datetime.combine(day, end, tzinfo=LOCAL)


def daily_slot(created: datetime) -> datetime:
    """The first 09:00 (Turkey) after ``created``."""
    local = created.astimezone(LOCAL)
    slot = datetime.combine(local.date(), DAILY_AT, tzinfo=LOCAL)
    return slot if slot > local else slot + timedelta(days=1)


def weekly_slot(now: datetime) -> datetime:
    """The latest Monday 09:00 (Turkey) at or before ``now``."""
    local = now.astimezone(LOCAL)
    monday = local.date() - timedelta(days=local.weekday())
    slot = datetime.combine(monday, DAILY_AT, tzinfo=LOCAL)
    return slot if slot <= local else slot - timedelta(days=7)


class DeliveryWorker:
    def __init__(
        self,
        db: Database,
        senders: Senders,
        vault: CredentialService,
        *,
        public_base_url: str,
        unsubscribe_key: str,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self._db = db
        self._senders = senders
        self._vault = vault
        self._base = public_base_url.rstrip("/")
        self._key = unsubscribe_key
        self._clock = clock

    async def run(self) -> DeliveryReport:
        now = self._clock()
        report = DeliveryReport()
        async with self._db.system("alerts: send due deliveries of every organization") as s:
            due = (
                await s.scalars(
                    select(AlertDelivery)
                    .where(
                        AlertDelivery.status == DeliveryStatus.QUEUED,
                        AlertDelivery.next_attempt_at <= now,
                    )
                    .order_by(AlertDelivery.next_attempt_at)
                    .limit(BATCH)
                    .with_for_update(skip_locked=True)
                )
            ).all()
            batches: dict[tuple[uuid.UUID, str], list[AlertDelivery]] = {}
            for delivery in due:
                channel = await s.get(AlertChannel, delivery.channel_id)
                event = await s.get(AlertEvent, delivery.event_id)
                if channel is None or event is None or not channel.enabled:
                    delivery.status = DeliveryStatus.SKIPPED
                    report.skipped += 1
                    continue
                recipient = await self._recipient(s, delivery)
                if channel.kind in (ChannelKind.EMAIL, ChannelKind.TELEGRAM) and recipient is None:
                    delivery.status = DeliveryStatus.SKIPPED
                    report.skipped += 1
                    continue
                hold = quiet_until(channel, now)
                if hold is not None and not (
                    channel.quiet_bypass_high and event.severity == "high"
                ):
                    delivery.next_attempt_at = hold
                    report.deferred += 1
                    continue
                if channel.daily:
                    slot = daily_slot(delivery.created_at)
                    if now < slot:
                        delivery.next_attempt_at = slot
                        report.deferred += 1
                        continue
                    batches.setdefault((channel.id, delivery.recipient_key), []).append(delivery)
                    continue
                await self._send(s, channel, recipient, [delivery], [event], now, report)
            for (channel_id, _), group in batches.items():
                channel = await s.get(AlertChannel, channel_id)
                events = [e for d in group if (e := await s.get(AlertEvent, d.event_id))]
                if channel is not None:
                    await self._send(
                        s, channel, await self._recipient(s, group[0]), group, events, now, report
                    )
        return report

    async def _recipient(self, s: AsyncSession, delivery: AlertDelivery) -> AlertRecipient | None:
        if not delivery.recipient_key:
            return None
        recipient = await s.get(AlertRecipient, uuid.UUID(delivery.recipient_key))
        if (
            recipient is None
            or recipient.unsubscribed_at is not None
            or recipient.confirmed_at is None
        ):
            return None
        return recipient

    async def _send(
        self,
        s: AsyncSession,
        channel: AlertChannel,
        recipient: AlertRecipient | None,
        deliveries: list[AlertDelivery],
        events: list[AlertEvent],
        now: datetime,
        report: DeliveryReport,
    ) -> None:
        org = await s.get(Organization, channel.org_id)
        home = f"{self._base}/o/{org.slug if org else ''}"
        if len(events) == 1:
            product = events[0].product_id
            message = render(events[0], f"{home}/urunler/{product}" if product else home)
        else:
            message = render_batch(events, f"{home}/riskler")
        try:
            await self._dispatch(s, channel, recipient, message, str(events[0].id))
        except (DeliveryError, VaultError) as exc:
            code = str(exc)[:200] if isinstance(exc, DeliveryError) else "vault"
            for delivery in deliveries:
                delivery.attempts += 1
                delivery.last_error = code
                if delivery.attempts > len(BACKOFF):
                    delivery.status = DeliveryStatus.FAILED
                    report.failed += 1
                else:
                    delivery.next_attempt_at = now + BACKOFF[delivery.attempts - 1]
                    report.retried += 1
            channel.failures += 1
            if channel.failures >= DISABLE_AFTER:
                channel.enabled, channel.disabled_reason = False, "too_many_failures"
                log.warning(
                    "alert channel %s disabled after %d failures", channel.id, channel.failures
                )
            return
        for delivery in deliveries:
            delivery.status, delivery.sent_at = DeliveryStatus.SENT, now
            delivery.attempts += 1
            report.sent += 1
        channel.failures = 0

    async def _dispatch(
        self,
        s: AsyncSession,
        channel: AlertChannel,
        recipient: AlertRecipient | None,
        message: Message,
        event_id: str,
    ) -> None:
        secret: dict[str, str] = {}
        if channel.sealed:
            secret = await self._vault.open_payload(s, channel.org_id, channel.id, channel.sealed)
        if channel.kind in (ChannelKind.SLACK, ChannelKind.WEBHOOK) and "url" not in secret:
            raise DeliveryError("not_configured")
        if channel.kind == ChannelKind.EMAIL and recipient is not None:
            token = unsubscribe_token(self._key, recipient.id)
            await self._senders.email(
                recipient.address, message, f"{self._base}/abone/iptal/{token}"
            )
        elif channel.kind == ChannelKind.TELEGRAM and recipient is not None:
            await self._senders.telegram(recipient.address, f"{message.subject}\n\n{message.text}")
        elif channel.kind == ChannelKind.SLACK:
            await self._senders.slack(secret["url"], message)
        elif channel.kind == ChannelKind.WEBHOOK:
            await self._senders.webhook(secret["url"], secret.get("secret", ""), message, event_id)

    async def run_weekly(self, digests: DigestViews) -> int:
        """Send the weekly digest on Mondays at 09:00 to channels that asked for it.

        The cursor moves before sending: a crash mid-way skips a week's digest for some
        channels rather than sending it twice. The first run only sets the cursor.
        """
        slot = weekly_slot(self._clock())
        async with self._db.system("alerts: weekly digest channels of every organization") as s:
            cursor = await s.get(JobCursor, WEEKLY)
            if cursor is None or cursor.position >= slot:
                if cursor is None:
                    s.add(JobCursor(name=WEEKLY, position=slot))
                return 0
            cursor.position = slot
            channels = (
                await s.scalars(
                    select(AlertChannel).where(
                        AlertChannel.weekly_digest.is_(True), AlertChannel.enabled.is_(True)
                    )
                )
            ).all()
            targets = [(c.id, c.org_id) for c in channels]
        sent = 0
        built: dict[uuid.UUID, Message] = {}
        for channel_id, org_id in targets:
            async with self._db.system("alerts: send one weekly digest") as s:
                channel = await s.get(AlertChannel, channel_id)
                org = await s.get(Organization, org_id)
                if channel is None or org is None:
                    continue
                if org_id not in built:
                    digest = await digests.weekly(TenantScope(org_id, None, Role.VIEWER))
                    built[org_id] = render_digest(
                        digest, org.name, f"{self._base}/o/{org.slug}/ozet"
                    )
                recipients: list[AlertRecipient | None] = [None]
                if channel.kind in (ChannelKind.EMAIL, ChannelKind.TELEGRAM):
                    recipients = list(
                        (
                            await s.scalars(
                                select(AlertRecipient).where(
                                    AlertRecipient.channel_id == channel.id,
                                    AlertRecipient.confirmed_at.is_not(None),
                                    AlertRecipient.unsubscribed_at.is_(None),
                                )
                            )
                        ).all()
                    )
                for recipient in recipients:
                    try:
                        await self._dispatch(
                            s, channel, recipient, built[org_id], f"weekly-{slot.date()}"
                        )
                        sent += 1
                    except (DeliveryError, VaultError) as exc:
                        code = str(exc) if isinstance(exc, DeliveryError) else "vault"
                        log.warning("weekly digest to channel %s failed: %s", channel.id, code)
        return sent
