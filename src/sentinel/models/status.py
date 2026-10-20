"""Status: consensus rounds, per-source machines, component timelines, incidents.

Every row copies ``org_id`` from the source or component it describes, so shared
components have shared status and an organization's own components stay private.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import CheckConstraint, Date, ForeignKey, Index, Integer, String, Text
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


def _in(column: str, values: type[StrEnum]) -> str:
    return f"{column} IN ({', '.join(repr(v.value) for v in values)})"


class IncidentKind(StrEnum):
    AUTO = "auto"  # opened and resolved by the engine
    MANUAL = "manual"  # written by an organization for its own product


class IncidentStatus(StrEnum):
    OPEN = "open"
    INVESTIGATING = "investigating"
    IDENTIFIED = "identified"
    MONITORING = "monitoring"
    RESOLVED = "resolved"


MANUAL_STATUSES = (
    IncidentStatus.INVESTIGATING,
    IncidentStatus.IDENTIFIED,
    IncidentStatus.MONITORING,
    IncidentStatus.RESOLVED,
)


class SourceRound(SharedOrTenantMixin, Base):
    """One finished round of one availability source (the "minute bucket")."""

    __tablename__ = "source_rounds"

    source_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("sources.id", ondelete="CASCADE"), primary_key=True
    )
    start: Mapped[datetime] = mapped_column(primary_key=True)
    interval_s: Mapped[int] = mapped_column(Integer)
    verdict: Mapped[str] = mapped_column(String(16))
    state: Mapped[str] = mapped_column(String(16))  # the source's state after this round
    ok_nodes: Mapped[int] = mapped_column(Integer)
    fail_nodes: Mapped[int] = mapped_column(Integer)
    p50_ms: Mapped[int | None]
    p95_ms: Mapped[int | None]
    error_class: Mapped[str | None] = mapped_column(String(32))


class SourceState(SharedOrTenantMixin, Base):
    """The consensus machine of one source between engine runs."""

    __tablename__ = "source_states"

    source_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("sources.id", ondelete="CASCADE"), primary_key=True
    )
    state: Mapped[str] = mapped_column(String(16))
    machine: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    processed_until: Mapped[datetime]  # end of the last finished round
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)


class ComponentState(IdMixin, SharedOrTenantMixin, Base):
    """One interval of a component's consensus timeline; ``ended_at`` NULL = current."""

    __tablename__ = "component_states"
    __table_args__ = (
        Index("ix_component_states_component_time", "component_id", "started_at"),
        Index(
            "uq_component_states_current",
            "component_id",
            unique=True,
            postgresql_where="ended_at IS NULL",
        ),
    )

    component_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("components.id", ondelete="CASCADE"))
    state: Mapped[str] = mapped_column(String(16))
    started_at: Mapped[datetime]
    ended_at: Mapped[datetime | None]
    cause: Mapped[str | None] = mapped_column(String(32))


class Incident(IdMixin, CreatedAtMixin, SharedOrTenantMixin, Base):
    """``auto``: a component was down. ``manual``: an organization's note on its product."""

    __tablename__ = "incidents"
    __table_args__ = (
        CheckConstraint(_in("kind", IncidentKind), name="kind"),
        CheckConstraint(_in("status", IncidentStatus), name="status"),
        CheckConstraint(
            "(kind = 'auto' AND component_id IS NOT NULL AND product_id IS NULL)"
            " OR (kind = 'manual' AND product_id IS NOT NULL AND org_id IS NOT NULL)",
            name="subject",
        ),
        Index("ix_incidents_component_started", "component_id", "started_at"),
    )

    kind: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(16))
    component_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("components.id", ondelete="CASCADE")
    )
    product_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("products.id", ondelete="CASCADE"), index=True
    )
    title: Mapped[str | None] = mapped_column(String(200))
    cause: Mapped[str | None] = mapped_column(String(32))
    started_at: Mapped[datetime]
    resolved_at: Mapped[datetime | None]
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )


class IncidentUpdate(IdMixin, CreatedAtMixin, TenantMixin, Base):
    """A status update on a manual incident."""

    __tablename__ = "incident_updates"

    incident_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("incidents.id", ondelete="CASCADE"), index=True
    )
    status: Mapped[str] = mapped_column(String(16))
    body: Mapped[str] = mapped_column(Text)
    author_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))


class MaintenanceWindow(IdMixin, CreatedAtMixin, SharedOrTenantMixin, Base):
    """Planned work: suppresses incidents and is left out of uptime.

    Organizations declare windows for their own components; windows on shared
    components are platform-level (``org_id`` NULL) and come from platform admins or,
    from Phase 5, from vendor announcements.
    """

    __tablename__ = "maintenance_windows"
    __table_args__ = (
        CheckConstraint("ends_at > starts_at", name="ends_after_start"),
        Index("ix_maintenance_component_time", "component_id", "starts_at"),
    )

    component_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("components.id", ondelete="CASCADE"))
    title: Mapped[str] = mapped_column(String(200))
    starts_at: Mapped[datetime]
    ends_at: Mapped[datetime]
    origin: Mapped[str] = mapped_column(String(16), default="manual")
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )


class ComponentDaily(SharedOrTenantMixin, Base):
    """Seconds per state in one UTC day (kept forever; uptime bars read these)."""

    __tablename__ = "component_daily"

    component_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("components.id", ondelete="CASCADE"), primary_key=True
    )
    day: Mapped[date] = mapped_column(Date, primary_key=True)
    operational_s: Mapped[int] = mapped_column(Integer, default=0)
    degraded_s: Mapped[int] = mapped_column(Integer, default=0)
    unverified_s: Mapped[int] = mapped_column(Integer, default=0)
    down_s: Mapped[int] = mapped_column(Integer, default=0)
    unknown_s: Mapped[int] = mapped_column(Integer, default=0)
    maintenance_s: Mapped[int] = mapped_column(Integer, default=0)


class SourceHourly(SharedOrTenantMixin, Base):
    """Latency percentiles of one source in one hour."""

    __tablename__ = "source_hourly"

    source_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("sources.id", ondelete="CASCADE"), primary_key=True
    )
    hour: Mapped[datetime] = mapped_column(primary_key=True)
    samples: Mapped[int] = mapped_column(Integer)
    p50_ms: Mapped[int | None]
    p95_ms: Mapped[int | None]
