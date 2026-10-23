"""Risks: dated, open conditions on a component. Phase 8 adds per-organization states."""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import CheckConstraint, ForeignKey, Index, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from sentinel.db.base import (
    Base,
    CreatedAtMixin,
    IdMixin,
    SharedOrTenantMixin,
    TenantMixin,
    utcnow,
)


class RiskKind(StrEnum):
    SUNSET = "sunset"  # the provider announced the end of an endpoint (Sunset header)
    DEPRECATION = "deprecation"  # Deprecation header
    CERT_EXPIRY = "cert_expiry"
    DOMAIN_EXPIRY = "domain_expiry"
    EOL = "eol"  # Phase 6
    ADVISORY = "advisory"  # Phase 6
    UNADOPTED_MAJOR = "unadopted_major"  # Phase 6


class Risk(IdMixin, CreatedAtMixin, SharedOrTenantMixin, Base):
    __tablename__ = "risks"
    __table_args__ = (
        CheckConstraint(f"kind IN ({', '.join(repr(k.value) for k in RiskKind)})", name="kind"),
        Index(
            "uq_risks_open_per_subject",
            "source_id",
            "kind",
            "subject",
            unique=True,
            postgresql_where="resolved_at IS NULL",
        ),
        Index("ix_risks_component_due", "component_id", "due_at"),
    )

    source_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("sources.id", ondelete="CASCADE")
    )
    component_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("components.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(String(32))
    # what the risk is about within its source: "" for the source itself, a dependency
    # id for a pinned version, an advisory id for a vulnerability
    subject: Mapped[str] = mapped_column(String(120), default="", server_default="")
    due_at: Mapped[datetime | None]  # None: deprecated without a date
    link: Mapped[str | None] = mapped_column(String(2000))
    detail: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    first_seen_at: Mapped[datetime]
    last_seen_at: Mapped[datetime]
    resolved_at: Mapped[datetime | None]


class RiskDecision(StrEnum):
    SNOOZED = "snoozed"  # hidden until a date
    ACCEPTED = "accepted"  # the organization lives with it, with a note
    RESOLVED = "resolved"  # handled on the organization's side


class RiskState(IdMixin, TenantMixin, Base):
    """One organization's decision about a risk. No row means "open"."""

    __tablename__ = "risk_states"
    __table_args__ = (
        UniqueConstraint("org_id", "risk_id"),
        CheckConstraint(
            f"state IN ({', '.join(repr(d.value) for d in RiskDecision)})", name="state"
        ),
    )

    risk_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("risks.id", ondelete="CASCADE"))
    state: Mapped[str] = mapped_column(String(16))
    until: Mapped[datetime | None]  # snoozed until
    note: Mapped[str | None] = mapped_column(String(500))
    updated_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)
