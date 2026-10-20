"""Risks: dated, open conditions on a component. Phase 8 adds per-organization states."""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import CheckConstraint, ForeignKey, Index, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from sentinel.db.base import Base, CreatedAtMixin, IdMixin, SharedOrTenantMixin


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
