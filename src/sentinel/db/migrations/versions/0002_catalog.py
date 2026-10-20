"""Catalog and dependency graph.

Revision ID: 0002_catalog
Revises: 0001_accounts
Create Date: 2026-10-20 09:24:06.165973
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002_catalog"
down_revision: str | Sequence[str] | None = "0001_accounts"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "products",
        sa.Column("slug", sa.String(length=63), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
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
            name=op.f("fk_products_org_id_organizations"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_products")),
        sa.UniqueConstraint("org_id", "slug", name=op.f("uq_products_org_id_slug")),
    )
    op.create_index(op.f("ix_products_org_id"), "products", ["org_id"], unique=False)
    op.create_table(
        "services",
        sa.Column("slug", sa.String(length=63), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("homepage", sa.String(length=500), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("tags", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("purl", sa.String(length=255), nullable=True),
        sa.Column("pack", sa.String(length=63), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("org_id", sa.Uuid(), nullable=True),
        sa.CheckConstraint(
            "kind in ('saas_api', 'integrator', 'authority', 'cloud', 'package', 'container_image', 'domain', 'website', 'internal', 'other')",
            name=op.f("ck_services_kind"),
        ),
        sa.ForeignKeyConstraint(
            ["org_id"],
            ["organizations.id"],
            name=op.f("fk_services_org_id_organizations"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_services")),
        sa.UniqueConstraint(
            "org_id",
            "slug",
            name=op.f("uq_services_org_id_slug"),
            postgresql_nulls_not_distinct=True,
        ),
    )
    op.create_index(op.f("ix_services_org_id"), "services", ["org_id"], unique=False)
    op.create_index(op.f("ix_services_purl"), "services", ["purl"], unique=False)
    op.create_table(
        "catalog_proposals",
        sa.Column("service_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("proposed_by_user_id", sa.Uuid(), nullable=True),
        sa.Column("reviewed_by_user_id", sa.Uuid(), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("review_note", sa.Text(), nullable=True),
        sa.Column("shared_service_id", sa.Uuid(), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(
            "status in ('pending', 'accepted', 'rejected')",
            name=op.f("ck_catalog_proposals_status"),
        ),
        sa.ForeignKeyConstraint(
            ["org_id"],
            ["organizations.id"],
            name=op.f("fk_catalog_proposals_org_id_organizations"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["proposed_by_user_id"],
            ["users.id"],
            name=op.f("fk_catalog_proposals_proposed_by_user_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["reviewed_by_user_id"],
            ["users.id"],
            name=op.f("fk_catalog_proposals_reviewed_by_user_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["service_id"],
            ["services.id"],
            name=op.f("fk_catalog_proposals_service_id_services"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["shared_service_id"],
            ["services.id"],
            name=op.f("fk_catalog_proposals_shared_service_id_services"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_catalog_proposals")),
    )
    op.create_index(
        op.f("ix_catalog_proposals_org_id"), "catalog_proposals", ["org_id"], unique=False
    )
    op.create_index(
        op.f("ix_catalog_proposals_service_id"), "catalog_proposals", ["service_id"], unique=False
    )
    op.create_table(
        "components",
        sa.Column("service_id", sa.Uuid(), nullable=False),
        sa.Column("slug", sa.String(length=63), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("tags", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("org_id", sa.Uuid(), nullable=True),
        sa.ForeignKeyConstraint(
            ["org_id"],
            ["organizations.id"],
            name=op.f("fk_components_org_id_organizations"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["service_id"],
            ["services.id"],
            name=op.f("fk_components_service_id_services"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_components")),
        sa.UniqueConstraint("service_id", "slug", name=op.f("uq_components_service_id_slug")),
    )
    op.create_index(op.f("ix_components_org_id"), "components", ["org_id"], unique=False)
    op.create_index(op.f("ix_components_service_id"), "components", ["service_id"], unique=False)
    op.create_table(
        "catalog_tag_rules",
        sa.Column("tag", sa.String(length=63), nullable=False),
        sa.Column("to_component_id", sa.Uuid(), nullable=False),
        sa.Column("pack", sa.String(length=63), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["to_component_id"],
            ["components.id"],
            name=op.f("fk_catalog_tag_rules_to_component_id_components"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_catalog_tag_rules")),
        sa.UniqueConstraint(
            "tag", "to_component_id", name=op.f("uq_catalog_tag_rules_tag_to_component_id")
        ),
    )
    op.create_table(
        "dependencies",
        sa.Column("from_product_id", sa.Uuid(), nullable=True),
        sa.Column("from_component_id", sa.Uuid(), nullable=True),
        sa.Column("to_component_id", sa.Uuid(), nullable=False),
        sa.Column("usage", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("note", sa.String(length=500), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("org_id", sa.Uuid(), nullable=True),
        sa.CheckConstraint(
            "(from_product_id IS NULL) <> (from_component_id IS NULL)",
            name=op.f("ck_dependencies_one_origin"),
        ),
        sa.CheckConstraint(
            "from_component_id IS NULL OR from_component_id <> to_component_id",
            name=op.f("ck_dependencies_no_self_edge"),
        ),
        sa.ForeignKeyConstraint(
            ["from_component_id"],
            ["components.id"],
            name=op.f("fk_dependencies_from_component_id_components"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["from_product_id"],
            ["products.id"],
            name=op.f("fk_dependencies_from_product_id_products"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["org_id"],
            ["organizations.id"],
            name=op.f("fk_dependencies_org_id_organizations"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["to_component_id"],
            ["components.id"],
            name=op.f("fk_dependencies_to_component_id_components"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_dependencies")),
        sa.UniqueConstraint(
            "org_id",
            "from_product_id",
            "from_component_id",
            "to_component_id",
            name=op.f("uq_dependencies_org_id_from_product_id_from_component_id_to_component_id"),
            postgresql_nulls_not_distinct=True,
        ),
    )
    op.create_index(
        op.f("ix_dependencies_from_component_id"),
        "dependencies",
        ["from_component_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_dependencies_from_product_id"), "dependencies", ["from_product_id"], unique=False
    )
    op.create_index(op.f("ix_dependencies_org_id"), "dependencies", ["org_id"], unique=False)
    op.create_index(
        op.f("ix_dependencies_to_component_id"), "dependencies", ["to_component_id"], unique=False
    )
    op.create_table(
        "sources",
        sa.Column("component_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("identity", sa.String(length=500), nullable=False),
        sa.Column("config", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("interval_s", sa.Integer(), nullable=False),
        sa.Column("timeout_s", sa.Integer(), nullable=False),
        sa.Column("allow_http", sa.Boolean(), nullable=False),
        sa.Column("availability_ignores_robots", sa.Boolean(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("provenance", sa.String(length=500), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("org_id", sa.Uuid(), nullable=True),
        sa.CheckConstraint(
            "kind in ('http', 'tls_cert', 'dns', 'domain_expiry', 'status_page', 'api_contract', 'release', 'feed', 'page_watch', 'advisory', 'eol')",
            name=op.f("ck_sources_kind"),
        ),
        sa.ForeignKeyConstraint(
            ["component_id"],
            ["components.id"],
            name=op.f("fk_sources_component_id_components"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["org_id"],
            ["organizations.id"],
            name=op.f("fk_sources_org_id_organizations"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_sources")),
    )
    op.create_index(op.f("ix_sources_component_id"), "sources", ["component_id"], unique=False)
    op.create_index(op.f("ix_sources_org_id"), "sources", ["org_id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_sources_org_id"), table_name="sources")
    op.drop_index(op.f("ix_sources_component_id"), table_name="sources")
    op.drop_table("sources")
    op.drop_index(op.f("ix_dependencies_to_component_id"), table_name="dependencies")
    op.drop_index(op.f("ix_dependencies_org_id"), table_name="dependencies")
    op.drop_index(op.f("ix_dependencies_from_product_id"), table_name="dependencies")
    op.drop_index(op.f("ix_dependencies_from_component_id"), table_name="dependencies")
    op.drop_table("dependencies")
    op.drop_table("catalog_tag_rules")
    op.drop_index(op.f("ix_components_service_id"), table_name="components")
    op.drop_index(op.f("ix_components_org_id"), table_name="components")
    op.drop_table("components")
    op.drop_index(op.f("ix_catalog_proposals_service_id"), table_name="catalog_proposals")
    op.drop_index(op.f("ix_catalog_proposals_org_id"), table_name="catalog_proposals")
    op.drop_table("catalog_proposals")
    op.drop_index(op.f("ix_services_purl"), table_name="services")
    op.drop_index(op.f("ix_services_org_id"), table_name="services")
    op.drop_table("services")
    op.drop_index(op.f("ix_products_org_id"), table_name="products")
    op.drop_table("products")
