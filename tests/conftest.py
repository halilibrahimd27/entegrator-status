"""Shared fixtures. Tests never touch the real network (enforced below).

Database tests use the throwaway PostgreSQL from ``compose.test.yml`` (``make test``
starts it; CI provides service containers) via ``TEST_DATABASE_URL``.
"""

from __future__ import annotations

import asyncio
import os
import secrets
import socket
from collections.abc import AsyncIterator
from typing import Any

import httpx2
import pytest
from fastapi import FastAPI
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from sentinel.accounts.mail import Mail, MemoryMailer
from sentinel.config import SECRET_FIELDS, load_settings
from sentinel.db.base import Base
from sentinel.db.migrate import upgrade
from sentinel.db.session import Database
from sentinel.web.app import create_app
from sentinel.web.services import AppServices

_LOOPBACK = {"127.0.0.1", "::1", "localhost"}
_FALLBACK_DB = "postgresql+asyncpg://sentinel@127.0.0.1:55432/sentinel_test"
_FALLBACK_REDIS = "redis://127.0.0.1:56379/0"


@pytest.fixture(autouse=True)
def _no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    real_connect = socket.socket.connect
    real_getaddrinfo = socket.getaddrinfo

    def guarded_connect(self: socket.socket, address: Any) -> None:
        if self.family == socket.AF_UNIX or (
            isinstance(address, tuple) and address[0] in _LOOPBACK
        ):
            real_connect(self, address)
            return
        raise RuntimeError(f"tests must not use the network (connect {address!r})")

    def guarded_getaddrinfo(host: Any, *args: Any, **kwargs: Any) -> Any:
        if host is None or host in _LOOPBACK:
            return real_getaddrinfo(host, *args, **kwargs)
        raise RuntimeError(f"tests must not use the network (resolve {host!r})")

    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    monkeypatch.setattr(socket, "getaddrinfo", guarded_getaddrinfo)


def random_secret() -> str:
    return secrets.token_urlsafe(48)


@pytest.fixture
def valid_env(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """A complete, valid environment; tests break one variable at a time."""
    env = {
        "ENVIRONMENT": "test",
        "PUBLIC_BASE_URL": "https://status.example.invalid/",
        "CONTACT_EMAIL": "bot@status.example.invalid",
        "DATABASE_URL": os.environ.get("TEST_DATABASE_URL", _FALLBACK_DB),
        "REDIS_URL": os.environ.get("TEST_REDIS_URL", _FALLBACK_REDIS),
        "MAIL_BACKEND": "memory",
        **{name.upper(): random_secret() for name in SECRET_FIELDS},
    }
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return env


@pytest.fixture(scope="session")
def database_url() -> str:
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.fail("TEST_DATABASE_URL is not set; run tests with `make test`")
    return url


@pytest.fixture(scope="session")
def _migrated_schema(database_url: str) -> None:
    """Fresh schema once per run, built by the real migrations."""

    async def rebuild() -> None:
        engine = create_async_engine(database_url)
        async with engine.begin() as connection:
            await connection.execute(text("DROP SCHEMA public CASCADE"))
            await connection.execute(text("CREATE SCHEMA public"))
        await upgrade(engine)
        await engine.dispose()

    asyncio.run(rebuild())


@pytest.fixture
async def db(_migrated_schema: None, database_url: str) -> AsyncIterator[Database]:
    database = Database(database_url, pool_size=2)
    try:
        yield database
    finally:
        tables = ", ".join(table.name for table in Base.metadata.sorted_tables)
        async with database.engine.begin() as connection:
            await connection.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))
        await database.dispose()


BASE_URL = "https://status.example.invalid"


@pytest.fixture
async def app(valid_env: dict[str, str], db: Database) -> AsyncIterator[FastAPI]:
    """The real app with its lifespan, on the test database and Redis."""
    application = create_app(load_settings())
    async with application.router.lifespan_context(application):
        services: AppServices = application.state.services
        await services.redis.flushdb()
        try:
            yield application
        finally:
            await services.redis.flushdb()


@pytest.fixture
def services(app: FastAPI) -> AppServices:
    svc: AppServices = app.state.services
    return svc


@pytest.fixture
def outbox(services: AppServices) -> list[Mail]:
    assert isinstance(services.mailer, MemoryMailer)
    return services.mailer.outbox


@pytest.fixture
async def http(app: FastAPI) -> AsyncIterator[httpx2.AsyncClient]:
    transport = httpx2.ASGITransport(app=app)
    async with httpx2.AsyncClient(transport=transport, base_url=BASE_URL) as client:
        yield client


@pytest.fixture
async def redis_client() -> AsyncIterator[Redis]:
    url = os.environ.get("TEST_REDIS_URL")
    if not url:
        pytest.fail("TEST_REDIS_URL is not set; run tests with `make test`")
    client = Redis.from_url(url)
    await client.flushdb()
    try:
        yield client
    finally:
        await client.flushdb()
        await client.aclose()
