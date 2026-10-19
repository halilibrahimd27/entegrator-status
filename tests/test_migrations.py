from __future__ import annotations

from typing import Any

from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import inspect
from sqlalchemy.engine import Connection

from sentinel.db.base import Base
from sentinel.db.migrate import downgrade, upgrade
from sentinel.db.session import Database


def _diff(connection: Connection) -> list[Any]:
    context = MigrationContext.configure(connection, opts={"compare_type": True})
    return list(compare_metadata(context, Base.metadata))


def _tables(connection: Connection) -> set[str]:
    return set(inspect(connection).get_table_names())


async def test_models_match_the_migrated_schema(db: Database) -> None:
    async with db.engine.connect() as connection:
        assert await connection.run_sync(_diff) == []


async def test_downgrade_to_base_and_back(db: Database) -> None:
    await downgrade(db.engine, "base")
    async with db.engine.connect() as connection:
        assert await connection.run_sync(_tables) == {"alembic_version"}
    await upgrade(db.engine)
    async with db.engine.connect() as connection:
        assert {"users", "organizations", "memberships"} <= await connection.run_sync(_tables)
