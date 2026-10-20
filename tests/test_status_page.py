"""Vendor status pages: parsing, the adapter, vendor status, incidents, announced maintenance."""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any

import httpx
import httpx2
import pytest
from sqlalchemy import select

from org_support import create_org, create_service
from probe_support import env_for, job, outbound
from sentinel.accounts.mail import Mail
from sentinel.db.base import utcnow
from sentinel.engine.changes import run_changes
from sentinel.models import (
    Change,
    Component,
    MaintenanceWindow,
    Organization,
    Service,
    Snapshot,
    Source,
    VendorStatus,
)
from sentinel.sources.adapters import adapter_for
from sentinel.sources.snapshots import snapshot_hash
from sentinel.sources.statuspages import StatusPageError, parse_instatus, parse_statuspage
from sentinel.tenancy.roles import Role
from sentinel.tenancy.scope import TenantScope
from sentinel.web.services import AppServices
from support import signed_in

BASE = "https://status.vendor.test/"
NOW = utcnow().replace(microsecond=0)


def iso(delta: timedelta) -> str:
    return (NOW + delta).isoformat()


def summary(**overrides: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "page": {"id": "p", "name": "Vendor", "updated_at": iso(timedelta())},
        "status": {"indicator": "minor", "description": "Minor Service Outage"},
        "components": [
            {"id": "c1", "name": "API", "status": "degraded_performance", "updated_at": "x"},
            {"id": "c2", "name": "Portal", "status": "operational"},
            {"id": "g1", "name": "Region group", "status": "operational", "group": True},
        ],
        "incidents": [],
        "scheduled_maintenances": [
            {
                "id": "m1",
                "name": "Database upgrade",
                "status": "scheduled",
                "impact": "maintenance",
                "scheduled_for": iso(timedelta(days=1)),
                "scheduled_until": iso(timedelta(days=1, hours=2)),
                "shortlink": "https://stspg.example.invalid/m1",
                "components": [{"name": "API"}],
            }
        ],
    }
    data.update(overrides)
    return data


def test_statuspage_parsing_keeps_meaning_and_drops_noise() -> None:
    items = parse_statuspage(summary(), BASE)
    by_id = {i["id"]: i for i in items}
    assert by_id["page"]["indicator"] == "minor"
    assert by_id["c:c1"]["status"] == "degraded"
    assert "c:g1" not in by_id  # groups only aggregate
    window = by_id["m:m1"]
    assert (window["type"], window["components"]) == ("maintenance", ["API"])
    assert window["link"] == "https://stspg.example.invalid/m1"
    touched = summary(page={"id": "p", "name": "Vendor", "updated_at": "later"})
    assert snapshot_hash(parse_statuspage(touched, BASE)) == snapshot_hash(items)
    with pytest.raises(StatusPageError):
        parse_statuspage({"page": {}}, BASE)


def test_instatus_parsing() -> None:
    data = {
        "page": {"name": "Vendor", "url": BASE, "status": "HASISSUES"},
        "activeIncidents": [
            {
                "id": "i1",
                "name": "Login errors",
                "started": iso(timedelta()),
                "status": "INVESTIGATING",
                "impact": "PARTIALOUTAGE",
                "url": "https://status.vendor.test/i1",
            }
        ],
        "activeMaintenances": [],
    }
    items = parse_instatus(data, BASE)
    assert items[0] == {"id": "page", "type": "page", "indicator": "minor"}
    assert items[1]["status"] == "investigating"
    with pytest.raises(StatusPageError):
        parse_instatus([], BASE)


