"""Alerts: channels, recipients, rules, events and deliveries (Phase 9).

One event (something upstream that reaches one product) leads to at most one delivery
per matching channel and recipient: deliveries are unique on (event, channel,
recipient). Channel secrets (webhook and Slack URLs, signing secrets) are sealed in the
organization's vault like credentials, never shown again and never logged.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from sentinel.db.base import Base, CreatedAtMixin, IdMixin, TenantMixin


def _in(column: str, values: type[StrEnum]) -> str:
    return f"{column} IN ({', '.join(repr(v.value) for v in values)})"


class ChannelKind(StrEnum):
    EMAIL = "email"
    SLACK = "slack"  # Slack-compatible incoming webhook ({"text": ...})
    WEBHOOK = "webhook"  # signed JSON
    TELEGRAM = "telegram"


class DeliveryStatus(StrEnum):
    QUEUED = "queued"
    SENT = "sent"
    FAILED = "failed"  # gave up after retries
    SKIPPED = "skipped"  # channel disabled or recipient gone before sending


class AlertChannel(IdMixin, CreatedAtMixin, TenantMixin, Base):
    __tablename__ = "alert_channels"
    __table_args__ = (
        UniqueConstraint("org_id", "name"),
        CheckConstraint(_in("kind", ChannelKind), name="kind"),
    )

    name: Mapped[str] = mapped_column(String(80))
    kind: Mapped[str] = mapped_column(String(16))
    target_host: Mapped[str | None] = mapped_column(String(253))  # shown instead of the URL
    sealed: Mapped[bytes | None] = mapped_column(LargeBinary)  # URL / signing secret
    quiet_start: Mapped[str | None] = mapped_column(String(5))  # "22:00", Turkey time
    quiet_end: Mapped[str | None] = mapped_column(String(5))
    quiet_bypass_high: Mapped[bool] = mapped_column(default=True)
    daily: Mapped[bool] = mapped_column(default=False)  # one message a day instead of each
    weekly_digest: Mapped[bool] = mapped_column(default=False)
    enabled: Mapped[bool] = mapped_column(default=True)
    disabled_reason: Mapped[str | None] = mapped_column(String(64))
    failures: Mapped[int] = mapped_column(Integer, default=0)  # consecutive
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )


class AlertRecipient(IdMixin, CreatedAtMixin, TenantMixin, Base):
    """An e-mail address or Telegram chat of a channel. External addresses opt in twice."""

    __tablename__ = "alert_recipients"
    __table_args__ = (UniqueConstraint("channel_id", "address"),)

    channel_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("alert_channels.id", ondelete="CASCADE"), index=True
    )
    address: Mapped[str] = mapped_column(String(320))  # e-mail or Telegram chat id
    user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    confirmed_at: Mapped[datetime | None]
    confirm_hash: Mapped[bytes | None] = mapped_column(LargeBinary(32), unique=True)
    unsubscribe_hash: Mapped[bytes | None] = mapped_column(LargeBinary(32), unique=True)
    unsubscribed_at: Mapped[datetime | None]


class AlertRule(IdMixin, CreatedAtMixin, TenantMixin, Base):
    __tablename__ = "alert_rules"

    name: Mapped[str] = mapped_column(String(80))
    channel_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("alert_channels.id", ondelete="CASCADE"), index=True
    )
    product_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("products.id", ondelete="CASCADE")
    )  # None: every product
    service_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("services.id", ondelete="CASCADE")
    )  # None: every service on the path
    kinds: Mapped[list[str]] = mapped_column(JSONB, default=list)  # incident | change | risk
    min_severity: Mapped[str] = mapped_column(String(8), default="medium")
    include_non_breaking: Mapped[bool] = mapped_column(default=False)
    enabled: Mapped[bool] = mapped_column(default=True)


class AlertEvent(IdMixin, CreatedAtMixin, TenantMixin, Base):
    """Something upstream reached one product. Unique per product, subject and phase."""

    __tablename__ = "alert_events"
    __table_args__ = (
        UniqueConstraint("org_id", "product_id", "subject_type", "subject_id", "phase"),
        Index("ix_alert_events_org_created", "org_id", "created_at"),
    )

    product_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("products.id", ondelete="CASCADE"))
    subject_type: Mapped[str] = mapped_column(String(16))  # incident | change | risk
    subject_id: Mapped[uuid.UUID]
    phase: Mapped[str] = mapped_column(String(16))  # opened | resolved | new
    severity: Mapped[str] = mapped_column(String(8))
    reason: Mapped[str] = mapped_column(String(64))
    title: Mapped[str] = mapped_column(String(300))
    link: Mapped[str | None] = mapped_column(String(2000))
    path: Mapped[list[str]] = mapped_column(JSONB, default=list)
    detail: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    occurred_at: Mapped[datetime]


class AlertDelivery(IdMixin, CreatedAtMixin, TenantMixin, Base):
    __tablename__ = "alert_deliveries"
    __table_args__ = (
        UniqueConstraint("event_id", "channel_id", "recipient_key"),
        CheckConstraint(_in("status", DeliveryStatus), name="status"),
        Index("ix_alert_deliveries_due", "status", "next_attempt_at"),
    )

    event_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("alert_events.id", ondelete="CASCADE"))
    channel_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("alert_channels.id", ondelete="CASCADE"), index=True
    )
    recipient_key: Mapped[str] = mapped_column(String(64), default="")  # recipient id or ""
    status: Mapped[str] = mapped_column(String(16), default=DeliveryStatus.QUEUED)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime]
    sent_at: Mapped[datetime | None]
    last_error: Mapped[str | None] = mapped_column(String(200))  # never a URL or secret


class JobCursor(Base):
    """How far a platform job has read (event generation, digests)."""

    __tablename__ = "job_cursors"

    name: Mapped[str] = mapped_column(String(64), primary_key=True)
    position: Mapped[datetime]
