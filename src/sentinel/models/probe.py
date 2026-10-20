"""Probe nodes, the batches they ship, and the observations those batches carry."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import ForeignKey, Index, Integer, LargeBinary, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from sentinel.db.base import Base, CreatedAtMixin, IdMixin, SharedOrTenantMixin, utcnow


class ProbeNode(IdMixin, CreatedAtMixin, Base):
    """A machine that runs the prober. Platform-level; its token is stored as an HMAC."""

    __tablename__ = "probe_nodes"

    name: Mapped[str] = mapped_column(String(63), unique=True)
    location: Mapped[str] = mapped_column(String(120))
    token_hash: Mapped[bytes] = mapped_column(LargeBinary(32), unique=True)
    enabled: Mapped[bool] = mapped_column(default=True)
    last_seen_at: Mapped[datetime | None]
    healthy: Mapped[bool | None]
    # may receive jobs of authenticated sources (ADR 0011); off unless registered so
    handles_credentials: Mapped[bool] = mapped_column(default=False, server_default="false")


class IngestBatch(Base):
    """Seen batch ids (replay protection). Pruned with observations."""

    __tablename__ = "ingest_batches"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    node_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("probe_nodes.id", ondelete="CASCADE"), index=True
    )
    received_at: Mapped[datetime] = mapped_column(default=utcnow, index=True)
    results: Mapped[int] = mapped_column(Integer)


class Observation(IdMixin, SharedOrTenantMixin, Base):
    """One measurement of one source by one node.

    ``org_id`` is copied from the source: observations of shared sources are shared,
    observations of an organization's own sources are private to it.
    """

    __tablename__ = "observations"
    __table_args__ = (Index("ix_observations_source_time", "source_id", "observed_at"),)

    source_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("sources.id", ondelete="CASCADE"))
    node_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("probe_nodes.id", ondelete="CASCADE"), index=True
    )
    observed_at: Mapped[datetime]
    ok: Mapped[bool]
    error_class: Mapped[str | None] = mapped_column(String(32))
    status_code: Mapped[int | None]
    latency_ms: Mapped[int | None]
    node_healthy: Mapped[bool] = mapped_column(default=True)
    details: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
