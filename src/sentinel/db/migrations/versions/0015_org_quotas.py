"""Per-organization source quota.

Revision ID: 0015_org_quotas
Revises: 0014_corrections
Create Date: 2026-10-24 01:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0015_org_quotas"
down_revision: str | Sequence[str] | None = "0014_corrections"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("organizations", sa.Column("max_sources", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("organizations", "max_sources")
