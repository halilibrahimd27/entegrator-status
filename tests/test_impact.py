"""Impact propagation: two and three hops, usage-aware severity, owner-only risks."""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any

import httpx2
import pytest

from org_support import create_org, create_product
from probe_support import two_orgs
from sentinel.accounts.mail import Mail
from sentinel.db.base import utcnow
from sentinel.models import (
    Change,
    Component,
    Dependency,
    Incident,
    IncidentKind,
    IncidentStatus,
    Product,
    Risk,
    Service,
)
from sentinel.risk.impact import applies, attenuate, change_rule, risk_rule
from sentinel.tenancy.roles import Role
from sentinel.tenancy.scope import TenantScope
from sentinel.web.services import AppServices
from support import post_form, signed_in

NOW = utcnow()


def test_attenuation() -> None:
    assert [attenuate(2, d) for d in (1, 2, 3)] == [2, 1, 0]
    assert attenuate(0, 3) == 0


@pytest.mark.parametrize(
    ("kind", "category", "detail", "depth", "usage", "expected"),
    [
        (
            "contract",
            "breaking",
            {"operations": ["POST /invoices"]},
            1,
            ["POST /invoices"],
            (2, "used_operation_breaking"),
        ),
        (
            "contract",
            "breaking",
            {"operations": ["GET /status"]},
            1,
            ["POST /invoices"],
            (0, "unused_operation"),
        ),
        (
            "contract",
            "breaking",
            {"operations": ["GET /status"]},
            1,
            None,
            (1, "possibly_affected"),
        ),
        (
            "contract",
            "breaking",
            {"operations": ["POST /invoices"]},
            2,
            ["POST /invoices"],
            (1, "possibly_affected"),
        ),
        ("contract", "non_breaking", {}, 1, None, (0, "contract_non_breaking")),
        ("contract", "cosmetic", {}, 1, None, None),
        ("release", "major", {}, 1, None, (1, "major_release")),
        ("release", "patch", {"breaking_notes": True}, 1, None, (1, "major_release")),
        ("release", "prerelease", {}, 1, None, None),
        ("announcement", "security", {}, 1, None, (2, "security_notice")),
        ("vendor_incident", "incident", {}, 1, None, (1, "vendor_reported_incident")),
    ],
)
def test_change_rules(
    kind: str,
    category: str,
    detail: dict[str, Any],
    depth: int,
    usage: list[str] | None,
    expected: Any,
) -> None:
    change = Change(kind=kind, category=category, detail=detail)
    assert change_rule(change, depth, usage) == expected


@pytest.mark.parametrize(
    ("kind", "due", "detail", "expected"),
    [
        ("advisory", None, {"severity": "CRITICAL"}, (2, "advisory")),
        ("advisory", None, {"severity": "LOW"}, (1, "advisory")),
        ("sunset", timedelta(days=10), {}, (2, "due_soon")),
        ("cert_expiry", timedelta(days=-1), {}, (2, "due_soon")),
        ("eol", timedelta(days=120), {}, (1, "due_later")),
        ("deprecation", None, {}, (1, "deprecated")),
        ("eol", None, {}, (1, "ended")),
        ("unadopted_major", None, {}, (1, "unadopted_major")),
    ],
)
def test_risk_rules(
    kind: str, due: timedelta | None, detail: dict[str, Any], expected: Any
) -> None:
    risk = Risk(kind=kind, due_at=NOW + due if due is not None else None, detail=detail)
    assert risk_rule(risk, NOW) == expected


def test_pinned_version_risks_apply_to_their_owner_only() -> None:
    mine, other = str(uuid.uuid4()), str(uuid.uuid4())
    assert applies(Risk(subject=""), {mine})
    assert applies(Risk(subject="GHSA-1|source"), {mine})
    assert applies(Risk(subject=f"GHSA-1|{mine}"), {mine})
    assert not applies(Risk(subject=f"GHSA-1|{other}"), {mine})
    assert applies(Risk(subject=mine), {mine})  # unadopted major: the subject is the dependency


async def chain(services: AppServices) -> dict[str, uuid.UUID]:
    """Product → Integrator API → Central e-Invoice → Certificate authority (shared edges)."""
    ids: dict[str, uuid.UUID] = {}
    async with services.db.system("test setup") as s:
        for key, service_name, component_name in (
            ("api", "Entegratör X", "e-Fatura API"),
            ("central", "GİB", "Merkezi e-Fatura"),
            ("ca", "Sertifika Sağlayıcı", "OCSP"),
        ):
            service = Service(slug=f"s-{key}", name=service_name, kind="other", tags=[])
            s.add(service)
            await s.flush()
            component = Component(service_id=service.id, slug=key, name=component_name, tags=[])
            s.add(component)
            await s.flush()
            ids[key] = component.id
        s.add_all(
            [
                Dependency(from_component_id=ids["api"], to_component_id=ids["central"], usage={}),
                Dependency(from_component_id=ids["central"], to_component_id=ids["ca"], usage={}),
            ]
        )
    return ids


