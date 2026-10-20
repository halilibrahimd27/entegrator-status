"""The dependency graph: products, services, components, sources and edges.

Services, components, sources and edges are *shared* (``org_id`` NULL, curated catalog,
readable by every organization) or *private* to one organization. A private row may
hang under a shared parent (an organization's own check on a shared vendor API), never
the other way round.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import CheckConstraint, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from sentinel.db.base import Base, CreatedAtMixin, IdMixin, SharedOrTenantMixin, TenantMixin


class ServiceKind(StrEnum):
    SAAS_API = "saas_api"
    INTEGRATOR = "integrator"
    AUTHORITY = "authority"
    CLOUD = "cloud"
    PACKAGE = "package"
    CONTAINER_IMAGE = "container_image"
    DOMAIN = "domain"
    WEBSITE = "website"
    INTERNAL = "internal"
    OTHER = "other"


class SourceKind(StrEnum):
    HTTP = "http"
    TLS_CERT = "tls_cert"
    DNS = "dns"
    DOMAIN_EXPIRY = "domain_expiry"
    STATUS_PAGE = "status_page"
    API_CONTRACT = "api_contract"
    RELEASE = "release"
    FEED = "feed"
    PAGE_WATCH = "page_watch"
    ADVISORY = "advisory"
    EOL = "eol"


def _in(column: str, values: type[StrEnum]) -> str:
    return f"{column} in ({', '.join(repr(v.value) for v in values)})"


class Product(IdMixin, CreatedAtMixin, TenantMixin, Base):
    """An organization's own system; the root of its graph."""

    __tablename__ = "products"
    __table_args__ = (UniqueConstraint("org_id", "slug"),)

    slug: Mapped[str] = mapped_column(String(63))
    name: Mapped[str] = mapped_column(String(120))
    description: Mapped[str | None] = mapped_column(Text)


class Service(IdMixin, CreatedAtMixin, SharedOrTenantMixin, Base):
    __tablename__ = "services"
    __table_args__ = (
        UniqueConstraint("org_id", "slug", postgresql_nulls_not_distinct=True),
        CheckConstraint(_in("kind", ServiceKind), name="kind"),
    )

    slug: Mapped[str] = mapped_column(String(63))
    name: Mapped[str] = mapped_column(String(120))
    kind: Mapped[str] = mapped_column(String(32))
    homepage: Mapped[str | None] = mapped_column(String(500))
    description: Mapped[str | None] = mapped_column(Text)
    tags: Mapped[list[str]] = mapped_column(JSONB, default=list)
    purl: Mapped[str | None] = mapped_column(String(255), index=True)  # packages: pkg:npm/react
    pack: Mapped[str | None] = mapped_column(String(63))  # catalog pack of a shared row


class Component(IdMixin, CreatedAtMixin, SharedOrTenantMixin, Base):
    __tablename__ = "components"
    __table_args__ = (UniqueConstraint("service_id", "slug"),)

    service_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("services.id", ondelete="CASCADE"), index=True
    )
    slug: Mapped[str] = mapped_column(String(63))
    name: Mapped[str] = mapped_column(String(120))
    description: Mapped[str | None] = mapped_column(Text)
    tags: Mapped[list[str]] = mapped_column(JSONB, default=list)


class Source(IdMixin, CreatedAtMixin, SharedOrTenantMixin, Base):
    """How a component is observed. ``config`` is validated per kind (sentinel.sources)."""

    __tablename__ = "sources"
    __table_args__ = (CheckConstraint(_in("kind", SourceKind), name="kind"),)

    component_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("components.id", ondelete="CASCADE"), index=True
    )
    kind: Mapped[str] = mapped_column(String(32))
    identity: Mapped[str] = mapped_column(String(500))  # stable key within a component
    config: Mapped[dict[str, Any]] = mapped_column(JSONB)
    interval_s: Mapped[int] = mapped_column(Integer)
    timeout_s: Mapped[int] = mapped_column(Integer)
    allow_http: Mapped[bool] = mapped_column(default=False)
    availability_ignores_robots: Mapped[bool] = mapped_column(default=False)
    enabled: Mapped[bool] = mapped_column(default=True)
    provenance: Mapped[str | None] = mapped_column(String(500))  # where the URL came from
    # the organization's own credential (private sources only, ADR 0011)
    credential_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("credentials.id", ondelete="SET NULL"), index=True
    )


class Dependency(IdMixin, CreatedAtMixin, SharedOrTenantMixin, Base):
    """Directed edge: (product | component) depends on component."""

    __tablename__ = "dependencies"
    __table_args__ = (
        CheckConstraint(
            "(from_product_id IS NULL) <> (from_component_id IS NULL)", name="one_origin"
        ),
        CheckConstraint(
            "from_component_id IS NULL OR from_component_id <> to_component_id", name="no_self_edge"
        ),
        UniqueConstraint(
            "org_id",
            "from_product_id",
            "from_component_id",
            "to_component_id",
            postgresql_nulls_not_distinct=True,
        ),
    )

    from_product_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("products.id", ondelete="CASCADE"), index=True
    )
    from_component_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("components.id", ondelete="CASCADE"), index=True
    )
    to_component_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("components.id", ondelete="CASCADE"), index=True
    )
    # Pinned version, operations used: makes impact usage-aware (Phase 8).
    usage: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    note: Mapped[str | None] = mapped_column(String(500))


class CatalogTagRule(IdMixin, CreatedAtMixin, Base):
    """Platform rule: every component tagged ``tag`` depends on ``to_component_id``."""

    __tablename__ = "catalog_tag_rules"
    __table_args__ = (UniqueConstraint("tag", "to_component_id"),)

    tag: Mapped[str] = mapped_column(String(63))
    to_component_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("components.id", ondelete="CASCADE")
    )
    pack: Mapped[str | None] = mapped_column(String(63))


class ProposalStatus(StrEnum):
    PENDING = "pending"
    ACCEPTED = "accepted"
    REJECTED = "rejected"


class CatalogProposal(IdMixin, CreatedAtMixin, TenantMixin, Base):
    """An organization proposes one of its private services for the shared catalog."""

    __tablename__ = "catalog_proposals"
    __table_args__ = (CheckConstraint(_in("status", ProposalStatus), name="status"),)

    service_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("services.id", ondelete="CASCADE"), index=True
    )
    status: Mapped[str] = mapped_column(String(16), default=ProposalStatus.PENDING.value)
    note: Mapped[str | None] = mapped_column(Text)
    proposed_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    reviewed_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    reviewed_at: Mapped[datetime | None]
    review_note: Mapped[str | None] = mapped_column(Text)
    shared_service_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("services.id", ondelete="SET NULL")
    )
