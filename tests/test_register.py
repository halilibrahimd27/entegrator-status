"""Risk register (per-organization decisions) and the planned-changes calendar."""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any

import httpx2
import pytest
from fastapi import FastAPI
from sqlalchemy import select

from org_support import create_org, create_product, join
from probe_support import two_orgs
from sentinel.accounts.mail import Mail
from sentinel.db.base import utcnow
from sentinel.models import (
    AuditEvent,
    Change,
    Component,
    Dependency,
    MaintenanceWindow,
    Product,
    Risk,
    Service,
)
from sentinel.risk.register import RegisterError, RiskRegister
from sentinel.tenancy.roles import Role
from sentinel.tenancy.scope import TenantScope
from sentinel.web.services import AppServices
from support import post_form, signed_in

NOW = utcnow()


async def shared_component(services: AppServices) -> uuid.UUID:
    async with services.db.system("test setup") as s:
        service = Service(slug=f"v-{uuid.uuid4().hex[:6]}", name="Vendor", kind="other", tags=[])
        s.add(service)
        await s.flush()
        component = Component(service_id=service.id, slug="api", name="API", tags=[])
        s.add(component)
        await s.flush()
        return component.id


async def depend(
    services: AppServices, org_id: uuid.UUID, component_id: uuid.UUID, name: str
) -> None:
    async with services.db.system("test setup") as s:
        product = Product(org_id=org_id, slug=f"p-{uuid.uuid4().hex[:6]}", name=name)
        s.add(product)
        await s.flush()
        s.add(
            Dependency(
                org_id=org_id, from_product_id=product.id, to_component_id=component_id, usage={}
            )
        )


async def risk(
    services: AppServices, component_id: uuid.UUID, kind: str, due: timedelta | None, **extra: Any
) -> uuid.UUID:
    async with services.db.system("test setup") as s:
        row = Risk(
            component_id=component_id,
            kind=kind,
            due_at=NOW + due if due else None,
            first_seen_at=NOW,
            last_seen_at=NOW,
            **extra,
        )
        s.add(row)
        await s.flush()
        return row.id


def scope(org_id: uuid.UUID, role: Role = Role.MEMBER) -> TenantScope:
    return TenantScope(org_id=org_id, user_id=None, role=role)


async def test_register_order_and_per_org_decisions(services: AppServices) -> None:
    component = await shared_component(services)
    org_a, org_b = await two_orgs(services)
    await depend(services, org_a, component, "Fatura")
    await depend(services, org_a, component, "Mutabakat")
    await depend(services, org_b, component, "B ürünü")
    later = await risk(services, component, "eol", timedelta(days=120))
    soon = await risk(services, component, "sunset", timedelta(days=10))
    undated = await risk(services, component, "deprecation", None)

    entries = await services.register.entries(scope(org_a))
    assert [e.risk.id for e in entries] == [soon, later, undated]  # soonest first, undated last
    assert entries[0].severity == "high"
    assert entries[0].products == ["Fatura", "Mutabakat"]
    assert entries[0].component == "Vendor — API"

    await services.register.decide(scope(org_a), soon, "accepted", note="Yeni uca geçiş planlandı")
    await services.register.decide(scope(org_a), later, "snoozed", until=NOW + timedelta(days=7))
    states = {e.risk.id: (e.state, e.note) for e in await services.register.entries(scope(org_a))}
    assert states[soon] == ("accepted", "Yeni uca geçiş planlandı")
    assert states[later][0] == "snoozed"
    order = [e.risk.id for e in await services.register.entries(scope(org_a))]
    assert order == [undated, later, soon]  # open first, then snoozed, then accepted
    assert {e.state for e in await services.register.entries(scope(org_b))} == {"open"}  # B's own

    after = RiskRegister(services.db, services.impact, clock=lambda: NOW + timedelta(days=8))
    states_later = {e.risk.id: e.state for e in await after.entries(scope(org_a))}
    assert states_later[later] == "open"  # the snooze ran out

    await services.register.decide(scope(org_a), soon, "open")
    reopened = {e.risk.id: e.state for e in await services.register.entries(scope(org_a))}
    assert reopened[soon] == "open"
    async with services.db.system("test check") as s:
        actions = set(
            (await s.scalars(select(AuditEvent.action).where(AuditEvent.org_id == org_a))).all()
        )
    assert {"risk.accepted", "risk.snoozed", "risk.reopened"} <= actions


