"""Accounts: users, their sessions and single-use tokens. Not tenant-owned."""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import BigInteger, ForeignKey, LargeBinary, String, false
from sqlalchemy.orm import Mapped, mapped_column

from sentinel.db.base import Base, CreatedAtMixin, IdMixin, utcnow


class User(IdMixin, CreatedAtMixin, Base):
    __tablename__ = "users"

    email: Mapped[str] = mapped_column(String(320), unique=True)  # normalised, lower case
    display_name: Mapped[str] = mapped_column(String(120))
    password_hash: Mapped[str] = mapped_column(String(255))
    password_changed_at: Mapped[datetime] = mapped_column(default=utcnow)
    email_verified_at: Mapped[datetime | None]
    is_platform_admin: Mapped[bool] = mapped_column(default=False, server_default=false())

    # TOTP secret encrypted with TOTP_ENCRYPTION_KEY (AES-GCM); set while enrolling,
    # active once ``totp_enabled_at`` is set. The last accepted time step blocks replay.
    totp_secret: Mapped[bytes | None] = mapped_column(LargeBinary)
    totp_enabled_at: Mapped[datetime | None]
    totp_last_step: Mapped[int | None] = mapped_column(BigInteger)

    failed_logins: Mapped[int] = mapped_column(default=0, server_default="0")
    locked_until: Mapped[datetime | None]
    last_login_at: Mapped[datetime | None]

    privacy_notice_version: Mapped[str] = mapped_column(String(32))
    privacy_notice_seen_at: Mapped[datetime] = mapped_column(default=utcnow)


class UserSession(IdMixin, Base):
    """Server-side session. The cookie holds a random token; only its HMAC is stored."""

    __tablename__ = "user_sessions"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    token_hash: Mapped[bytes] = mapped_column(LargeBinary(32), unique=True)
    mfa_verified: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(default=utcnow)
    expires_at: Mapped[datetime]


class TokenPurpose(StrEnum):
    VERIFY_EMAIL = "verify_email"
    RESET_PASSWORD = "reset_password"  # noqa: S105  # nosec B105 - purpose name, not a secret


class OneTimeToken(IdMixin, CreatedAtMixin, Base):
    __tablename__ = "one_time_tokens"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    purpose: Mapped[str] = mapped_column(String(32))
    token_hash: Mapped[bytes] = mapped_column(LargeBinary(32), unique=True)
    expires_at: Mapped[datetime]
    used_at: Mapped[datetime | None]
