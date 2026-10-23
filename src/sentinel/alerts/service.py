"""Managing an organization's alerts: channels, recipients, rules, the delivery log.

Channel URLs (Slack and webhook) are secrets: sealed in the organization's vault and
never shown again; the panel shows their host only. A webhook's signing secret is
generated here and shown once. Members see the alert setup and may subscribe
themselves; admins manage channels, other people's addresses and rules.
"""

from __future__ import annotations

import secrets
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from sentinel import audit
from sentinel.alerts.subscriptions import PENDING, SubscriptionService
from sentinel.db.base import utcnow
from sentinel.db.session import Database
from sentinel.models import (
    AlertChannel,
    AlertDelivery,
    AlertEvent,
    AlertRecipient,
    AlertRule,
    ChannelKind,
    Product,
    Service,
    User,
)
from sentinel.risk.impact import LEVELS
from sentinel.security.addresses import parse_target
from sentinel.security.errors import OutboundError
from sentinel.tenancy.credentials import CredentialError, CredentialService, public_host
from sentinel.tenancy.roles import Action, can
from sentinel.tenancy.scope import TenantScope

MAX_CHANNELS = 20
MAX_RECIPIENTS = 50  # per channel
MAX_RULES = 100
TEST_EVERY = timedelta(minutes=1)
LOG_SIZE = 50
KINDS = ("incident", "change", "risk")
_CLOCK = ("quiet_start", "quiet_end")


class AlertError(Exception):
    def __init__(self, key: str) -> None:
        super().__init__(key)
        self.key = key


@dataclass(frozen=True)
class RecipientInfo:
    id: uuid.UUID
    address: str
    confirmed: bool
    unsubscribed: bool
    mine: bool


@dataclass
class ChannelInfo:
    channel: AlertChannel
    recipients: list[RecipientInfo] = field(default_factory=list)


@dataclass(frozen=True)
class DeliveryLine:
    created_at: datetime
    channel: str
    title: str
    severity: str
    status: str
    attempts: int
    last_error: str | None
    sent_at: datetime | None


@dataclass
class AlertsOverview:
    channels: list[ChannelInfo]
    rules: list[AlertRule]
    log: list[DeliveryLine]
    products: dict[uuid.UUID, str]
    services: dict[uuid.UUID, str]
    telegram: bool


def _clock_value(raw: str) -> str | None:
    raw = raw.strip()
    if not raw:
        return None
    hour, _, minute = raw.partition(":")
    if not (hour.isdigit() and minute.isdigit() and len(minute) == 2):
        raise AlertError("alerts.invalid_quiet")
    if not (0 <= int(hour) <= 23 and 0 <= int(minute) <= 59):
        raise AlertError("alerts.invalid_quiet")
    return f"{int(hour):02d}:{minute}"


def _target(raw: str) -> tuple[str, str]:
    """(url, host) of a public https endpoint; the SSRF guard checks again on every send."""
    try:
        target = parse_target(raw.strip())
        host = public_host(target.host)
    except (OutboundError, CredentialError):
        raise AlertError("alerts.invalid_url") from None
    return str(target.url), host


def _name(raw: str) -> str:
    name = " ".join(raw.split())
    if not 1 <= len(name) <= 80:
        raise AlertError("alerts.invalid_name")
    return name


