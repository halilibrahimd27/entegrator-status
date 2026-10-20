"""Change sources: snapshots of what a source said, the watch state, classified changes.

``org_id`` is copied from the source (or set by the organization for a manual
announcement), so shared sources produce shared changes.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import CheckConstraint, ForeignKey, Index, Integer, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from sentinel.db.base import Base, CreatedAtMixin, IdMixin, SharedOrTenantMixin, utcnow


def _in(column: str, values: type[StrEnum]) -> str:
    return f"{column} IN ({', '.join(repr(v.value) for v in values)})"


class ChangeKind(StrEnum):
    ANNOUNCEMENT = "announcement"  # a feed or page item, or one entered by hand
    VENDOR_INCIDENT = "vendor_incident"  # declared on the vendor's status page
    VENDOR_MAINTENANCE = "vendor_maintenance"


class ChangeCategory(StrEnum):
    MAINTENANCE = "maintenance"
    DEPRECATION = "deprecation"
    SECURITY = "security"
    PACKAGE_UPDATE = "package_update"
    INCIDENT = "incident"
    OTHER = "other"


class Attention(StrEnum):
    BLOCKED_BY_ROBOTS = "blocked_by_robots"  # robots.txt disallows: we do not fetch
    NEEDS_ATTENTION = "needs_attention"  # fetched, but nothing usable came out
    FAILING = "failing"  # five failures in a row


class Snapshot(IdMixin, SharedOrTenantMixin, Base):
    """The normalised state of a change source, stored only when its hash changes."""

    __tablename__ = "snapshots"
    __table_args__ = (Index("ix_snapshots_source_time", "source_id", "taken_at"),)

    source_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("sources.id", ondelete="CASCADE"))
    taken_at: Mapped[datetime]
    hash: Mapped[str] = mapped_column(String(64))
    items: Mapped[list[dict[str, Any]]] = mapped_column(JSONB)
    validators: Mapped[dict[str, str]] = mapped_column(JSONB, default=dict)
    processed_at: Mapped[datetime | None]  # set once the engine has diffed it


class SourceWatch(SharedOrTenantMixin, Base):
    """How a change source is doing; shown next to it so silence is never ambiguous."""

    __tablename__ = "source_watch"
    __table_args__ = (
        CheckConstraint(f"attention IS NULL OR {_in('attention', Attention)}", name="attention"),
    )

    source_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("sources.id", ondelete="CASCADE"), primary_key=True
    )
    last_checked_at: Mapped[datetime]
    last_changed_at: Mapped[datetime | None]
    attention: Mapped[str | None] = mapped_column(String(32))
    reason: Mapped[str | None] = mapped_column(String(200))
    failures: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)


class Change(IdMixin, CreatedAtMixin, SharedOrTenantMixin, Base):
    """One classified difference: a new announcement, a vendor incident or maintenance."""

    __tablename__ = "changes"
    __table_args__ = (
        CheckConstraint(_in("kind", ChangeKind), name="kind"),
        CheckConstraint(_in("category", ChangeCategory), name="category"),
        UniqueConstraint("source_id", "fingerprint"),
        Index("ix_changes_component_detected", "component_id", "detected_at"),
    )

    source_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("sources.id", ondelete="CASCADE")
    )  # NULL for manual announcements
    component_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("components.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(String(32))
    category: Mapped[str] = mapped_column(String(32))
    title: Mapped[str] = mapped_column(String(300))
    link: Mapped[str | None] = mapped_column(String(2000))
    published_at: Mapped[datetime | None]
    effective_at: Mapped[datetime | None]  # e.g. the date a deprecation takes effect
    detected_at: Mapped[datetime]
    fingerprint: Mapped[str] = mapped_column(String(64))
    origin: Mapped[str] = mapped_column(String(16), default="auto")  # auto | manual
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )


class VendorStatus(SharedOrTenantMixin, Base):
    """What a vendor's status page declares now. Shown next to ours, never merged."""

    __tablename__ = "vendor_status"

    source_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("sources.id", ondelete="CASCADE"), primary_key=True
    )
    indicator: Mapped[str] = mapped_column(
        String(20)
    )  # none | minor | major | critical | maintenance
    # our component id -> the vendor's status of the mapped vendor component
    components: Mapped[dict[str, str]] = mapped_column(JSONB, default=dict)
    open_incidents: Mapped[int] = mapped_column(Integer, default=0)
    observed_at: Mapped[datetime]
