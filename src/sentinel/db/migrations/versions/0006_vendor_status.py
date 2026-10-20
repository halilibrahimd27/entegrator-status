"""Vendor status pages: declared status, announced maintenance ids.

Revision ID: 0006_vendor_status
Revises: 0005_changes
Create Date: 2026-10-20 11:17:17.125377
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0006_vendor_status"
down_revision: str | Sequence[str] | None = "0005_changes"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:

    op.create_table(
        "vendor_status",
        sa.Column("source_id", sa.Uuid(), nullable=False),
        sa.Column("indicator", sa.String(length=20), nullable=False),
        sa.Column("components", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("open_incidents", sa.Integer(), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("org_id", sa.Uuid(), nullable=True),
        sa.ForeignKeyConstraint(
            ["org_id"],
            ["organizations.id"],
            name=op.f("fk_vendor_status_org_id_organizations"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["source_id"],
            ["sources.id"],
            name=op.f("fk_vendor_status_source_id_sources"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("source_id", name=op.f("pk_vendor_status")),
    )
    op.create_index(op.f("ix_vendor_status_org_id"), "vendor_status", ["org_id"], unique=False)
    op.add_column(
        "maintenance_windows", sa.Column("external_id", sa.String(length=120), nullable=True)
    )
    op.create_unique_constraint(
        op.f("uq_maintenance_windows_component_id_external_id"),
        "maintenance_windows",
        ["component_id", "external_id"],
    )


def downgrade() -> None:

    op.drop_constraint(
        op.f("uq_maintenance_windows_component_id_external_id"),
        "maintenance_windows",
        type_="unique",
    )
    op.drop_column("maintenance_windows", "external_id")
    op.drop_index(op.f("ix_vendor_status_org_id"), table_name="vendor_status")
    op.drop_table("vendor_status")
