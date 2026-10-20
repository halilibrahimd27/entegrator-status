"""Probe-protocol helpers shared by the ingest and prober tests."""

from __future__ import annotations

import time
import uuid

import httpx
from fastapi import FastAPI

from sentinel.models import Component, Organization, Service, Source
from sentinel.probe.nodes import NodeCredentials
from sentinel.security.internal import CoreClient
from sentinel.sources.config import validate_source
from sentinel.web.services import AppServices


def client_for(
    app: FastAPI, creds: NodeCredentials, *, skew: float = 0, key: str | None = None
) -> CoreClient:
    return CoreClient(
        "http://core.test",
        creds.node_id,
        creds.token,
        key or creds.signing_key,
        user_agent="test",
        transport=httpx.ASGITransport(app=app),
        clock=lambda: time.time() + skew,
    )


async def add_source(
    services: AppServices, org_id: uuid.UUID | None, url: str, kind: str = "http"
) -> uuid.UUID:
    config = {"url": url} if kind == "http" else {"ecosystem": "npm", "name": url}
    valid = validate_source(kind, config)
    async with services.db.system("test setup") as s:
        service = Service(
            org_id=org_id, slug=f"s-{uuid.uuid4().hex[:8]}", name="S", kind="other", tags=[]
        )
        s.add(service)
        await s.flush()
        component = Component(org_id=org_id, service_id=service.id, slug="c", name="C", tags=[])
        s.add(component)
        await s.flush()
        source = Source(
            org_id=org_id,
            component_id=component.id,
            kind=valid.kind.value,
            identity=valid.identity,
            config=valid.config,
            interval_s=valid.interval_s,
            timeout_s=valid.timeout_s,
        )
        s.add(source)
        await s.flush()
        return source.id


async def two_orgs(services: AppServices) -> tuple[uuid.UUID, uuid.UUID]:
    async with services.db.system("test setup") as s:
        a, b = Organization(slug="a", name="A"), Organization(slug="b", name="B")
        s.add_all([a, b])
        await s.flush()
        return a.id, b.id
