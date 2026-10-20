"""Authenticated checks end to end (ADR 0011): sealed for trusted nodes, bound to one host.

A canary secret is followed through the whole path: the job list, the request headers,
a redirect to another host, the node's buffer, the stored observations and the logs.
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
import uuid
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select

from probe_support import Clock, Midpoint, client_for, env_for, job, outbound
from sentinel.models import Component, Observation, Organization, Service, Source
from sentinel.probe.buffer import ResultBuffer
from sentinel.probe.health import NodeHealth
from sentinel.probe.prober import Prober
from sentinel.probe.scheduler import Scheduler
from sentinel.security.vault import seal_for_node
from sentinel.sources.adapters import adapter_for
from sentinel.sources.auth import AuthProvider
from sentinel.tenancy.roles import Role
from sentinel.tenancy.scope import TenantScope
from sentinel.web.services import AppServices

API = "https://api.vendor.test/health"


async def authenticated_source(
    services: AppServices, kind: str = "bearer", **extra: str
) -> tuple[uuid.UUID, str]:
    """A private http source of a new organization with a credential holding a canary."""
    canary = "canary-" + secrets.token_hex(12)
    async with services.db.system("test setup") as s:
        org = Organization(slug=f"o-{uuid.uuid4().hex[:6]}", name="Org")
        s.add(org)
        await s.flush()
        service = Service(org_id=org.id, slug="api", name="API", kind="other", tags=[])
        s.add(service)
        await s.flush()
        component = Component(org_id=org.id, service_id=service.id, slug="c", name="C", tags=[])
        s.add(component)
        await s.flush()
        source = Source(
            org_id=org.id,
            component_id=component.id,
            kind="http",
            identity=f"http:{API}",
            config={"url": API},
            interval_s=120,
            timeout_s=5,
            availability_ignores_robots=True,
        )
        s.add(source)
        await s.flush()
        org_id, source_id = org.id, source.id
    scope = TenantScope(org_id=org_id, user_id=None, role=Role.OWNER)
    secret = (
        {"token": canary}
        if kind == "bearer"
        else {"client_id": "client-1", "client_secret": canary}
    )
    credential = await services.credentials.create(
        scope, name="API", kind=kind, host="api.vendor.test", secret=secret, entitled=True, **extra
    )
    await services.credentials.attach(scope, source_id, credential)
    return source_id, canary


async def test_only_trusted_nodes_get_sealed_jobs(app: FastAPI, services: AppServices) -> None:
    source_id, canary = await authenticated_source(services)
    trusted = await services.nodes.register("trusted", "test", credentials=True)
    plain = await services.nodes.register("plain", "test")
    assert await client_for(app, plain).jobs() == []  # never offered to an untrusted node
    raw = await client_for(app, trusted).jobs()
    (only,) = raw
    assert only["source_ids"] == [str(source_id)]
    assert only["auth"]
    assert canary not in json.dumps(raw)  # sealed, not readable in the job list
    listed = await services.nodes.jobs()  # an operator listing carries no secrets
    assert [j.auth for j in listed] == [None]


async def test_secret_travels_to_its_host_only(
    app: FastAPI, services: AppServices, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    _, canary = await authenticated_source(services)
    creds = await services.nodes.register("trusted", "test", credentials=True)
    seen: list[httpx.Request] = []

    def internet(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.headers["host"] == "api.vendor.test":
            return httpx.Response(302, headers={"Location": "https://cdn.other.test/health"})
        return httpx.Response(200)

    buffer = ResultBuffer(tmp_path / "buffer.sqlite3")
    clock = Clock()
    async with outbound(internet) as http:
        env = env_for(http)
        env.auth = AuthProvider(creds.signing_key, http)
        prober = Prober(
            core=client_for(app, creds),
            env=env,
            buffer=buffer,
            health=NodeHealth([], http),
            scheduler=Scheduler(clock=clock, rng=Midpoint()),
        )
        assert await prober.refresh_jobs()
        clock.advance(120)
        await asyncio.gather(*await prober.run_due())
        assert await prober.ship() == 1
    buffer.close()

    first, second = seen
    assert first.headers["authorization"] == f"Bearer {canary}"
    assert "authorization" not in second.headers  # dropped on the redirect to another host
    assert canary.encode() not in (tmp_path / "buffer.sqlite3").read_bytes()
    async with services.db.system("test check") as s:
        details = [o.details for o in (await s.scalars(select(Observation))).all()]
    assert details
    assert canary not in json.dumps(details)
    assert canary not in caplog.text


async def test_oauth2_client_credentials_token_is_cached(services: AppServices) -> None:
    _, canary = await authenticated_source(
        services,
        kind="oauth2_client",
        token_url="https://auth.vendor.test/token",
        oauth_scope="read",
    )
    creds = await services.nodes.register("trusted", "test", credentials=True)
    (wire,) = await services.nodes.jobs(creds.node_id)
    hits: dict[str, list[httpx.Request]] = {"token": [], "api": []}

    def internet(request: httpx.Request) -> httpx.Response:
        if request.headers["host"] == "auth.vendor.test":
            hits["token"].append(request)
            return httpx.Response(200, json={"access_token": "at-123", "expires_in": 3600})
        hits["api"].append(request)
        return httpx.Response(200)

    adapter = adapter_for("http")
    assert adapter is not None
    async with outbound(internet) as http:
        env = env_for(http)
        env.auth = AuthProvider(creds.signing_key, http)
        probe = job(key=wire.key, url=API, auth_sealed=wire.auth, config=wire.config)
        for _ in range(2):
            outcome = await adapter.run(probe, env)
            assert outcome.ok, outcome
    (token,) = hits["token"]  # exchanged once, then cached
    assert token.method == "POST"
    assert token.content == b"grant_type=client_credentials&scope=read"
    assert token.headers["authorization"].startswith("Basic ")
    assert canary not in token.content.decode()
    assert [r.headers["authorization"] for r in hits["api"]] == ["Bearer at-123"] * 2


async def test_unopenable_secrets_are_our_failure_not_the_providers(services: AppServices) -> None:
    adapter = adapter_for("http")
    assert adapter is not None
    async with outbound(lambda r: httpx.Response(200)) as http:
        env = env_for(http)
        env.auth = AuthProvider("k" * 64, http)
        forged = seal_for_node("another-node-key" * 4, "job", {"kind": "bearer"})
        outcome = await adapter.run(job(key="job", url=API, auth_sealed=forged), env)
        assert outcome.error_class == "auth_failed"
        env.auth = None
        outcome = await adapter.run(job(key="job", url=API, auth_sealed=forged), env)
        assert outcome.error_class == "auth_failed"  # this node does not handle credentials
