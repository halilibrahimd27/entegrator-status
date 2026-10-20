"""Risks: sunsets, deprecations, expiring certificates and domains.

Revision ID: 0007_risks
Revises: 0006_vendor_status
Create Date: 2026-10-20 11:27:41.205025
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0007_risks"
down_revision: str | Sequence[str] | None = "0006_vendor_status"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:

    op.create_table(
        "risks",
        sa.Column("source_id", sa.Uuid(), nullable=True),
        sa.Column("component_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("link", sa.String(length=2000), nullable=True),
        sa.Column("detail", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("org_id", sa.Uuid(), nullable=True),
        sa.CheckConstraint(
            "kind IN ('sunset', 'deprecation', 'cert_expiry', 'domain_expiry', 'eol', 'advisory', 'unadopted_major')",
            name=op.f("ck_risks_kind"),
        ),
        sa.ForeignKeyConstraint(
            ["component_id"],
            ["components.id"],
            name=op.f("fk_risks_component_id_components"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["org_id"],
            ["organizations.id"],
            name=op.f("fk_risks_org_id_organizations"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["source_id"],
            ["sources.id"],
            name=op.f("fk_risks_source_id_sources"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_risks")),
    )
    op.create_index("ix_risks_component_due", "risks", ["component_id", "due_at"], unique=False)
    op.create_index(op.f("ix_risks_org_id"), "risks", ["org_id"], unique=False)
    op.create_index(
        "uq_risks_open_per_source",
        "risks",
        ["source_id", "kind"],
        unique=True,
        postgresql_where="resolved_at IS NULL",
    )


def downgrade() -> None:

    op.drop_index(
        "uq_risks_open_per_source", table_name="risks", postgresql_where="resolved_at IS NULL"
    )
    op.drop_index(op.f("ix_risks_org_id"), table_name="risks")
    op.drop_index("ix_risks_component_due", table_name="risks")
    op.drop_table("risks")
