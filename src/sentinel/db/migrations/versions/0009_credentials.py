"""Credential vault: data keys, credentials, source and node links.

Revision ID: 0009_credentials
Revises: 0008_releases
Create Date: 2026-10-20 16:40:04.862196
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009_credentials"
down_revision: str | Sequence[str] | None = "0008_releases"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:

    op.create_table(
        "credentials",
        sa.Column("name", sa.String(length=80), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("host", sa.String(length=253), nullable=False),
        sa.Column("header_name", sa.String(length=64), nullable=True),
        sa.Column("token_url", sa.String(length=2000), nullable=True),
        sa.Column("scope", sa.String(length=500), nullable=True),
        sa.Column("sealed", sa.LargeBinary(), nullable=False),
        sa.Column("secret_set_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
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
            "kind IN ('api_key_header', 'bearer', 'basic', 'oauth2_client')",
            name=op.f("ck_credentials_kind"),
        ),
        sa.ForeignKeyConstraint(
            ["created_by"],
            ["users.id"],
            name=op.f("fk_credentials_created_by_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["org_id"],
            ["organizations.id"],
            name=op.f("fk_credentials_org_id_organizations"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_credentials")),
        sa.UniqueConstraint("org_id", "name", name=op.f("uq_credentials_org_id_name")),
    )
    op.create_index(op.f("ix_credentials_org_id"), "credentials", ["org_id"], unique=False)
    op.create_table(
        "org_data_keys",
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("wrapped", sa.LargeBinary(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("rewrapped_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["org_id"],
            ["organizations.id"],
            name=op.f("fk_org_data_keys_org_id_organizations"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("org_id", name=op.f("pk_org_data_keys")),
    )
    op.add_column(
        "probe_nodes",
        sa.Column("handles_credentials", sa.Boolean(), server_default="false", nullable=False),
    )
    op.add_column("sources", sa.Column("credential_id", sa.Uuid(), nullable=True))
    op.create_index(op.f("ix_sources_credential_id"), "sources", ["credential_id"], unique=False)
    op.create_foreign_key(
        op.f("fk_sources_credential_id_credentials"),
        "sources",
        "credentials",
        ["credential_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:

    op.drop_constraint(op.f("fk_sources_credential_id_credentials"), "sources", type_="foreignkey")
    op.drop_index(op.f("ix_sources_credential_id"), table_name="sources")
    op.drop_column("sources", "credential_id")
    op.drop_column("probe_nodes", "handles_credentials")
    op.drop_table("org_data_keys")
    op.drop_index(op.f("ix_credentials_org_id"), table_name="credentials")
    op.drop_table("credentials")
