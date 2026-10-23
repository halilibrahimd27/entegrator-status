"""Correction requests from providers and the public (Phase 10). Platform-level."""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import CheckConstraint, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from sentinel.db.base import Base, CreatedAtMixin, IdMixin


class CorrectionStatus(StrEnum):
    OPEN = "open"
    ACCEPTED = "accepted"
    REJECTED = "rejected"


class CorrectionRequest(IdMixin, CreatedAtMixin, Base):
    __tablename__ = "correction_requests"
    __table_args__ = (CheckConstraint("status IN ('open', 'accepted', 'rejected')", name="status"),)

    subject: Mapped[str] = mapped_column(String(300))  # the page, service or check concerned
    body: Mapped[str] = mapped_column(Text)
    contact: Mapped[str | None] = mapped_column(String(320))  # optional e-mail for the answer
    status: Mapped[str] = mapped_column(String(16), default=CorrectionStatus.OPEN)
    decided_at: Mapped[datetime | None]
    decided_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    note: Mapped[str | None] = mapped_column(String(2000))  # the answer, sent to the contact
