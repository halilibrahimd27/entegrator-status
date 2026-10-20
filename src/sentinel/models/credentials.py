"""Credentials an organization attaches to its own sources (ADR 0011). Write-only."""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import CheckConstraint, ForeignKey, LargeBinary, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from sentinel.db.base import Base, CreatedAtMixin, IdMixin, TenantMixin, utcnow


class CredentialKind(StrEnum):
    API_KEY_HEADER = "api_key_header"  # a header such as X-Api-Key
    BEARER = "bearer"
    BASIC = "basic"
    OAUTH2_CLIENT = "oauth2_client"  # client credentials grant


SECRET_FIELDS: dict[CredentialKind, tuple[str, ...]] = {
    CredentialKind.API_KEY_HEADER: ("value",),
    CredentialKind.BEARER: ("token",),
    CredentialKind.BASIC: ("username", "password"),
    CredentialKind.OAUTH2_CLIENT: ("client_id", "client_secret"),
}


class OrgDataKey(TenantMixin, Base):
    """The organization's data key, wrapped by the master key."""

    __tablename__ = "org_data_keys"

    org_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), primary_key=True
    )
    wrapped: Mapped[bytes] = mapped_column(LargeBinary)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    rewrapped_at: Mapped[datetime | None]


class Credential(IdMixin, CreatedAtMixin, TenantMixin, Base):
    __tablename__ = "credentials"
    __table_args__ = (
        UniqueConstraint("org_id", "name"),
        CheckConstraint(
            f"kind IN ({', '.join(repr(k.value) for k in CredentialKind)})", name="kind"
        ),
    )

    name: Mapped[str] = mapped_column(String(80))
    kind: Mapped[str] = mapped_column(String(32))
    host: Mapped[str] = mapped_column(String(253))  # the only host it is ever sent to
    header_name: Mapped[str | None] = mapped_column(String(64))  # api_key_header
    token_url: Mapped[str | None] = mapped_column(String(2000))  # oauth2_client
    scope: Mapped[str | None] = mapped_column(String(500))  # oauth2_client, not secret
    sealed: Mapped[bytes] = mapped_column(LargeBinary)
    secret_set_at: Mapped[datetime] = mapped_column(default=utcnow)
    last_used_at: Mapped[datetime | None]
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
