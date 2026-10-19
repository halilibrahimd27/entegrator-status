"""Alembic environment. The connection or URL is handed in by ``sentinel.db.migrate``."""

from __future__ import annotations

import asyncio

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine

import sentinel.models  # noqa: F401  (registers every table on the metadata)
from sentinel.db.base import Base

config = context.config
target_metadata = Base.metadata


def _run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


async def _run_with_url(url: str) -> None:
    engine = create_async_engine(url, poolclass=pool.NullPool)
    async with engine.connect() as connection:
        await connection.run_sync(_run_migrations)
    await engine.dispose()


if context.is_offline_mode():
    raise RuntimeError("offline (SQL script) migrations are not supported")

_connection = config.attributes.get("connection")
if _connection is not None:
    _run_migrations(_connection)
else:
    asyncio.run(_run_with_url(config.attributes["database_url"]))