@pytest.mark.parametrize(
    ("decision", "kwargs", "key"),
    [
        ("accepted", {}, "register.note_required"),
        ("snoozed", {}, "register.invalid_until"),
        ("snoozed", {"until": NOW - timedelta(days=1)}, "register.invalid_until"),
        ("snoozed", {"until": NOW + timedelta(days=400)}, "register.invalid_until"),
        ("ignored", {}, "register.invalid_decision"),
    ],
)
async def test_invalid_decisions(
    services: AppServices, decision: str, kwargs: dict[str, Any], key: str
) -> None:
    component = await shared_component(services)
    org_a, _ = await two_orgs(services)
    risk_id = await risk(services, component, "sunset", timedelta(days=5))
    with pytest.raises(RegisterError, match=key.replace(".", r"\.")):
        await services.register.decide(scope(org_a), risk_id, decision, **kwargs)
    with pytest.raises(RegisterError, match="not_found"):
        await services.register.decide(scope(org_a), uuid.uuid4(), "open")


async def test_calendar(services: AppServices) -> None:
    component = await shared_component(services)
    org_a, _ = await two_orgs(services)
    await depend(services, org_a, component, "Fatura")
    accepted = await risk(services, component, "cert_expiry", timedelta(days=5))
    await services.register.decide(scope(org_a), accepted, "accepted", note="Yenileniyor")
    await risk(services, component, "sunset", timedelta(days=10))
    await risk(services, component, "eol", timedelta(days=400))  # beyond the horizon
    async with services.db.system("test setup") as s:
        s.add(
            Change(
                component_id=component,
                kind="announcement",
                category="package_update",
                title="UBL-TR 1.3 zorunlu",
                detected_at=NOW,
                effective_at=NOW + timedelta(days=20),
                fingerprint=uuid.uuid4().hex * 2,
            )
        )
        s.add(
            MaintenanceWindow(
                component_id=component,
                title="Platform bakımı",
                starts_at=NOW + timedelta(days=3),
                ends_at=NOW + timedelta(days=3, hours=2),
            )
        )
        s.add(
            MaintenanceWindow(
                org_id=org_a,
                component_id=component,
                title="Sahte",
                starts_at=NOW + timedelta(days=4),
                ends_at=NOW + timedelta(days=4, hours=1),
            )
        )
    items = await services.register.calendar(scope(org_a))
    assert [(i.kind, i.title) for i in items] == [
        (
            "maintenance",
            "Platform bakımı",
        ),  # the organization's window on a shared component does not count
        ("risk", "sunset"),  # the accepted certificate risk is left out
        ("change", "UBL-TR 1.3 zorunlu"),
    ]
    assert all(i.component == "Vendor — API" for i in items)


async def test_pages_and_permissions(
    app: FastAPI, http: httpx2.AsyncClient, outbox: list[Mail], services: AppServices
) -> None:
    component = await shared_component(services)
    await signed_in(http, outbox, "alice@example.invalid")
    slug = await create_org(http, "Alpha")
    product = await create_product(http, slug, "Fatura")
    base = f"/o/{slug}/urunler/{product}"
    await post_form(http, f"{base}/bagimlilik", {"to_component_id": str(component)}, csrf_from=base)
    risk_id = await risk(services, component, "sunset", timedelta(days=10))
    url = f"/o/{slug}/riskler"
    page = await http.get(url)
    assert "kapanış tarihini bildirdi" in page.text
    assert "Fatura" in page.text
    refused = await post_form(
        http, f"{url}/{risk_id}/karar", {"decision": "accepted", "note": ""}, csrf_from=url
    )
    assert refused.status_code == 400
    assert "Kabul ederken bir not yaz" in refused.text
    snooze = (NOW + timedelta(days=7)).date().isoformat()
    done = await post_form(
        http, f"{url}/{risk_id}/karar", {"decision": "snoozed", "until": snooze}, csrf_from=url
    )
    assert done.status_code == 303
    assert "Ertelendi" in (await http.get(url)).text
    assert "Planlı değişiklikler" in (await http.get(f"/o/{slug}/takvim")).text
    async with join(app, outbox, http, slug, "viewer@example.invalid", role="viewer") as viewer:
        assert "Karar ver" not in (await viewer.get(url)).text
        denied = await post_form(
            viewer, f"{url}/{risk_id}/karar", {"decision": "open"}, csrf_from=url
        )
        assert denied.status_code == 403
