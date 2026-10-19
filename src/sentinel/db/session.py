"""Engine and the three kinds of sessions: unscoped, tenant-scoped and system."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from sentinel.tenancy.guard import BYPASS_KEY, SCOPE_KEY, GuardedSession
from sentinel.tenancy.scope import TenantScope


class Database:
    """One per process. Each context manager is one transaction (commit on success)."""

    def __init__(self, url: str, *, pool_size: int = 5) -> None:
        self.engine = create_async_engine(url, pool_pre_ping=True, pool_size=pool_size)
        self._sessions = async_sessionmaker(
            self.engine, expire_on_commit=False, sync_session_class=GuardedSession
        )

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        """No tenant scope: may touch only non-tenant tables (users, sessions, orgs)."""
        async with self._sessions() as session, session.begin():
            yield session

    @asynccontextmanager
    async def tenant(self, scope: TenantScope) -> AsyncIterator[AsyncSession]:
        """Every tenant-owned row read or written belongs to ``scope.org_id``."""
        async with self._sessions() as session:
            session.info[SCOPE_KEY] = scope
            async with session.begin():
                yield session

    @asynccontextmanager
    async def system(self, reason: str) -> AsyncIterator[AsyncSession]:
        """Crosses tenants on purpose. ``reason`` names the use (shows up in reviews)."""
        if not reason:
            raise ValueError("system sessions need a reason")
        async with self._sessions() as session:
            session.info[BYPASS_KEY] = reason
            async with session.begin():
                yield session

    async def dispose(self) -> None:
        await self.engine.dispose()
