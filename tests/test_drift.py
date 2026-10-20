"""Pinned vs. latest: drift on product pages and unadopted-major risks per organization."""

from __future__ import annotations

import uuid
from typing import Any

import httpx2
import pytest
from sqlalchemy import delete, select, update

from org_support import create_org, create_product
from probe_support import two_orgs
from sentinel.accounts.mail import Mail
from sentinel.db.base import utcnow
from sentinel.models import Component, Dependency, Product, Risk, Service, Snapshot, Source
from sentinel.risk.drift import latest_of, parse_pin, run_drift
from sentinel.sources.snapshots import snapshot_hash
from sentinel.tenancy.roles import Role
from sentinel.tenancy.scope import TenantScope
from sentinel.web.services import AppServices
from support import post_form, signed_in


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("^1.2.0", "1.2.0"),
        ("~=2.1", "2.1"),
        (">=3, <4", "3"),
        ("v1.0.0", "1.0.0"),
        ("2.0.0", "2.0.0"),
    ],
)
def test_parse_pin(raw: str, expected: str) -> None:
    pinned = parse_pin(raw)
    assert pinned is not None
    assert pinned.raw == expected


@pytest.mark.parametrize("raw", ["", "latest", None, 3])
def test_unusable_pins(raw: Any) -> None:
    assert parse_pin(raw) is None


def test_latest_prefers_the_registry_tag_over_newer_prereleases() -> None:
    items: list[dict[str, Any]] = [
        {"version": "4.0.0-beta.1"},
        {"version": "3.1.0", "latest": True, "link": "https://x.test/3.1.0"},
        {"version": "3.2.0"},  # newer, but the registry does not call it latest
    ]
    found = latest_of(items)
    assert found is not None
    assert (found[0].raw, found[1]) == ("3.1.0", "https://x.test/3.1.0")
    fallback = latest_of([{"version": "1.0.0"}, {"version": "1.1.0-rc.1", "latest": True}])
    assert fallback is not None
    assert fallback[0].raw == "1.0.0"
    assert latest_of([{"version": "nightly"}]) is None


async def shared_package(services: AppServices) -> uuid.UUID:
    """A shared package whose release source last saw 3.1.0 as latest."""
    items = [
        {"id": f"v:{v}", "version": v, "latest": v == "3.1.0", "link": f"https://x.test/{v}"}
        for v in ("3.1.0", "3.0.1", "2.4.0")
    ]
    async with services.db.system("test setup") as s:
        service = Service(slug="acme-client", name="Acme Client", kind="package", tags=[])
        s.add(service)
        await s.flush()
        component = Component(service_id=service.id, slug="client", name="Client", tags=[])
        s.add(component)
        await s.flush()
        source = Source(
            component_id=component.id,
            kind="release",
            identity="release:npm:acme-client",
            config={"ecosystem": "npm", "name": "acme-client"},
            interval_s=21600,
            timeout_s=10,
        )
        s.add(source)
        await s.flush()
        s.add(
            Snapshot(source_id=source.id, taken_at=utcnow(), hash=snapshot_hash(items), items=items)
        )
        return component.id


async def depend(
    services: AppServices, org_id: uuid.UUID, component_id: uuid.UUID, version: str
) -> uuid.UUID:
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


async def open_majors(services: AppServices) -> list[Risk]:
    async with services.db.system("test check") as s:
        return list(
            (
                await s.scalars(
                    select(Risk).where(Risk.kind == "unadopted_major", Risk.resolved_at.is_(None))
                )
            ).all()
        )


async def test_unadopted_major_lifecycle(services: AppServices) -> None:
    component = await shared_package(services)
    org_a, org_b = await two_orgs(services)
    behind = await depend(services, org_a, component, "2.4.0")
    await depend(services, org_b, component, "3.0.1")  # a minor behind: not a risk
    assert (await run_drift(services.db)).opened == 1
    (risk,) = await open_majors(services)
    assert (risk.org_id, risk.subject) == (org_a, str(behind))
    assert risk.detail["pinned"] == "2.4.0"
    assert risk.detail["latest"] == "3.1.0"
    assert risk.link == "https://x.test/3.1.0"
    assert (await run_drift(services.db)).opened == 0  # still open, not duplicated

    for org, count in ((org_a, 1), (org_b, 0)):
        scope = TenantScope(org_id=org, user_id=None, role=Role.VIEWER)
        assert len((await services.risks.open(scope, [component]))[component]) == count

    async with services.db.system("test change") as s:
        await s.execute(
            update(Dependency).where(Dependency.id == behind).values(usage={"version": "3.1.0"})
        )
    assert (await run_drift(services.db)).resolved == 1
    again = await depend(services, org_a, component, "1.0.0")
    await run_drift(services.db)
    async with services.db.system("test change") as s:
        await s.execute(delete(Dependency).where(Dependency.id == again))
    assert (await run_drift(services.db)).resolved == 1  # the dependency is gone
    assert await open_majors(services) == []


async def test_product_page_shows_drift(
    http: httpx2.AsyncClient, outbox: list[Mail], services: AppServices
) -> None:
    component = await shared_package(services)
    await signed_in(http, outbox, "alice@example.invalid")
    slug = await create_org(http, "Alpha")
    product = await create_product(http, slug)
    base = f"/o/{slug}/urunler/{product}"
    added = await post_form(
        http,
        f"{base}/bagimlilik",
        {"to_component_id": str(component), "version": "^2.4.0"},
        csrf_from=base,
    )
    assert added.status_code == 303
    page = await http.get(base)
    assert "Güncel" in page.text
    assert 'href="https://x.test/3.1.0"' in page.text
    assert "Büyük sürüm geride" in page.text
