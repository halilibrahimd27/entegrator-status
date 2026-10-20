"""Change sources: snapshots, watch state, classified changes.

Revision ID: 0005_changes
Revises: 0004_status
Create Date: 2026-10-20 11:08:32.173767
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0005_changes"
down_revision: str | Sequence[str] | None = "0004_status"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:

    op.create_table(
        "changes",
        sa.Column("source_id", sa.Uuid(), nullable=True),
        sa.Column("component_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("category", sa.String(length=32), nullable=False),
        sa.Column("title", sa.String(length=300), nullable=False),
        sa.Column("link", sa.String(length=2000), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("effective_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("detected_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("fingerprint", sa.String(length=64), nullable=False),
        sa.Column("origin", sa.String(length=16), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("org_id", sa.Uuid(), nullable=True),
        sa.CheckConstraint(
            "category IN ('maintenance', 'deprecation', 'security', 'package_update', 'incident', 'other')",
            name=op.f("ck_changes_category"),
        ),
        sa.CheckConstraint(
            "kind IN ('announcement', 'vendor_incident', 'vendor_maintenance')",
            name=op.f("ck_changes_kind"),
        ),
        sa.ForeignKeyConstraint(
            ["component_id"],
            ["components.id"],
            name=op.f("fk_changes_component_id_components"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["created_by"],
            ["users.id"],
            name=op.f("fk_changes_created_by_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["org_id"],
            ["organizations.id"],
            name=op.f("fk_changes_org_id_organizations"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["source_id"],
            ["sources.id"],
            name=op.f("fk_changes_source_id_sources"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_changes")),
        sa.UniqueConstraint(
            "source_id", "fingerprint", name=op.f("uq_changes_source_id_fingerprint")
        ),
    )
    op.create_index(
        "ix_changes_component_detected", "changes", ["component_id", "detected_at"], unique=False
    )
    op.create_index(op.f("ix_changes_org_id"), "changes", ["org_id"], unique=False)
    op.create_table(
        "snapshots",
        sa.Column("source_id", sa.Uuid(), nullable=False),
        sa.Column("taken_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("hash", sa.String(length=64), nullable=False),
        sa.Column("items", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("validators", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("org_id", sa.Uuid(), nullable=True),
        sa.ForeignKeyConstraint(
            ["org_id"],
            ["organizations.id"],
            name=op.f("fk_snapshots_org_id_organizations"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["source_id"],
            ["sources.id"],
            name=op.f("fk_snapshots_source_id_sources"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_snapshots")),
    )
    op.create_index(op.f("ix_snapshots_org_id"), "snapshots", ["org_id"], unique=False)
    op.create_index(
        "ix_snapshots_source_time", "snapshots", ["source_id", "taken_at"], unique=False
    )
    op.create_table(
        "source_watch",
        sa.Column("source_id", sa.Uuid(), nullable=False),
        sa.Column("last_checked_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_changed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attention", sa.String(length=32), nullable=True),
        sa.Column("reason", sa.String(length=200), nullable=True),
        sa.Column("failures", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("org_id", sa.Uuid(), nullable=True),
        sa.CheckConstraint(
            "attention IS NULL OR attention IN ('blocked_by_robots', 'needs_attention', 'failing')",
            name=op.f("ck_source_watch_attention"),
        ),
        sa.ForeignKeyConstraint(
            ["org_id"],
            ["organizations.id"],
            name=op.f("fk_source_watch_org_id_organizations"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["source_id"],
            ["sources.id"],
            name=op.f("fk_source_watch_source_id_sources"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("source_id", name=op.f("pk_source_watch")),
    )
    op.create_index(op.f("ix_source_watch_org_id"), "source_watch", ["org_id"], unique=False)


def downgrade() -> None:

    op.drop_index(op.f("ix_source_watch_org_id"), table_name="source_watch")
    op.drop_table("source_watch")
    op.drop_index("ix_snapshots_source_time", table_name="snapshots")
    op.drop_index(op.f("ix_snapshots_org_id"), table_name="snapshots")
    op.drop_table("snapshots")
    op.drop_index(op.f("ix_changes_org_id"), table_name="changes")
    op.drop_index("ix_changes_component_detected", table_name="changes")
    op.drop_table("changes")