class AlertService:
    def __init__(
        self,
        db: Database,
        credentials: CredentialService,
        subscriptions: SubscriptionService,
        *,
        telegram: bool,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self._db = db
        self._vault = credentials
        self._subscriptions = subscriptions
        self._telegram = telegram
        self._clock = clock

    async def overview(self, scope: TenantScope) -> AlertsOverview:
        scope.require(Action.ALERTS_VIEW)
        async with self._db.tenant(scope) as s:
            channels = (
                await s.scalars(
                    select(AlertChannel)
                    .where(AlertChannel.public.is_(False))
                    .order_by(AlertChannel.name)
                )
            ).all()
            infos = {c.id: ChannelInfo(c) for c in channels}
            manage = can(scope.role, Action.ALERTS_MANAGE)
            for r in (
                await s.scalars(select(AlertRecipient).order_by(AlertRecipient.created_at))
            ).all():
                mine = r.user_id is not None and r.user_id == scope.user_id
                if r.channel_id not in infos or not (manage or mine):
                    continue  # members see their own addresses only
                address = "Telegram" if r.address.startswith(PENDING) else r.address
                infos[r.channel_id].recipients.append(
                    RecipientInfo(
                        r.id,
                        address,
                        r.confirmed_at is not None,
                        r.unsubscribed_at is not None,
                        mine,
                    )
                )
            rules = (await s.scalars(select(AlertRule).order_by(AlertRule.name))).all()
            rows = await s.execute(
                select(AlertDelivery, AlertEvent.title, AlertEvent.severity)
                .join(AlertEvent, AlertEvent.id == AlertDelivery.event_id)
                .where(AlertDelivery.channel_id.in_([c.id for c in channels]))
                .order_by(AlertDelivery.created_at.desc())
                .limit(LOG_SIZE)
            )
            names = {c.id: c.name for c in channels}
            log = [
                DeliveryLine(
                    d.created_at,
                    names.get(d.channel_id, ""),
                    title,
                    severity,
                    d.status,
                    d.attempts,
                    d.last_error,
                    d.sent_at,
                )
                for d, title, severity in rows
            ]
            products = dict(
                (await s.execute(select(Product.id, Product.name).order_by(Product.name))).all()
            )
        async with self._db.system("alerts: catalog service names for rule filters") as s:
            services = dict(
                (await s.execute(select(Service.id, Service.name).order_by(Service.name))).all()
            )
        return AlertsOverview(
            list(infos.values()), list(rules), log, products, services, self._telegram
        )

    # -- channels ------------------------------------------------------------------------

    async def create_channel(
        self,
        scope: TenantScope,
        *,
        name: str,
        kind: str,
        url: str = "",
        quiet_start: str = "",
        quiet_end: str = "",
        quiet_bypass_high: bool = True,
        daily: bool = False,
        weekly_digest: bool = False,
        ip: str = "",
    ) -> tuple[uuid.UUID, str | None]:
        """Returns the channel id and, for a webhook, its signing secret (shown once)."""
        scope.require(Action.ALERTS_MANAGE)
        name = _name(name)
        try:
            channel_kind = ChannelKind(kind)
        except ValueError:
            raise AlertError("alerts.invalid_kind") from None
        if channel_kind is ChannelKind.TELEGRAM and not self._telegram:
            raise AlertError("alerts.telegram_off")
        start, end = _clock_value(quiet_start), _clock_value(quiet_end)
        if (start is None) != (end is None):
            raise AlertError("alerts.invalid_quiet")
        secret: dict[str, str] = {}
        host = None
        if channel_kind in (ChannelKind.SLACK, ChannelKind.WEBHOOK):
            secret["url"], host = _target(url)
        signing = secrets.token_urlsafe(32) if channel_kind is ChannelKind.WEBHOOK else None
        if signing:
            secret["secret"] = signing
        async with self._db.tenant(scope) as s:
            if (
                await s.scalar(select(func.count()).select_from(AlertChannel)) or 0
            ) >= MAX_CHANNELS:
                raise AlertError("alerts.too_many_channels")
            if await s.scalar(select(AlertChannel.id).where(AlertChannel.name == name)):
                raise AlertError("alerts.name_taken")
            channel = AlertChannel(
                org_id=scope.org_id,
                name=name,
                kind=channel_kind,
                target_host=host,
                quiet_start=start,
                quiet_end=end,
                quiet_bypass_high=quiet_bypass_high,
                daily=daily,
                weekly_digest=weekly_digest,
                created_by=scope.user_id,
            )
            s.add(channel)
            await s.flush()
            if secret:
                channel.sealed = await self._vault.seal_payload(s, scope.org_id, channel.id, secret)
            self._audit(s, scope, "alert_channel.created", channel.id, ip, kind=channel_kind.value)
            return channel.id, signing

    async def _channel(
        self, s: AsyncSession, scope: TenantScope, channel_id: uuid.UUID
    ) -> AlertChannel:
        channel = await s.get(AlertChannel, channel_id)
        if channel is None or channel.public:  # managed from the status page settings
            raise AlertError("not_found")
        return channel

    async def update_channel(
        self,
        scope: TenantScope,
        channel_id: uuid.UUID,
        *,
        quiet_start: str,
        quiet_end: str,
        quiet_bypass_high: bool,
        daily: bool,
        weekly_digest: bool,
        enabled: bool,
        ip: str = "",
    ) -> None:
        scope.require(Action.ALERTS_MANAGE)
        start, end = _clock_value(quiet_start), _clock_value(quiet_end)
        if (start is None) != (end is None):
            raise AlertError("alerts.invalid_quiet")
        async with self._db.tenant(scope) as s:
            channel = await self._channel(s, scope, channel_id)
            channel.quiet_start, channel.quiet_end = start, end
            channel.quiet_bypass_high, channel.daily = quiet_bypass_high, daily
            channel.weekly_digest = weekly_digest
            if enabled and not channel.enabled:
                channel.failures, channel.disabled_reason = 0, None
            channel.enabled = enabled
            self._audit(s, scope, "alert_channel.updated", channel.id, ip)

    async def replace_url(
        self, scope: TenantScope, channel_id: uuid.UUID, url: str, *, ip: str = ""
    ) -> None:
        scope.require(Action.ALERTS_MANAGE)
        address, host = _target(url)
        async with self._db.tenant(scope) as s:
            channel = await self._channel(s, scope, channel_id)
            if channel.kind not in (ChannelKind.SLACK, ChannelKind.WEBHOOK):
                raise AlertError("alerts.no_url")
            secret = {}
            if channel.sealed:
                secret = await self._vault.open_payload(s, scope.org_id, channel.id, channel.sealed)
            secret["url"] = address
            channel.sealed = await self._vault.seal_payload(s, scope.org_id, channel.id, secret)
            channel.target_host = host
            self._audit(s, scope, "alert_channel.url_replaced", channel.id, ip, host=host)

    async def rotate_signing_secret(
        self, scope: TenantScope, channel_id: uuid.UUID, *, ip: str = ""
    ) -> str:
        scope.require(Action.ALERTS_MANAGE)
        signing = secrets.token_urlsafe(32)
        async with self._db.tenant(scope) as s:
            channel = await self._channel(s, scope, channel_id)
            if channel.kind != ChannelKind.WEBHOOK or not channel.sealed:
                raise AlertError("alerts.no_signing")
            secret = await self._vault.open_payload(s, scope.org_id, channel.id, channel.sealed)
            secret["secret"] = signing
            channel.sealed = await self._vault.seal_payload(s, scope.org_id, channel.id, secret)
            self._audit(s, scope, "alert_channel.secret_rotated", channel.id, ip)
        return signing

    async def delete_channel(
        self, scope: TenantScope, channel_id: uuid.UUID, *, ip: str = ""
    ) -> None:
        scope.require(Action.ALERTS_MANAGE)
        async with self._db.tenant(scope) as s:
            channel = await self._channel(s, scope, channel_id)
            await s.delete(channel)
            self._audit(s, scope, "alert_channel.deleted", channel_id, ip)

    async def send_test(self, scope: TenantScope, channel_id: uuid.UUID) -> None:
        """Queue a test message on one channel; the engine sends it within seconds."""
        scope.require(Action.ALERTS_MANAGE)
        now = self._clock()
        async with self._db.tenant(scope) as s:
            channel = await self._channel(s, scope, channel_id)
            recent = await s.scalar(
                select(AlertDelivery.id)
                .join(AlertEvent, AlertEvent.id == AlertDelivery.event_id)
                .where(
                    AlertDelivery.channel_id == channel.id,
                    AlertEvent.subject_type == "test",
                    AlertDelivery.created_at > now - TEST_EVERY,
                )
                .limit(1)
            )
            if recent is not None:
                raise AlertError("alerts.test_wait")
            event = AlertEvent(
                org_id=scope.org_id,
                product_id=None,
                subject_type="test",
                subject_id=uuid.uuid4(),
                phase="test",
                severity="low",
                reason="test",
                title=channel.name,
                path=[],
                detail={},
                occurred_at=now,
            )
            s.add(event)
            await s.flush()
            keys = [""]
            if channel.kind in (ChannelKind.EMAIL, ChannelKind.TELEGRAM):
                keys = [
                    str(r)
                    for r in (
                        await s.scalars(
                            select(AlertRecipient.id).where(
                                AlertRecipient.channel_id == channel.id,
                                AlertRecipient.confirmed_at.is_not(None),
                                AlertRecipient.unsubscribed_at.is_(None),
                            )
                        )
                    ).all()
                ]
                if not keys:
                    raise AlertError("alerts.no_recipients")
            for key in keys:
                s.add(
                    AlertDelivery(
                        org_id=scope.org_id,
                        event_id=event.id,
                        channel_id=channel.id,
                        recipient_key=key,
                        next_attempt_at=now,
                    )
                )

    # -- recipients ----------------------------------------------------------------------

    async def add_email(
        self, scope: TenantScope, channel_id: uuid.UUID, address: str, *, ip: str = ""
    ) -> None:
        """An address someone else reads: it gets a confirmation link and nothing else."""
        scope.require(Action.ALERTS_MANAGE)
        address = address.strip().lower()
        if not (3 <= len(address) <= 320 and address.count("@") == 1) or any(
            c in address for c in " \r\n<>,;\"'"
        ):
            raise AlertError("alerts.invalid_email")
        async with self._db.tenant(scope) as s:
            channel = await self._channel(s, scope, channel_id)
            if channel.kind != ChannelKind.EMAIL:
                raise AlertError("alerts.not_email")
            recipient = await self._new_recipient(s, scope, channel, address, None, confirmed=False)
            self._audit(s, scope, "alert_recipient.invited", channel.id, ip)
        await self._subscriptions.send_confirmation(recipient)

    async def subscribe_me(self, scope: TenantScope, channel_id: uuid.UUID) -> None:
        """The signed-in user's own verified address: no confirmation mail needed."""
        scope.require(Action.ALERTS_VIEW)
        async with self._db.tenant(scope) as s:
            channel = await self._channel(s, scope, channel_id)
            if channel.kind != ChannelKind.EMAIL:
                raise AlertError("alerts.not_email")
            user = await s.get(User, scope.user_id) if scope.user_id else None
            if user is None or user.email_verified_at is None:
                raise AlertError("alerts.invalid_email")
            existing = await s.scalar(
                select(AlertRecipient).where(
                    AlertRecipient.channel_id == channel.id, AlertRecipient.address == user.email
                )
            )
            if existing is not None:
                existing.user_id = user.id
                existing.confirmed_at = existing.confirmed_at or self._clock()
                existing.unsubscribed_at, existing.confirm_hash = None, None
                return
            await self._new_recipient(s, scope, channel, user.email, user.id, confirmed=True)

    async def telegram_link(self, scope: TenantScope, channel_id: uuid.UUID) -> str:
        scope.require(Action.ALERTS_VIEW)
        async with self._db.tenant(scope) as s:
            channel = await self._channel(s, scope, channel_id)
            if channel.kind != ChannelKind.TELEGRAM:
                raise AlertError("alerts.not_telegram")
            await self._check_room(s, channel)
        link = await self._subscriptions.telegram_link(scope.org_id, channel_id)
        if link is None:
            raise AlertError("alerts.telegram_off")
        return link

    async def _check_room(self, s: AsyncSession, channel: AlertChannel) -> None:
        count = await s.scalar(
            select(func.count())
            .select_from(AlertRecipient)
            .where(AlertRecipient.channel_id == channel.id)
        )
        if (count or 0) >= MAX_RECIPIENTS:
            raise AlertError("alerts.too_many_recipients")

    async def _new_recipient(
        self,
        s: AsyncSession,
        scope: TenantScope,
        channel: AlertChannel,
        address: str,
        user_id: uuid.UUID | None,
        *,
        confirmed: bool,
    ) -> uuid.UUID:
        await self._check_room(s, channel)
        if await s.scalar(
            select(AlertRecipient.id).where(
                AlertRecipient.channel_id == channel.id, AlertRecipient.address == address
            )
        ):
            raise AlertError("alerts.address_taken")
        recipient = AlertRecipient(
            org_id=scope.org_id,
            channel_id=channel.id,
            address=address,
            user_id=user_id,
            confirmed_at=self._clock() if confirmed else None,
        )
        s.add(recipient)
        await s.flush()
        return recipient.id

    async def resend_confirmation(self, scope: TenantScope, recipient_id: uuid.UUID) -> None:
        scope.require(Action.ALERTS_MANAGE)
        async with self._db.tenant(scope) as s:
            recipient = await s.get(AlertRecipient, recipient_id)
            if recipient is None:
                raise AlertError("not_found")
            if recipient.confirmed_at is not None:
                raise AlertError("alerts.already_confirmed")
        await self._subscriptions.send_confirmation(recipient_id)

    async def remove_recipient(
        self, scope: TenantScope, recipient_id: uuid.UUID, *, ip: str = ""
    ) -> None:
        """Admins remove anyone; members remove their own address."""
        scope.require(Action.ALERTS_VIEW)
        async with self._db.tenant(scope) as s:
            recipient = await s.get(AlertRecipient, recipient_id)
            if recipient is None:
                raise AlertError("not_found")
            mine = recipient.user_id is not None and recipient.user_id == scope.user_id
            if not mine:
                scope.require(Action.ALERTS_MANAGE)
            await s.delete(recipient)
            self._audit(s, scope, "alert_recipient.removed", recipient.channel_id, ip)

    # -- rules ---------------------------------------------------------------------------

    async def create_rule(
        self,
        scope: TenantScope,
        *,
        name: str,
        channel_id: uuid.UUID,
        kinds: list[str],
        min_severity: str,
        product_id: uuid.UUID | None = None,
        service_id: uuid.UUID | None = None,
        include_non_breaking: bool = False,
        ip: str = "",
    ) -> uuid.UUID:
        scope.require(Action.ALERTS_MANAGE)
        name = _name(name)
        if min_severity not in LEVELS:
            raise AlertError("alerts.invalid_severity")
        if not kinds or any(k not in KINDS for k in kinds):
            raise AlertError("alerts.invalid_kinds")
        async with self._db.tenant(scope) as s:
            channel = await self._channel(s, scope, channel_id)
            if product_id is not None and await s.get(Product, product_id) is None:
                raise AlertError("not_found")
            if (await s.scalar(select(func.count()).select_from(AlertRule)) or 0) >= MAX_RULES:
                raise AlertError("alerts.too_many_rules")
            rule = AlertRule(
                org_id=scope.org_id,
                name=name,
                channel_id=channel.id,
                product_id=product_id,
                kinds=sorted(set(kinds)),
                min_severity=min_severity,
                include_non_breaking=include_non_breaking,
            )
            if service_id is not None:
                async with self._db.system("alerts: check a catalog service exists") as catalog:
                    if await catalog.get(Service, service_id) is None:
                        raise AlertError("not_found")
                rule.service_id = service_id
            s.add(rule)
            await s.flush()
            self._audit(s, scope, "alert_rule.created", rule.id, ip)
            return rule.id

    async def set_rule_enabled(
        self, scope: TenantScope, rule_id: uuid.UUID, enabled: bool, *, ip: str = ""
    ) -> None:
        scope.require(Action.ALERTS_MANAGE)
        async with self._db.tenant(scope) as s:
            rule = await s.get(AlertRule, rule_id)
            if rule is None:
                raise AlertError("not_found")
            rule.enabled = enabled
            self._audit(s, scope, "alert_rule.updated", rule.id, ip, enabled=enabled)

    async def delete_rule(self, scope: TenantScope, rule_id: uuid.UUID, *, ip: str = "") -> None:
        scope.require(Action.ALERTS_MANAGE)
        async with self._db.tenant(scope) as s:
            rule = await s.get(AlertRule, rule_id)
            if rule is None:
                raise AlertError("not_found")
            await s.delete(rule)
            self._audit(s, scope, "alert_rule.deleted", rule_id, ip)

    def _audit(
        self,
        s: AsyncSession,
        scope: TenantScope,
        action: str,
        target_id: uuid.UUID,
        ip: str,
        **details: object,
    ) -> None:
        audit.record(
            s,
            action,
            org_id=scope.org_id,
            actor_user_id=scope.user_id,
            target_type=action.split(".", 1)[0],
            target_id=target_id,
            details=details or None,
            ip=ip,
        )
