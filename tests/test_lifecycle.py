"""Advisories (OSV) and end of life (endoflife.date) for the versions in use."""

from __future__ import annotations

import ipaddress
import json
import uuid
from collections.abc import Sequence
from datetime import timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy import select, update

from probe_support import env_for, job, two_orgs
from sentinel.db.base import utcnow
from sentinel.models import Component, Dependency, Product, Risk, Service, Snapshot, Source
from sentinel.risk.lifecycle import affected, cycle_of, run_lifecycle
from sentinel.security.addresses import AddressPolicy, IPAddress
from sentinel.security.http import OutboundClient
from sentinel.security.ratelimit import HostLimiter
from sentinel.sources.adapters import adapter_for
from sentinel.sources.snapshots import snapshot_hash
from sentinel.sources.versions import parse

VULN: dict[str, Any] = {
    "id": "GHSA-xxxx-0001",
    "events": [
        {"introduced": "0"},
        {"fixed": "1.2.3"},
        {"introduced": "2.0.0"},
        {"last_affected": "2.1.0"},
    ],
    "versions": ["3.0.0-rc.1"],
}


@pytest.mark.parametrize(
    ("version", "hit"),
    [
        ("1.0.0", True),
        ("1.2.2", True),
        ("1.2.3", False),  # fixed
        ("1.9.0", False),
        ("2.0.0", True),
        ("2.1.0", True),  # last_affected is inclusive
        ("2.1.1", False),
        ("3.0.0-rc.1", True),  # listed explicitly
    ],
)
def test_affected(version: str, hit: bool) -> None:
    parsed = parse(version)
    assert parsed is not None
    assert affected(VULN, parsed) is hit


def test_open_ended_and_odd_ranges() -> None:
    v = parse("5.0.0")
    assert v is not None
    assert affected({"events": [{"introduced": "4.0.0"}]}, v)  # never fixed
    assert not affected({"events": [{"introduced": "abc123"}, {"fixed": "def456"}]}, v)  # commits


def test_cycle_of() -> None:
    cycles = [{"cycle": "3"}, {"cycle": "3.12"}, {"cycle": "3.11"}, {"cycle": "jammy"}]
    v = parse("3.12.4")
    assert v is not None
    found = cycle_of(v, cycles)
    assert found is not None
    assert found["cycle"] == "3.12"  # the longest prefix
    other = parse("4.0")
    assert other is not None
    assert cycle_of(other, cycles) is None


# --- adapters ---------------------------------------------------------------------------


async def anywhere(host: str, port: int) -> Sequence[IPAddress]:
    return [ipaddress.ip_address("127.0.0.1")]


async def run(kind: str, config: dict[str, Any], handler: Any) -> Any:
    adapter = adapter_for(kind)
    assert adapter is not None
    http = OutboundClient(
        user_agent="EntegratorStatus/test",
        resolver=anywhere,
        policy=AddressPolicy.for_tests(),
        transport=httpx.MockTransport(handler),
        limiter=HostLimiter(rate_per_s=100, burst=10),
    )
    async with http:
        return await adapter.run(
            job(kind=kind, url="https://x.test/", config=config), env_for(http)
        )


