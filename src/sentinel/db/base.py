"""Declarative base and mixins shared by every model."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sqlalchemy import Column, DateTime, ForeignKey, MetaData, Uuid, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)
    type_annotation_map = {datetime: DateTime(timezone=True)}  # noqa: RUF012


class IdMixin:
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)


class CreatedAtMixin:
    created_at: Mapped[datetime] = mapped_column(default=utcnow, server_default=func.now())


class TenantOwned:
    """Marker: rows of these models belong to one organization (``org_id``).

    The tenant guard filters every query on these models by the session's scope and
    refuses queries and writes without one (see ``sentinel.tenancy.guard``).
    """

    if not TYPE_CHECKING:
        # Placeholder so ``with_loader_criteria(TenantOwned, lambda cls: cls.org_id ...)``
        # can trace its lambda on the marker itself. Never mapped: the concrete mixins
        # below define the real column, and they come first in every model's MRO.
        org_id = Column("org_id", Uuid)


class StrictTenant(TenantOwned):
    """Marker: scoped sessions see only their own organization's rows."""


class TenantMixin(StrictTenant):
    org_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), index=True
    )


class OptionalTenantMixin(StrictTenant):
    """Tenant-owned when ``org_id`` is set; platform-level rows have ``org_id = NULL``.

    Scoped sessions never see the platform-level rows.
    """

    org_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), index=True
    )


class SharedOrTenantMixin(TenantOwned):
    """Shared catalog row when ``org_id`` is NULL, otherwise private to one organization.

    Scoped sessions read their own rows and every shared row, and write only their
    own. Shared rows are written by system sessions (catalog import, platform admin).
    """

    org_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), index=True
    )
