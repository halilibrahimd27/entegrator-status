"""Risk register: per-organization decisions about risks.

Revision ID: 0011_risk_register
Revises: 0010_contracts
Create Date: 2026-10-23 21:40:33.006062
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0011_risk_register"
down_revision: str | Sequence[str] | None = "0010_contracts"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:

    op.create_table(
        "risk_states",
        sa.Column("risk_id", sa.Uuid(), nullable=False),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("note", sa.String(length=500), nullable=True),
        sa.Column("updated_by", sa.Uuid(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(
            "state IN ('snoozed', 'accepted', 'resolved')", name=op.f("ck_risk_states_state")
        ),
        sa.ForeignKeyConstraint(
            ["org_id"],
            ["organizations.id"],
            name=op.f("fk_risk_states_org_id_organizations"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["risk_id"], ["risks.id"], name=op.f("fk_risk_states_risk_id_risks"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["updated_by"],
            ["users.id"],
            name=op.f("fk_risk_states_updated_by_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_risk_states")),
        sa.UniqueConstraint("org_id", "risk_id", name=op.f("uq_risk_states_org_id_risk_id")),
    )
    op.create_index(op.f("ix_risk_states_org_id"), "risk_states", ["org_id"], unique=False)


def downgrade() -> None:

    op.drop_index(op.f("ix_risk_states_org_id"), table_name="risk_states")
    op.drop_table("risk_states")