async def test_adapter_fetches_the_provider_endpoint() -> None:
    requested: list[str] = []

    def vendor(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        requested.append(request.url.path)
        return httpx.Response(200, json=summary(), headers={"ETag": '"s1"'})

    adapter = adapter_for("status_page")
    assert adapter is not None
    async with outbound(vendor, rate_per_s=100, burst=10) as http:
        outcome = await adapter.run(
            job(kind="status_page", url=BASE, config={"url": BASE, "provider": "statuspage"}),
            env_for(http),
        )
        broken = await adapter.run(
            job(kind="status_page", url=BASE, config={"url": BASE, "provider": "instatus"}),
            env_for(http),
        )
    assert requested == ["/api/v2/summary.json", "/summary.json"]
    assert outcome.ok
    assert outcome.snapshot is not None
    assert outcome.snapshot.validators == {"etag": '"s1"'}
    assert broken.error_class == "parse_error"  # a Statuspage body is not an Instatus one


# --- the engine ----------------------------------------------------------------------


async def vendor_service(services: AppServices) -> tuple[Source, dict[str, uuid.UUID]]:
    async with services.db.system("test setup") as s:
        service = Service(slug="vendor", name="Vendor", kind="other", tags=[])
        s.add(service)
        await s.flush()
        components = {}
        for slug in ("api", "portal", "status"):
            component = Component(service_id=service.id, slug=slug, name=slug.title(), tags=[])
            s.add(component)
            await s.flush()
            components[slug] = component.id
        source = Source(
            component_id=components["status"],
            kind="status_page",
            identity=f"status_page:{BASE}",
            config={
                "url": BASE,
                "provider": "statuspage",
                "component_map": {"API": "api", "Portal": "portal"},
            },
            interval_s=300,
            timeout_s=10,
        )
        s.add(source)
        return source, components


async def snapshot(
    services: AppServices, source: Source, data: dict[str, Any], at: timedelta
) -> None:
    items = parse_statuspage(data, BASE)
    async with services.db.system("test setup") as s:
        s.add(
            Snapshot(source_id=source.id, taken_at=NOW + at, hash=snapshot_hash(items), items=items)
        )


async def test_vendor_status_changes_and_announced_maintenance(services: AppServices) -> None:
    source, components = await vendor_service(services)
    await snapshot(services, source, summary(), timedelta())
    assert (await run_changes(services.db)).changes == 0  # baseline
    async with services.db.system("test check") as s:
        vendor = await s.get(VendorStatus, source.id)
        assert vendor is not None
        assert vendor.indicator == "minor"
        assert vendor.components == {
            str(components["api"]): "degraded",
            str(components["portal"]): "operational",
        }
        window = await s.scalar(select(MaintenanceWindow))
        assert window is not None  # announced a day ahead: a real window
        assert (window.component_id, window.origin, window.org_id) == (
            components["api"],
            "announcement",
            None,
        )

    first_window = summary()["scheduled_maintenances"][0]
    later = summary(
        incidents=[
            {
                "id": "i1",
                "name": "Portal login errors",
                "status": "investigating",
                "impact": "major",
                "created_at": iso(timedelta(minutes=50)),
                "components": [{"name": "Portal"}],
            }
        ],
        scheduled_maintenances=[
            {
                **first_window,
                "scheduled_for": iso(timedelta(days=2)),
                "scheduled_until": iso(timedelta(days=2, hours=1)),
            },
            {
                "id": "m2",
                "name": "Emergency patch",
                "status": "in_progress",
                "scheduled_for": iso(timedelta(minutes=30)),
                "scheduled_until": iso(timedelta(hours=3)),
                "components": [{"name": "API"}],
            },
        ],
    )
    await snapshot(services, source, later, timedelta(hours=1))
    assert (await run_changes(services.db)).changes == 2
    async with services.db.system("test check") as s:
        changes = {(c.kind, c.component_id) for c in (await s.scalars(select(Change))).all()}
        windows = (await s.scalars(select(MaintenanceWindow))).all()
    assert changes == {
        ("vendor_incident", components["portal"]),
        ("vendor_maintenance", components["api"]),
    }
    # the emergency patch was first seen after it started: an announcement, not a window
    (moved,) = windows
    assert moved.starts_at == NOW + timedelta(days=2)  # rescheduled before it started

    async with services.db.system("test setup") as s:
        org = Organization(slug="a", name="A")
        s.add(org)
        await s.flush()
    scope = TenantScope(org_id=org.id, user_id=None, role=Role.VIEWER)
    claims = await services.changes.vendor(scope, list(components.values()))
    assert claims[components["api"]].status == "degraded"
    assert claims[components["status"]].status is None  # the page itself: indicator only
    assert claims[components["status"]].indicator == "minor"
    assert claims[components["portal"]].open_incidents == 1


async def test_service_page_shows_the_vendor_claim_next_to_ours(
    http: httpx2.AsyncClient, outbox: list[Mail], services: AppServices
) -> None:
    await signed_in(http, outbox, "alice@example.invalid")
    slug = await create_org(http, "Alpha")
    service = await create_service(http, slug)
    async with services.db.system("test setup") as s:
        component = await s.scalar(
            select(Component).where(Component.service_id == uuid.UUID(service))
        )
        assert component is not None
        source = Source(
            org_id=component.org_id,
            component_id=component.id,
            kind="status_page",
            identity=f"status_page:{BASE}",
            config={"url": BASE, "provider": "statuspage"},
            interval_s=300,
            timeout_s=10,
        )
        s.add(source)
        await s.flush()
        s.add(
            VendorStatus(
                org_id=component.org_id,
                source_id=source.id,
                indicator="major",
                components={},
                open_incidents=2,
                observed_at=NOW,
            )
        )
    page = await http.get(f"/o/{slug}/servisler/{service}")
    assert "Sağlayıcının beyanı" in page.text
    assert "Büyük bir sorun bildiriliyor" in page.text
    assert "Açık olay: 2" in page.text
