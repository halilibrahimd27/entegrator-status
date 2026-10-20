"""Probe nodes and observations.

Revision ID: 0003_probe
Revises: 0002_catalog
Create Date: 2026-10-20 09:58:24.363924
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003_probe"
down_revision: str | Sequence[str] | None = "0002_catalog"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "probe_nodes",
        sa.Column("name", sa.String(length=63), nullable=False),
        sa.Column("location", sa.String(length=120), nullable=False),
        sa.Column("token_hash", sa.LargeBinary(length=32), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("healthy", sa.Boolean(), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_probe_nodes")),
        sa.UniqueConstraint("name", name=op.f("uq_probe_nodes_name")),
        sa.UniqueConstraint("token_hash", name=op.f("uq_probe_nodes_token_hash")),
    )
    op.create_table(
        "ingest_batches",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("node_id", sa.Uuid(), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("results", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["node_id"],
            ["probe_nodes.id"],
            name=op.f("fk_ingest_batches_node_id_probe_nodes"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ingest_batches")),
    )
    op.create_index(op.f("ix_ingest_batches_node_id"), "ingest_batches", ["node_id"], unique=False)
    op.create_index(
        op.f("ix_ingest_batches_received_at"), "ingest_batches", ["received_at"], unique=False
    )
    op.create_table(
        "observations",
        sa.Column("source_id", sa.Uuid(), nullable=False),
        sa.Column("node_id", sa.Uuid(), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ok", sa.Boolean(), nullable=False),
        sa.Column("error_class", sa.String(length=32), nullable=True),
        sa.Column("status_code", sa.Integer(), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("node_healthy", sa.Boolean(), nullable=False),
        sa.Column("details", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("org_id", sa.Uuid(), nullable=True),
        sa.ForeignKeyConstraint(
            ["node_id"],
            ["probe_nodes.id"],
            name=op.f("fk_observations_node_id_probe_nodes"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["org_id"],
            ["organizations.id"],
            name=op.f("fk_observations_org_id_organizations"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["source_id"],
            ["sources.id"],
            name=op.f("fk_observations_source_id_sources"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_observations")),
    )
    op.create_index(op.f("ix_observations_node_id"), "observations", ["node_id"], unique=False)
    op.create_index(op.f("ix_observations_org_id"), "observations", ["org_id"], unique=False)
    op.create_index(
        "ix_observations_source_time", "observations", ["source_id", "observed_at"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_observations_source_time", table_name="observations")
    op.drop_index(op.f("ix_observations_org_id"), table_name="observations")
    op.drop_index(op.f("ix_observations_node_id"), table_name="observations")
    op.drop_table("observations")
    op.drop_index(op.f("ix_ingest_batches_received_at"), table_name="ingest_batches")
    op.drop_index(op.f("ix_ingest_batches_node_id"), table_name="ingest_batches")
    op.drop_table("ingest_batches")
    op.drop_table("probe_nodes")
