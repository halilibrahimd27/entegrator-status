"""Audit log. Org events are tenant-owned; sign-in events are platform-level (org NULL)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import ForeignKey, Index, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from sentinel.db.base import Base, IdMixin, OptionalTenantMixin, utcnow


class AuditEvent(IdMixin, OptionalTenantMixin, Base):
    __tablename__ = "audit_events"
    __table_args__ = (Index("ix_audit_events_org_time", "org_id", "occurred_at"),)

    occurred_at: Mapped[datetime] = mapped_column(default=utcnow)
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    action: Mapped[str] = mapped_column(String(64))
    target_type: Mapped[str | None] = mapped_column(String(32))
    target_id: Mapped[str | None] = mapped_column(String(64))
    # Never secrets, tokens or full IPs: callers pass only what reviewers need.
    details: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    ip_prefix: Mapped[str | None] = mapped_column(String(64))  # /24 (IPv4) or /48 (IPv6)
