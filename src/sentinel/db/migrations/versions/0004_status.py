"""Status engine: rounds, timelines, incidents, maintenance, rollups.

Revision ID: 0004_status
Revises: 0003_probe
Create Date: 2026-10-20 10:36:41.237835
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004_status"
down_revision: str | Sequence[str] | None = "0003_probe"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:

    op.create_table(
        "component_daily",
        sa.Column("component_id", sa.Uuid(), nullable=False),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("operational_s", sa.Integer(), nullable=False),
        sa.Column("degraded_s", sa.Integer(), nullable=False),
        sa.Column("unverified_s", sa.Integer(), nullable=False),
        sa.Column("down_s", sa.Integer(), nullable=False),
        sa.Column("unknown_s", sa.Integer(), nullable=False),
        sa.Column("maintenance_s", sa.Integer(), nullable=False),
        sa.Column("org_id", sa.Uuid(), nullable=True),
        sa.ForeignKeyConstraint(
            ["component_id"],
            ["components.id"],
            name=op.f("fk_component_daily_component_id_components"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["org_id"],
            ["organizations.id"],
            name=op.f("fk_component_daily_org_id_organizations"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("component_id", "day", name=op.f("pk_component_daily")),
    )
    op.create_index(op.f("ix_component_daily_org_id"), "component_daily", ["org_id"], unique=False)
    op.create_table(
        "component_states",
        sa.Column("component_id", sa.Uuid(), nullable=False),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cause", sa.String(length=32), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("org_id", sa.Uuid(), nullable=True),
        sa.ForeignKeyConstraint(
            ["component_id"],
            ["components.id"],
            name=op.f("fk_component_states_component_id_components"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["org_id"],
            ["organizations.id"],
            name=op.f("fk_component_states_org_id_organizations"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_component_states")),
    )
    op.create_index(
        "ix_component_states_component_time",
        "component_states",
        ["component_id", "started_at"],
        unique=False,
    )
    op.create_index(
        op.f("ix_component_states_org_id"), "component_states", ["org_id"], unique=False
    )
    op.create_index(
        "uq_component_states_current",
        "component_states",
        ["component_id"],
        unique=True,
        postgresql_where="ended_at IS NULL",
    )
    op.create_table(
        "incidents",
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("component_id", sa.Uuid(), nullable=True),
        sa.Column("product_id", sa.Uuid(), nullable=True),
        sa.Column("title", sa.String(length=200), nullable=True),
        sa.Column("cause", sa.String(length=32), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
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
            "(kind = 'auto' AND component_id IS NOT NULL AND product_id IS NULL) OR (kind = 'manual' AND product_id IS NOT NULL AND org_id IS NOT NULL)",
            name=op.f("ck_incidents_subject"),
        ),
        sa.CheckConstraint("kind IN ('auto', 'manual')", name=op.f("ck_incidents_kind")),
        sa.CheckConstraint(
            "status IN ('open', 'investigating', 'identified', 'monitoring', 'resolved')",
            name=op.f("ck_incidents_status"),
        ),
        sa.ForeignKeyConstraint(
            ["component_id"],
            ["components.id"],
            name=op.f("fk_incidents_component_id_components"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["created_by"],
            ["users.id"],
            name=op.f("fk_incidents_created_by_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["org_id"],
            ["organizations.id"],
            name=op.f("fk_incidents_org_id_organizations"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["product_id"],
            ["products.id"],
            name=op.f("fk_incidents_product_id_products"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_incidents")),
    )
    op.create_index(
        "ix_incidents_component_started", "incidents", ["component_id", "started_at"], unique=False
    )
    op.create_index(op.f("ix_incidents_org_id"), "incidents", ["org_id"], unique=False)
    op.create_index(op.f("ix_incidents_product_id"), "incidents", ["product_id"], unique=False)
    op.create_table(
        "maintenance_windows",
        sa.Column("component_id", sa.Uuid(), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=False),
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
            "ends_at > starts_at", name=op.f("ck_maintenance_windows_ends_after_start")
        ),
        sa.ForeignKeyConstraint(
            ["component_id"],
            ["components.id"],
            name=op.f("fk_maintenance_windows_component_id_components"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["created_by"],
            ["users.id"],
            name=op.f("fk_maintenance_windows_created_by_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["org_id"],
            ["organizations.id"],
            name=op.f("fk_maintenance_windows_org_id_organizations"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_maintenance_windows")),
    )
    op.create_index(
        "ix_maintenance_component_time",
        "maintenance_windows",
        ["component_id", "starts_at"],
        unique=False,
    )
    op.create_index(
        op.f("ix_maintenance_windows_org_id"), "maintenance_windows", ["org_id"], unique=False
    )
    op.create_table(
        "incident_updates",
        sa.Column("incident_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("author_id", sa.Uuid(), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(
            ["author_id"],
            ["users.id"],
            name=op.f("fk_incident_updates_author_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["incident_id"],
            ["incidents.id"],
            name=op.f("fk_incident_updates_incident_id_incidents"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["org_id"],
            ["organizations.id"],
            name=op.f("fk_incident_updates_org_id_organizations"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_incident_updates")),
    )
    op.create_index(
        op.f("ix_incident_updates_incident_id"), "incident_updates", ["incident_id"], unique=False
    )
    op.create_index(
        op.f("ix_incident_updates_org_id"), "incident_updates", ["org_id"], unique=False
    )
    op.create_table(
        "source_hourly",
        sa.Column("source_id", sa.Uuid(), nullable=False),
        sa.Column("hour", sa.DateTime(timezone=True), nullable=False),
        sa.Column("samples", sa.Integer(), nullable=False),
        sa.Column("p50_ms", sa.Integer(), nullable=True),
        sa.Column("p95_ms", sa.Integer(), nullable=True),
        sa.Column("org_id", sa.Uuid(), nullable=True),
        sa.ForeignKeyConstraint(
            ["org_id"],
            ["organizations.id"],
            name=op.f("fk_source_hourly_org_id_organizations"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["source_id"],
            ["sources.id"],
            name=op.f("fk_source_hourly_source_id_sources"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("source_id", "hour", name=op.f("pk_source_hourly")),
    )
    op.create_index(op.f("ix_source_hourly_org_id"), "source_hourly", ["org_id"], unique=False)
    op.create_table(
        "source_rounds",
        sa.Column("source_id", sa.Uuid(), nullable=False),
        sa.Column("start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("interval_s", sa.Integer(), nullable=False),
        sa.Column("verdict", sa.String(length=16), nullable=False),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("ok_nodes", sa.Integer(), nullable=False),
        sa.Column("fail_nodes", sa.Integer(), nullable=False),
        sa.Column("p50_ms", sa.Integer(), nullable=True),
        sa.Column("p95_ms", sa.Integer(), nullable=True),
        sa.Column("error_class", sa.String(length=32), nullable=True),
        sa.Column("org_id", sa.Uuid(), nullable=True),
        sa.ForeignKeyConstraint(
            ["org_id"],
            ["organizations.id"],
            name=op.f("fk_source_rounds_org_id_organizations"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["source_id"],
            ["sources.id"],
            name=op.f("fk_source_rounds_source_id_sources"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("source_id", "start", name=op.f("pk_source_rounds")),
    )
    op.create_index(op.f("ix_source_rounds_org_id"), "source_rounds", ["org_id"], unique=False)
    op.create_table(
        "source_states",
        sa.Column("source_id", sa.Uuid(), nullable=False),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("machine", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("processed_until", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("org_id", sa.Uuid(), nullable=True),
        sa.ForeignKeyConstraint(
            ["org_id"],
            ["organizations.id"],
            name=op.f("fk_source_states_org_id_organizations"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["source_id"],
            ["sources.id"],
            name=op.f("fk_source_states_source_id_sources"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("source_id", name=op.f("pk_source_states")),
    )
    op.create_index(op.f("ix_source_states_org_id"), "source_states", ["org_id"], unique=False)


def downgrade() -> None:

    op.drop_index(op.f("ix_source_states_org_id"), table_name="source_states")
    op.drop_table("source_states")
    op.drop_index(op.f("ix_source_rounds_org_id"), table_name="source_rounds")
    op.drop_table("source_rounds")
    op.drop_index(op.f("ix_source_hourly_org_id"), table_name="source_hourly")
    op.drop_table("source_hourly")
    op.drop_index(op.f("ix_incident_updates_org_id"), table_name="incident_updates")
    op.drop_index(op.f("ix_incident_updates_incident_id"), table_name="incident_updates")
    op.drop_table("incident_updates")
    op.drop_index(op.f("ix_maintenance_windows_org_id"), table_name="maintenance_windows")
    op.drop_index("ix_maintenance_component_time", table_name="maintenance_windows")
    op.drop_table("maintenance_windows")
    op.drop_index(op.f("ix_incidents_product_id"), table_name="incidents")
    op.drop_index(op.f("ix_incidents_org_id"), table_name="incidents")
    op.drop_index("ix_incidents_component_started", table_name="incidents")
    op.drop_table("incidents")
    op.drop_index(
        "uq_component_states_current",
        table_name="component_states",
        postgresql_where="ended_at IS NULL",
    )
    op.drop_index(op.f("ix_component_states_org_id"), table_name="component_states")
    op.drop_index("ix_component_states_component_time", table_name="component_states")
    op.drop_table("component_states")
    op.drop_index(op.f("ix_component_daily_org_id"), table_name="component_daily")
    op.drop_table("component_daily")
