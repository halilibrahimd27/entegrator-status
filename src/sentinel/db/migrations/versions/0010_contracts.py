"""Contracts: contract changes and their classes.

Revision ID: 0010_contracts
Revises: 0009_credentials
Create Date: 2026-10-23 10:00:00
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0010_contracts"
down_revision: str | Sequence[str] | None = "0009_credentials"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

KINDS = ("announcement", "vendor_incident", "vendor_maintenance", "release")
CATEGORIES = (
    "maintenance",
    "deprecation",
    "security",
    "package_update",
    "incident",
    "other",
    "major",
    "minor",
    "patch",
    "prerelease",
)
CONTRACT_CATEGORIES = ("breaking", "non_breaking", "cosmetic")


def _in(column: str, values: Sequence[str]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def _checks(kinds: Sequence[str], categories: Sequence[str]) -> None:
    op.drop_constraint(op.f("ck_changes_kind"), "changes", type_="check")
    op.drop_constraint(op.f("ck_changes_category"), "changes", type_="check")
    op.create_check_constraint(op.f("ck_changes_kind"), "changes", _in("kind", kinds))
    op.create_check_constraint(op.f("ck_changes_category"), "changes", _in("category", categories))


def upgrade() -> None:
    _checks((*KINDS, "contract"), (*CATEGORIES, *CONTRACT_CATEGORIES))


def downgrade() -> None:
    op.execute("DELETE FROM changes WHERE kind = 'contract'")
    _checks(KINDS, CATEGORIES)
