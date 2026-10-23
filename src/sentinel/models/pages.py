"""An organization's public status page (Phase 10): what its own customers may see."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from sentinel.db.base import Base, CreatedAtMixin, IdMixin, TenantMixin


class StatusPage(IdMixin, CreatedAtMixin, TenantMixin, Base):
    """Off until the organization switches it on; shows only the products it picks."""

    __tablename__ = "status_pages"
    __table_args__ = (UniqueConstraint("org_id"),)

    enabled: Mapped[bool] = mapped_column(default=False)
    title: Mapped[str] = mapped_column(String(120))
    intro: Mapped[str | None] = mapped_column(Text)
    product_ids: Mapped[list[str]] = mapped_column(JSONB, default=list)
    show_dependencies: Mapped[bool] = mapped_column(default=False)
    subscriptions: Mapped[bool] = mapped_column(default=True)  # e-mail, double opt-in
    updated_at: Mapped[datetime | None]
