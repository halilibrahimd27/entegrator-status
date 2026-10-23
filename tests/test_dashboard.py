"""The organization dashboard: derived product states, filters, one line per subject."""

from __future__ import annotations

import uuid
from datetime import timedelta

import httpx2

from org_support import create_org, create_product
from probe_support import two_orgs
from sentinel.accounts.mail import Mail
from sentinel.db.base import utcnow
from sentinel.models import (
    Change,
    Incident,
    IncidentKind,
    IncidentStatus,
    Risk,
)
from sentinel.risk.dashboard import derive
from sentinel.risk.impact import Impact
from sentinel.tenancy.roles import Role
from sentinel.tenancy.scope import TenantScope
from sentinel.web.services import AppServices
from support import post_form, signed_in
from test_impact import chain, product_on

NOW = utcnow()


def impact(kind: str, depth: int, severity: str) -> Impact:
    return Impact(
        kind, uuid.uuid4(), uuid.uuid4(), depth, ("P", "X"), severity, "upstream_down", "", NOW
    )


def test_derived_state() -> None:
    assert derive([], own_incident=False) == ("ok", None)
    assert derive([impact("change", 1, "medium")], own_incident=False) == ("ok", None)
    serious = impact("risk", 1, "high")
    assert derive([serious], own_incident=False) == ("at_risk", serious)
    far = impact("incident", 2, "medium")
    assert derive([far, serious], own_incident=False) == ("at_risk", far)
    near = impact("incident", 1, "high")
    assert derive([far, near], own_incident=False) == ("affected", near)
    assert derive([near], own_incident=True) == ("own_incident", None)


async def test_dashboard_orders_products_and_dedupes_subjects(services: AppServices) -> None:
    ids = await chain(services)
    org_a, org_b = await two_orgs(services)
    direct, _ = await product_on(services, org_a, ids["central"], {})
    indirect, _ = await product_on(services, org_a, ids["api"], {})
    calm, _ = await product_on(services, org_a, ids["ca"], {})
    other, _ = await product_on(services, org_b, ids["central"], {})
    async with services.db.system("test setup") as s:
        s.add(
            Incident(
                kind=IncidentKind.AUTO,
                status=IncidentStatus.OPEN,
                component_id=ids["central"],
                started_at=NOW,
                cause="read_timeout",
            )
        )
        s.add(
            Change(
                component_id=ids["central"],
                kind="release",
                category="major",
                title="v3.0.0",
                detected_at=NOW - timedelta(days=1),
                fingerprint=uuid.uuid4().hex * 2,
            )
        )
        s.add(
            Change(
                component_id=ids["central"],
                kind="release",
                category="major",
                title="v2.0.0",
                detected_at=NOW - timedelta(days=9),  # older than a week
                fingerprint=uuid.uuid4().hex * 2,
            )
        )
        s.add(
            Risk(
                component_id=ids["ca"],
                kind="cert_expiry",
                due_at=NOW + timedelta(days=10),
                first_seen_at=NOW,
                last_seen_at=NOW,
            )
        )
        s.add(
            Incident(
                kind=IncidentKind.MANUAL,
                status=IncidentStatus.INVESTIGATING,
                product_id=calm,
                org_id=org_a,
                title="Yavaşlık",
                started_at=NOW,
            )
        )
    scope = TenantScope(org_id=org_a, user_id=None, role=Role.VIEWER)
    board = await services.dashboard.build(scope)
    states = [(p.id, p.state) for p in board.products]
    assert states == [(calm, "own_incident"), (direct, "affected"), (indirect, "at_risk")]
    assert other not in {p.id for p in board.products}  # another tenant's product
    assert len(board.incidents) == 1  # one line, although three products are reached
    assert board.incidents[0].depth == 1  # the closest reach is the one shown
    assert [c.title for c in board.changes] == ["v3.0.0"]
    assert [e.risk.kind for e in board.due] == ["cert_expiry"]
    assert not board.calm

    only = await services.dashboard.build(scope, product_id=indirect, min_severity="high")
    assert [p.id for p in only.products] == [indirect]
    assert only.incidents == []  # medium at depth 2: below the filter
    assert {name for _, name in only.choices} == {"Fatura modülü"}


