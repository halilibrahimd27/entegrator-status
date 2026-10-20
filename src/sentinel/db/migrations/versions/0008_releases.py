"""Releases: release changes and classes, change details, risk subjects.

Revision ID: 0008_releases
Revises: 0007_risks
Create Date: 2026-10-20 12:10:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0008_releases"
down_revision: str | Sequence[str] | None = "0007_risks"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

KINDS = ("announcement", "vendor_incident", "vendor_maintenance")
CATEGORIES = ("maintenance", "deprecation", "security", "package_update", "incident", "other")
RELEASE_CATEGORIES = ("major", "minor", "patch", "prerelease")


def _in(column: str, values: Sequence[str]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def _checks(kinds: Sequence[str], categories: Sequence[str]) -> None:
    op.drop_constraint(op.f("ck_changes_kind"), "changes", type_="check")
    op.drop_constraint(op.f("ck_changes_category"), "changes", type_="check")
    op.create_check_constraint(op.f("ck_changes_kind"), "changes", _in("kind", kinds))
    op.create_check_constraint(op.f("ck_changes_category"), "changes", _in("category", categories))


def upgrade() -> None:
    _checks((*KINDS, "release"), (*CATEGORIES, *RELEASE_CATEGORIES))
    op.add_column(
        "changes",
        sa.Column(
            "detail",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default="{}",
            nullable=False,
        ),
    )
    op.add_column(
        "risks", sa.Column("subject", sa.String(length=120), server_default="", nullable=False)
    )
    op.drop_index(
        "uq_risks_open_per_source", table_name="risks", postgresql_where="resolved_at IS NULL"
    )
    op.create_index(
        "uq_risks_open_per_subject",
        "risks",
        ["source_id", "kind", "subject"],
        unique=True,
        postgresql_where="resolved_at IS NULL",
    )


def downgrade() -> None:
    op.drop_index(
        "uq_risks_open_per_subject", table_name="risks", postgresql_where="resolved_at IS NULL"
    )
    op.execute("DELETE FROM risks WHERE subject <> ''")
    op.create_index(
        "uq_risks_open_per_source",
        "risks",
        ["source_id", "kind"],
        unique=True,
        postgresql_where="resolved_at IS NULL",
    )
    op.drop_column("risks", "subject")
    op.drop_column("changes", "detail")
    op.execute("DELETE FROM changes WHERE kind = 'release'")
    _checks(KINDS, CATEGORIES)