async def product_on(
    services: AppServices, org_id: uuid.UUID, component_id: uuid.UUID, usage: dict[str, Any]
) -> tuple[uuid.UUID, uuid.UUID]:
    async with services.db.system("test setup") as s:
        product = Product(org_id=org_id, slug=f"p-{uuid.uuid4().hex[:6]}", name="Fatura modülü")
        s.add(product)
        await s.flush()
        dependency = Dependency(
            org_id=org_id, from_product_id=product.id, to_component_id=component_id, usage=usage
        )
        s.add(dependency)
        await s.flush()
        return product.id, dependency.id


async def test_two_and_three_hops_with_reasons(services: AppServices) -> None:
    ids = await chain(services)
    org_a, org_b = await two_orgs(services)
    product, dependency = await product_on(
        services, org_a, ids["api"], {"operations": ["POST /invoices"]}
    )
    unknown, _ = await product_on(services, org_b, ids["api"], {})

    def change(component: str, ops: list[str]) -> Change:
        return Change(
            component_id=ids[component],
            kind="contract",
            category="breaking",
            title="openapi",
            detected_at=NOW - timedelta(hours=1),
            fingerprint=uuid.uuid4().hex * 2,
            detail={"operations": ops},
        )

    async with services.db.system("test setup") as s:
        for component in ("central", "ca"):
            s.add(
                Incident(
                    kind=IncidentKind.AUTO,
                    status=IncidentStatus.OPEN,
                    component_id=ids[component],
                    started_at=NOW,
                    cause="read_timeout",
                )
            )
        s.add_all([change("api", ["POST /invoices"]), change("api", ["GET /status"])])
        s.add_all(
            [
                Risk(
                    component_id=ids["api"],
                    kind="advisory",
                    subject=f"GHSA-1|{dependency}",
                    detail={"severity": "HIGH"},
                    first_seen_at=NOW,
                    last_seen_at=NOW,
                ),
                Risk(
                    component_id=ids["api"],
                    kind="advisory",
                    subject=f"GHSA-2|{uuid.uuid4()}",
                    detail={"severity": "HIGH"},
                    first_seen_at=NOW,
                    last_seen_at=NOW,
                ),
            ]
        )

    scope = TenantScope(org_id=org_a, user_id=None, role=Role.VIEWER)
    found = await services.impact.for_product(scope, product)
    summary = sorted((i.kind, i.depth, i.severity, i.reason) for i in found)
    assert summary == sorted(
        [
            ("incident", 2, "medium", "upstream_down"),  # second degree: one level lower
            ("incident", 3, "low", "upstream_down"),  # third degree
            ("change", 1, "high", "used_operation_breaking"),
            ("change", 1, "low", "unused_operation"),
            ("risk", 1, "high", "advisory"),  # only the advisory on this product's pin
        ]
    )
    third = next(i for i in found if i.depth == 3)
    assert third.path == (
        "Fatura modülü",
        "Entegratör X — e-Fatura API",
        "GİB — Merkezi e-Fatura",
        "Sertifika Sağlayıcı — OCSP",
    )
    assert found[0].severity == "high"  # worst first

    other = await services.impact.for_product(
        TenantScope(org_id=org_b, user_id=None, role=Role.VIEWER), unknown
    )
    changes = sorted(i.reason for i in other if i.kind == "change")
    assert changes == ["possibly_affected", "possibly_affected"]  # no usage data: hedged
    assert all(i.kind != "risk" for i in other)  # org A's pinned-version risk is not org B's
    assert set((await services.impact.for_org(scope)).keys()) == {product}


async def test_product_page_lists_impacts(
    http: httpx2.AsyncClient, outbox: list[Mail], services: AppServices
) -> None:
    ids = await chain(services)
    await signed_in(http, outbox, "alice@example.invalid")
    slug = await create_org(http, "Alpha")
    product = await create_product(http, slug, "Fatura modülü")
    base = f"/o/{slug}/urunler/{product}"
    await post_form(
        http, f"{base}/bagimlilik", {"to_component_id": str(ids["api"])}, csrf_from=base
    )
    assert "Şu an ürününü etkileyen bir şey görünmüyor" in (await http.get(base)).text
    async with services.db.system("test setup") as s:
        s.add(
            Incident(
                kind=IncidentKind.AUTO,
                status=IncidentStatus.OPEN,
                component_id=ids["central"],
                started_at=NOW,
            )
        )
    page = await http.get(base)
    assert "Bağımlılıkta erişim sorunu" in page.text
    assert "Fatura modülü → Entegratör X — e-Fatura API → GİB — Merkezi e-Fatura" in page.text