async def test_osv_query() -> None:
    asked: list[httpx.Request] = []
    answer = {
        "vulns": [
            {
                "id": "GHSA-xxxx-0001",
                "summary": "Prototype pollution",
                "database_specific": {"severity": "HIGH"},
                "affected": [
                    {
                        "package": {"name": "acme-utils", "ecosystem": "npm"},
                        "ranges": [
                            {"type": "SEMVER", "events": [{"introduced": "0"}, {"fixed": "1.2.3"}]},
                            {"type": "GIT", "events": [{"introduced": "abc"}]},
                        ],
                    },
                    {"package": {"name": "other", "ecosystem": "npm"}, "versions": ["9.9.9"]},
                ],
            },
            {"id": "GHSA-gone", "withdrawn": "2026-01-01T00:00:00Z"},
        ]
    }

    def osv(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        asked.append(request)
        return httpx.Response(200, json=answer)

    outcome = await run("advisory", {"ecosystem": "npm", "name": "acme-utils"}, osv)
    assert outcome.ok
    (item,) = outcome.snapshot.items
    assert item["severity"] == "HIGH"
    assert item["events"] == [
        {"introduced": "0"},
        {"fixed": "1.2.3"},
    ]  # GIT ranges and others dropped
    assert item["versions"] == []
    assert item["link"] == "https://osv.dev/vulnerability/GHSA-xxxx-0001"
    (request,) = asked
    # the connection is pinned to the resolved IP; the Host header names OSV
    assert (request.method, request.headers["host"], request.url.path) == (
        "POST",
        "api.osv.dev",
        "/v1/query",
    )
    assert json.loads(request.content) == {"package": {"name": "acme-utils", "ecosystem": "npm"}}

    clean = await run(
        "advisory", {"ecosystem": "pypi", "name": "acme"}, lambda r: httpx.Response(200, json={})
    )
    assert clean.ok  # no advisories is a result, not "needs attention"
    assert clean.snapshot.items == [{"id": "none", "type": "none"}]
    odd = await run("advisory", {"ecosystem": "cobol", "name": "x"}, osv)
    assert odd.error_class == "unsupported"


async def test_endoflife() -> None:
    cycles = [
        {"cycle": "3.12", "eol": "2028-10-31", "latest": "3.12.7", "support": "2025-04-02"},
        {"cycle": "2.7", "eol": True},
        {"cycle": "3.13", "eol": False, "lts": False},
    ]

    def site(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /\n")
        assert request.url.path == "/api/python.json"
        return httpx.Response(200, json=cycles)

    outcome = await run("eol", {"product": "python"}, site)
    by_cycle = {i["cycle"]: i for i in outcome.snapshot.items}
    assert by_cycle["3.12"]["eol"] == "2028-10-31"
    assert by_cycle["2.7"]["eol"] is True
    assert by_cycle["3.13"]["eol"] is False
    assert by_cycle["3.12"]["link"] == "https://endoflife.date/python"


# --- risks -----------------------------------------------------------------------------


async def component_with(
    services: Any, kind: str, config: dict[str, Any], items: list[dict[str, Any]]
) -> Source:
    async with services.db.system("test setup") as s:
        service = Service(slug=f"s-{uuid.uuid4().hex[:6]}", name="Paket", kind="package", tags=[])
        s.add(service)
        await s.flush()
        component = Component(service_id=service.id, slug="c", name="C", tags=[])
        s.add(component)
        await s.flush()
        source = Source(
            component_id=component.id,
            kind=kind,
            identity=f"{kind}:{uuid.uuid4().hex}",
            config=config,
            interval_s=21600,
            timeout_s=10,
        )
        s.add(source)
        await s.flush()
        s.add(
            Snapshot(source_id=source.id, taken_at=utcnow(), hash=snapshot_hash(items), items=items)
        )
        return source


async def pin(services: Any, org_id: uuid.UUID, component_id: uuid.UUID, version: str) -> uuid.UUID:
    async with services.db.system("test setup") as s:
        product = Product(org_id=org_id, slug=f"p-{uuid.uuid4().hex[:6]}", name="Ürün")
        s.add(product)
        await s.flush()
        dependency = Dependency(
            org_id=org_id,
            from_product_id=product.id,
            to_component_id=component_id,
            usage={"version": version},
        )
        s.add(dependency)
        await s.flush()
        return dependency.id


async def open_risks(services: Any, kind: str) -> list[Risk]:
    async with services.db.system("test check") as s:
        return list(
            (
                await s.scalars(select(Risk).where(Risk.kind == kind, Risk.resolved_at.is_(None)))
            ).all()
        )


async def test_advisory_risks_per_pinned_version(services: Any) -> None:
    item = {"id": "GHSA-xxxx-0001", "title": "Prototype pollution", "severity": "HIGH",
            "link": "https://osv.dev/vulnerability/GHSA-xxxx-0001", **VULN}  # fmt: skip
    source = await component_with(
        services, "advisory", {"ecosystem": "npm", "name": "acme-utils", "version": "1.0.0"}, [item]
    )
    org_a, org_b = await two_orgs(services)
    vulnerable = await pin(services, org_a, source.component_id, "^1.2.0")
    await pin(services, org_b, source.component_id, "1.3.0")
    assert (
        await run_lifecycle(services.db)
    ).opened == 2  # org A's pin and the source's own version
    risks = {r.subject: r for r in await open_risks(services, "advisory")}
    assert set(risks) == {f"GHSA-xxxx-0001|{vulnerable}", "GHSA-xxxx-0001|source"}
    mine = risks[f"GHSA-xxxx-0001|{vulnerable}"]
    assert (mine.org_id, mine.detail["severity"], mine.detail["version"]) == (
        org_a,
        "HIGH",
        "1.2.0",
    )
    assert risks["GHSA-xxxx-0001|source"].org_id is None  # the shared source's own version

    async with services.db.system("test change") as s:
        await s.execute(
            update(Dependency).where(Dependency.id == vulnerable).values(usage={"version": "1.2.3"})
        )
    assert (await run_lifecycle(services.db)).resolved == 1  # upgraded past the fix


async def test_end_of_life_risks(services: Any) -> None:
    soon = (utcnow() + timedelta(days=100)).date().isoformat()
    later = (utcnow() + timedelta(days=1000)).date().isoformat()
    items: list[dict[str, Any]] = [
        {"id": "c:3.11", "cycle": "3.11", "eol": soon, "link": "https://endoflife.date/python"},
        {"id": "c:3.12", "cycle": "3.12", "eol": later},
        {"id": "c:2.7", "cycle": "2.7", "eol": True},
    ]
    source = await component_with(services, "eol", {"product": "python"}, items)
    org_a, _ = await two_orgs(services)
    for version in ("3.11.9", "3.12.4", "2.7.18"):
        await pin(services, org_a, source.component_id, version)
    assert (await run_lifecycle(services.db)).opened == 2
    by_cycle = {r.detail["cycle"]: r for r in await open_risks(services, "eol")}
    assert set(by_cycle) == {"3.11", "2.7"}  # 3.12 is years away
    assert by_cycle["3.11"].due_at is not None
    assert by_cycle["3.11"].due_at.date().isoformat() == soon
    assert by_cycle["2.7"].due_at is None
    assert by_cycle["2.7"].detail["ended"] is True
