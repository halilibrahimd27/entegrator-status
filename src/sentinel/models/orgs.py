"""Organizations (the tenants) and the rows that belong to them."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, ForeignKey, LargeBinary, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from sentinel.db.base import Base, CreatedAtMixin, IdMixin, TenantMixin

_ROLE_CHECK = "role in ('owner', 'admin', 'member', 'viewer')"


class Organization(IdMixin, CreatedAtMixin, Base):
    """The tenant itself. Accessed through memberships, never listed across tenants."""

    __tablename__ = "organizations"

    slug: Mapped[str] = mapped_column(String(63), unique=True)
    name: Mapped[str] = mapped_column(String(120))
    deleted_at: Mapped[datetime | None]  # soft delete; purged after ``purge_after``
    purge_after: Mapped[datetime | None]
    max_sources: Mapped[int | None]  # set by a platform admin; None: ORG_MAX_SOURCES


class Membership(IdMixin, CreatedAtMixin, TenantMixin, Base):
    __tablename__ = "memberships"
    __table_args__ = (
        UniqueConstraint("org_id", "user_id"),
        CheckConstraint(_ROLE_CHECK, name="role"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    role: Mapped[str] = mapped_column(String(16))


class Invitation(IdMixin, CreatedAtMixin, TenantMixin, Base):
    __tablename__ = "invitations"
    __table_args__ = (CheckConstraint(_ROLE_CHECK, name="role"),)

    email: Mapped[str] = mapped_column(String(320))
    role: Mapped[str] = mapped_column(String(16))
    token_hash: Mapped[bytes] = mapped_column(LargeBinary(32), unique=True)
    invited_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    expires_at: Mapped[datetime]
    accepted_at: Mapped[datetime | None]
    revoked_at: Mapped[datetime | None]


class ApiToken(IdMixin, CreatedAtMixin, TenantMixin, Base):
    """Org API token. The secret is shown once; only its HMAC is stored."""

    __tablename__ = "api_tokens"

    name: Mapped[str] = mapped_column(String(80))
    prefix: Mapped[str] = mapped_column(String(16), unique=True)  # public lookup id
    token_hash: Mapped[bytes] = mapped_column(LargeBinary(32), unique=True)
    scopes: Mapped[list[str]] = mapped_column(JSONB)
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    expires_at: Mapped[datetime]
    last_used_at: Mapped[datetime | None]
    revoked_at: Mapped[datetime | None]
