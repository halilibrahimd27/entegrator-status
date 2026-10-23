"""Public status pages; the public subscribers' alert channel.

Revision ID: 0013_status_pages
Revises: 0012_alerts
Create Date: 2026-10-23 23:40:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0013_status_pages"
down_revision: str | Sequence[str] | None = "0012_alerts"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "status_pages",
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("title", sa.String(length=120), nullable=False),
        sa.Column("intro", sa.Text(), nullable=True),
        sa.Column("product_ids", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("show_dependencies", sa.Boolean(), nullable=False),
        sa.Column("subscriptions", sa.Boolean(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(
            ["org_id"],
            ["organizations.id"],
            name=op.f("fk_status_pages_org_id_organizations"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_status_pages")),
        sa.UniqueConstraint("org_id", name=op.f("uq_status_pages_org_id")),
    )
    op.create_index(op.f("ix_status_pages_org_id"), "status_pages", ["org_id"], unique=False)
    op.add_column(
        "alert_channels",
        sa.Column("public", sa.Boolean(), server_default="false", nullable=False),
    )


def downgrade() -> None:
    op.drop_column("alert_channels", "public")
    op.drop_index(op.f("ix_status_pages_org_id"), table_name="status_pages")
    op.drop_table("status_pages")