async def test_dashboard_page(
    http: httpx2.AsyncClient, outbox: list[Mail], services: AppServices
) -> None:
    ids = await chain(services)
    await signed_in(http, outbox, "alice@example.invalid")
    slug = await create_org(http, "Alpha")
    empty = await http.get(f"/o/{slug}")
    assert "Henüz ürün yok." in empty.text
    product = await create_product(http, slug, "Fatura <script>alert(1)</script>")
    base = f"/o/{slug}/urunler/{product}"
    await post_form(
        http, f"{base}/bagimlilik", {"to_component_id": str(ids["api"])}, csrf_from=base
    )
    calm = await http.get(f"/o/{slug}")
    assert "bir şey görünmüyor" in calm.text
    assert "<script>alert(1)</script>" not in calm.text  # escaped
    assert "Fatura &lt;script&gt;" in calm.text
    async with services.db.system("test setup") as s:
        s.add(
            Incident(
                kind=IncidentKind.AUTO,
                status=IncidentStatus.OPEN,
                component_id=ids["api"],
                started_at=NOW,
                cause="read_timeout",
            )
        )
    page = await http.get(f"/o/{slug}")
    assert "Etkileniyor" in page.text
    assert "Bağımlılıklardaki açık erişim sorunları" in page.text
    filtered = await http.get(f"/o/{slug}?urun={product}&onem=high")
    assert filtered.status_code == 200
    for junk in ("?urun=not-a-uuid", "?onem=extreme", f"?urun={uuid.uuid4()}"):
        assert (await http.get(f"/o/{slug}{junk}")).status_code == 200


async def test_api_v1_read_endpoints(services: AppServices, http: httpx2.AsyncClient) -> None:
    ids = await chain(services)
    org_a, org_b = await two_orgs(services)
    product, _ = await product_on(services, org_a, ids["central"], {})
    await product_on(services, org_b, ids["central"], {})
    async with services.db.system("test setup") as s:
        s.add(
            Incident(
                kind=IncidentKind.AUTO,
                status=IncidentStatus.OPEN,
                component_id=ids["central"],
                started_at=NOW,
            )
        )
        s.add(
            Risk(
                component_id=ids["ca"],
                kind="cert_expiry",
                due_at=NOW + timedelta(days=10),
                first_seen_at=NOW,
                last_seen_at=NOW,
            )
        )
    scope = TenantScope(org_id=org_a, user_id=None, role=Role.ADMIN)
    raw, _ = await services.api_tokens.create(scope, "ci", 30, "")
    headers = {"Authorization": f"Bearer {raw}"}

    products = (await http.get("/api/v1/products", headers=headers)).json()["products"]
    assert [(p["product_id"], p["state"]) for p in products] == [(str(product), "affected")]
    assert products[0]["reason"]["path"][-1] == "GİB — Merkezi e-Fatura"

    impacts = (await http.get(f"/api/v1/impacts?product={product}", headers=headers)).json()
    kinds = sorted(i["type"] for i in impacts["products"][str(product)])
    assert kinds == ["incident", "risk"]
    assert (await http.get(f"/api/v1/impacts?product={uuid.uuid4()}", headers=headers)).json() == {
        "products": {}
    }
    risks = (await http.get("/api/v1/risks", headers=headers)).json()["risks"]
    assert [(r["kind"], r["state"]) for r in risks] == [("cert_expiry", "open")]
    items = (await http.get("/api/v1/calendar", headers=headers)).json()["items"]
    assert [i["type"] for i in items] == ["risk"]
    for path in ("/api/v1/products", "/api/v1/impacts", "/api/v1/risks", "/api/v1/calendar"):
        assert (await http.get(path)).status_code == 401
