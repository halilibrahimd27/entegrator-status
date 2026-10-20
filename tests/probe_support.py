"""Probe-protocol helpers and fakes shared by the ingest and prober tests."""

from __future__ import annotations

import asyncio
import ipaddress
import random
import time
import uuid
from collections.abc import Callable, Sequence
from typing import Any

import httpx
from fastapi import FastAPI

from sentinel.models import Component, Organization, Service, Source
from sentinel.probe.nodes import NodeCredentials
from sentinel.probe.robots import RobotsCache
from sentinel.security.addresses import AddressPolicy, IPAddress
from sentinel.security.errors import DNSResolutionError
from sentinel.security.http import OutboundClient
from sentinel.security.internal import CoreClient
from sentinel.security.ratelimit import HostLimiter
from sentinel.sources.adapters import AdapterEnv, ProbeJob, ProbeOutcome
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


# --- prober fakes ---------------------------------------------------------------


class Clock:
    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class Midpoint(random.Random):
    """Deterministic jitter: always the middle of the range."""

    def uniform(self, a: float, b: float) -> float:
        return (a + b) / 2


class Earliest(random.Random):
    def uniform(self, a: float, b: float) -> float:
        return a


def job(key: str = "k", url: str = "https://a.test/", **overrides: Any) -> ProbeJob:
    values: dict[str, Any] = {
        "key": key,
        "kind": "http",
        "config": {"url": url},
        "interval_s": 60,
        "timeout_s": 2,
        "allow_http": False,
        "ignores_robots": True,
        "source_ids": (str(uuid.uuid4()),),
    }
    values.update(overrides)
    return ProbeJob(**values)


OK = ProbeOutcome(True, None, 200, 5)


async def loopback(host: str, port: int) -> Sequence[IPAddress]:
    if host.endswith(".test"):
        return [ipaddress.ip_address("127.0.0.1")]
    raise DNSResolutionError("unknown test host")


def outbound(handler: Callable[[httpx.Request], Any], **limits: Any) -> OutboundClient:
    """An outbound client whose 'internet' is ``handler`` (sync or async)."""

    async def respond(request: httpx.Request) -> httpx.Response:
        answer = handler(request)
        if asyncio.iscoroutine(answer):
            answer = await answer
        assert isinstance(answer, httpx.Response)
        return answer

    return OutboundClient(
        user_agent="EntegratorStatus/test",
        resolver=loopback,
        policy=AddressPolicy.for_tests(),
        transport=httpx.MockTransport(respond),
        limiter=HostLimiter(**limits) if limits else None,
    )


def env_for(http: OutboundClient) -> AdapterEnv:
    async def unused(*args: Any) -> Any:
        raise AssertionError("not used in these tests")

    return AdapterEnv(http=http, robots=RobotsCache(http), tls=unused, dns=unused)


def host_of(request: httpx.Request) -> str:
    return request.headers["host"].split(":")[0]


def stored(n: int) -> list[dict[str, Any]]:
    return [{"id": str(uuid.uuid4()), "n": i} for i in range(n)]
