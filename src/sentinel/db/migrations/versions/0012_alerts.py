"""Alerts: channels, recipients, rules, events, deliveries, job cursors.

Revision ID: 0012_alerts
Revises: 0011_risk_register
Create Date: 2026-10-23 21:54:23.261488
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0012_alerts"
down_revision: str | Sequence[str] | None = "0011_risk_register"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:

    op.create_table(
        "job_cursors",
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("position", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("name", name=op.f("pk_job_cursors")),
    )
    op.create_table(
        "alert_channels",
        sa.Column("name", sa.String(length=80), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("target_host", sa.String(length=253), nullable=True),
        sa.Column("sealed", sa.LargeBinary(), nullable=True),
        sa.Column("quiet_start", sa.String(length=5), nullable=True),
        sa.Column("quiet_end", sa.String(length=5), nullable=True),
        sa.Column("quiet_bypass_high", sa.Boolean(), nullable=False),
        sa.Column("daily", sa.Boolean(), nullable=False),
        sa.Column("weekly_digest", sa.Boolean(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("disabled_reason", sa.String(length=64), nullable=True),
        sa.Column("failures", sa.Integer(), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(
            "kind IN ('email', 'slack', 'webhook', 'telegram')", name=op.f("ck_alert_channels_kind")
        ),
        sa.ForeignKeyConstraint(
            ["created_by"],
            ["users.id"],
            name=op.f("fk_alert_channels_created_by_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["org_id"],
            ["organizations.id"],
            name=op.f("fk_alert_channels_org_id_organizations"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_alert_channels")),
        sa.UniqueConstraint("org_id", "name", name=op.f("uq_alert_channels_org_id_name")),
    )
    op.create_index(op.f("ix_alert_channels_org_id"), "alert_channels", ["org_id"], unique=False)
    op.create_table(
        "alert_events",
        sa.Column("product_id", sa.Uuid(), nullable=False),
        sa.Column("subject_type", sa.String(length=16), nullable=False),
        sa.Column("subject_id", sa.Uuid(), nullable=False),
        sa.Column("phase", sa.String(length=16), nullable=False),
        sa.Column("severity", sa.String(length=8), nullable=False),
        sa.Column("reason", sa.String(length=64), nullable=False),
        sa.Column("title", sa.String(length=300), nullable=False),
        sa.Column("link", sa.String(length=2000), nullable=True),
        sa.Column("path", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("detail", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
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
            name=op.f("fk_alert_events_org_id_organizations"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["product_id"],
            ["products.id"],
            name=op.f("fk_alert_events_product_id_products"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_alert_events")),
        sa.UniqueConstraint(
            "org_id",
            "product_id",
            "subject_type",
            "subject_id",
            "phase",
            name=op.f("uq_alert_events_org_id_product_id_subject_type_subject_id_phase"),
        ),
    )
    op.create_index(
        "ix_alert_events_org_created", "alert_events", ["org_id", "created_at"], unique=False
    )
    op.create_index(op.f("ix_alert_events_org_id"), "alert_events", ["org_id"], unique=False)
    op.create_table(
        "alert_recipients",
        sa.Column("channel_id", sa.Uuid(), nullable=False),
        sa.Column("address", sa.String(length=320), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=True),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("confirm_hash", sa.LargeBinary(length=32), nullable=True),
        sa.Column("unsubscribed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(
            ["channel_id"],
            ["alert_channels.id"],
            name=op.f("fk_alert_recipients_channel_id_alert_channels"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["org_id"],
            ["organizations.id"],
            name=op.f("fk_alert_recipients_org_id_organizations"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_alert_recipients_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_alert_recipients")),
        sa.UniqueConstraint(
            "channel_id", "address", name=op.f("uq_alert_recipients_channel_id_address")
        ),
        sa.UniqueConstraint("confirm_hash", name=op.f("uq_alert_recipients_confirm_hash")),
    )
    op.create_index(
        op.f("ix_alert_recipients_channel_id"), "alert_recipients", ["channel_id"], unique=False
    )
    op.create_index(
        op.f("ix_alert_recipients_org_id"), "alert_recipients", ["org_id"], unique=False
    )
    op.create_table(
        "alert_rules",
        sa.Column("name", sa.String(length=80), nullable=False),
        sa.Column("channel_id", sa.Uuid(), nullable=False),
        sa.Column("product_id", sa.Uuid(), nullable=True),
        sa.Column("service_id", sa.Uuid(), nullable=True),
        sa.Column("kinds", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("min_severity", sa.String(length=8), nullable=False),
        sa.Column("include_non_breaking", sa.Boolean(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(
            ["channel_id"],
            ["alert_channels.id"],
            name=op.f("fk_alert_rules_channel_id_alert_channels"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["org_id"],
            ["organizations.id"],
            name=op.f("fk_alert_rules_org_id_organizations"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["product_id"],
            ["products.id"],
            name=op.f("fk_alert_rules_product_id_products"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["service_id"],
            ["services.id"],
            name=op.f("fk_alert_rules_service_id_services"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_alert_rules")),
    )
    op.create_index(op.f("ix_alert_rules_channel_id"), "alert_rules", ["channel_id"], unique=False)
    op.create_index(op.f("ix_alert_rules_org_id"), "alert_rules", ["org_id"], unique=False)
    op.create_table(
        "alert_deliveries",
        sa.Column("event_id", sa.Uuid(), nullable=False),
        sa.Column("channel_id", sa.Uuid(), nullable=False),
        sa.Column("recipient_key", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.String(length=200), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(
            "status IN ('queued', 'sent', 'failed', 'skipped')",
            name=op.f("ck_alert_deliveries_status"),
        ),
        sa.ForeignKeyConstraint(
            ["channel_id"],
            ["alert_channels.id"],
            name=op.f("fk_alert_deliveries_channel_id_alert_channels"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["event_id"],
            ["alert_events.id"],
            name=op.f("fk_alert_deliveries_event_id_alert_events"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["org_id"],
            ["organizations.id"],
            name=op.f("fk_alert_deliveries_org_id_organizations"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_alert_deliveries")),
        sa.UniqueConstraint(
            "event_id",
            "channel_id",
            "recipient_key",
            name=op.f("uq_alert_deliveries_event_id_channel_id_recipient_key"),
        ),
    )
    op.create_index(
        op.f("ix_alert_deliveries_channel_id"), "alert_deliveries", ["channel_id"], unique=False
    )
    op.create_index(
        "ix_alert_deliveries_due", "alert_deliveries", ["status", "next_attempt_at"], unique=False
    )
    op.create_index(
        op.f("ix_alert_deliveries_org_id"), "alert_deliveries", ["org_id"], unique=False
    )


def downgrade() -> None:

    op.drop_index(op.f("ix_alert_deliveries_org_id"), table_name="alert_deliveries")
    op.drop_index("ix_alert_deliveries_due", table_name="alert_deliveries")
    op.drop_index(op.f("ix_alert_deliveries_channel_id"), table_name="alert_deliveries")
    op.drop_table("alert_deliveries")
    op.drop_index(op.f("ix_alert_rules_org_id"), table_name="alert_rules")
    op.drop_index(op.f("ix_alert_rules_channel_id"), table_name="alert_rules")
    op.drop_table("alert_rules")
    op.drop_index(op.f("ix_alert_recipients_org_id"), table_name="alert_recipients")
    op.drop_index(op.f("ix_alert_recipients_channel_id"), table_name="alert_recipients")
    op.drop_table("alert_recipients")
    op.drop_index(op.f("ix_alert_events_org_id"), table_name="alert_events")
    op.drop_index("ix_alert_events_org_created", table_name="alert_events")
    op.drop_table("alert_events")
    op.drop_index(op.f("ix_alert_channels_org_id"), table_name="alert_channels")
    op.drop_table("alert_channels")
    op.drop_table("job_cursors")
