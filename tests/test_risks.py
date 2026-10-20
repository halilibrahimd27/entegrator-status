"""Risks from Sunset / Deprecation headers and from expiring certificates and domains."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx2
import pytest
from sqlalchemy import select

from org_support import create_org, create_service
from probe_support import add_source, two_orgs
from sentinel.accounts.mail import Mail
from sentinel.db.base import utcnow
from sentinel.models import Component, Observation, Risk, Source
from sentinel.risk.headers import deprecation, explanation, sunset
from sentinel.risk.scan import findings, run_risks
from sentinel.tenancy.roles import Role
from sentinel.tenancy.scope import TenantScope
from sentinel.web.services import AppServices
from support import signed_in

BASE = "https://api.vendor.test/v1/"


def test_header_parsing() -> None:
    assert sunset("Sat, 31 Dec 2026 23:59:59 GMT") == datetime(2026, 12, 31, 23, 59, 59, tzinfo=UTC)
    assert sunset("yakında") is None
    assert sunset(None) is None
    assert deprecation("@1798761600") == (True, datetime(2027, 1, 1, tzinfo=UTC))
    assert deprecation("true") == (True, None)
    assert deprecation("Wed, 01 Jul 2026 00:00:00 GMT") == (True, datetime(2026, 7, 1, tzinfo=UTC))
    assert deprecation("perhaps") == (False, None)
    assert deprecation(None) == (False, None)
    link = '<https://docs.vendor.test/v1-sunset>; rel="sunset", <https://x.test>; rel="next"'
    assert explanation(link, BASE) == "https://docs.vendor.test/v1-sunset"
    assert explanation('<javascript:alert(1)>; rel="deprecation"', BASE) is None
    assert explanation(None, BASE) is None


END_OF_YEAR = "Sat, 31 Dec 2026 23:59:59 GMT"
EXPIRING_CERT = {"expiring_soon": True, "not_after": "2026-11-01T00:00:00+00:00", "days_left": 12}
EXPIRING_DOMAIN = {"expiring_soon": True, "expires_at": "2026-11-15T00:00:00+00:00"}


@pytest.mark.parametrize(
    ("kind", "details", "expected"),
    [
        ("http", {"sunset": END_OF_YEAR}, ["sunset"]),
        ("http", {"deprecation": "true", "sunset": END_OF_YEAR}, ["sunset", "deprecation"]),
        ("http", {}, []),
        ("tls_cert", EXPIRING_CERT, ["cert_expiry"]),
        ("tls_cert", {"expiring_soon": False, "days_left": 80}, []),
        ("domain_expiry", EXPIRING_DOMAIN, ["domain_expiry"]),
    ],
)
def test_findings(kind: str, details: dict[str, Any], expected: list[str]) -> None:
    assert [f.kind.value for f in findings(kind, details, BASE)] == expected


async def observe(services: AppServices, source_id: uuid.UUID, **values: Any) -> None:
    node = (await services.nodes.register(f"n-{uuid.uuid4().hex[:6]}", "test")).node_id
    async with services.db.system("test setup") as s:
        source = await s.get(Source, source_id)
        assert source is not None
        s.add(
            Observation(
                org_id=source.org_id,
                source_id=source_id,
                node_id=node,
                observed_at=values.pop("at", utcnow()),
                ok=values.pop("ok", True),
                status_code=200,
                details=values.pop("details", {}),
            )
        )


async def open_risks(services: AppServices) -> list[Risk]:
    async with services.db.system("test check") as s:
        return list((await s.scalars(select(Risk).where(Risk.resolved_at.is_(None)))).all())


async def test_sunset_lifecycle(services: AppServices) -> None:
    source = await add_source(services, None, BASE)
    first = "Sat, 31 Dec 2026 23:59:59 GMT"
    link = '<https://docs.vendor.test/sunset>; rel="sunset"'
    await observe(
        services,
        source,
        details={"sunset": first, "link": link},
        at=utcnow() - timedelta(minutes=9),
    )
    assert (await run_risks(services.db)).opened == 1
    (risk,) = await open_risks(services)
    assert (risk.kind, risk.due_at, risk.link) == (
        "sunset",
        sunset(first),
        "https://docs.vendor.test/sunset",
    )

    moved = "Wed, 30 Jun 2027 23:59:59 GMT"
    await observe(services, source, details={"sunset": moved}, at=utcnow() - timedelta(minutes=6))
    assert (await run_risks(services.db)).updated == 1
    (risk,) = await open_risks(services)
    assert risk.due_at == sunset(moved)

    await observe(services, source, ok=False, at=utcnow() - timedelta(minutes=3))
    assert (await run_risks(services.db)).resolved == 0  # an outage says nothing about the sunset
    await observe(services, source, details={}, at=utcnow())
    assert (await run_risks(services.db)).resolved == 1
    assert await open_risks(services) == []


async def test_expired_certificate_is_a_risk_and_renewal_resolves_it(services: AppServices) -> None:
    source_id = await add_source(services, None, BASE)
    async with services.db.system("test setup") as s:
        source = await s.get(Source, source_id)
        assert source is not None
        source.kind, source.config = "tls_cert", {"host": "api.vendor.test", "port": 443}
        component_id = source.component_id
    expired = {"expiring_soon": True, "days_left": -2, "not_after": "2026-10-18T00:00:00+00:00"}
    await observe(
        services, source_id, ok=False, details=expired, at=utcnow() - timedelta(minutes=5)
    )
    await run_risks(services.db)
    (risk,) = await open_risks(services)
    assert (risk.kind, risk.component_id) == ("cert_expiry", component_id)
    await observe(services, source_id, details={"expiring_soon": False, "days_left": 89})
    assert (await run_risks(services.db)).resolved == 1


async def test_risks_follow_tenancy(services: AppServices) -> None:
    org_a, org_b = await two_orgs(services)
    private = await add_source(services, org_a, BASE)
    await observe(services, private, details={"deprecation": "true"})
    await run_risks(services.db)
    async with services.db.system("test lookup") as s:
        source = await s.get(Source, private)
        assert source is not None
        component = source.component_id
    for org, count in ((org_a, 1), (org_b, 0)):
        scope = TenantScope(org_id=org, user_id=None, role=Role.VIEWER)
        assert len((await services.risks.open(scope, [component]))[component]) == count


async def test_service_page_lists_risks(
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
        for kind, due in (
            ("sunset", utcnow() + timedelta(days=30, hours=1)),
            ("deprecation", None),
        ):
            s.add(
                Risk(
                    org_id=component.org_id,
                    component_id=component.id,
                    kind=kind,
                    due_at=due,
                    link="https://docs.vendor.test/x" if due else None,
                    first_seen_at=utcnow(),
                    last_seen_at=utcnow(),
                )
            )
    page = await http.get(f"/o/{slug}/servisler/{service}")
    assert "kapanış tarihini bildirdi" in page.text
    assert "30 gün kaldı" in page.text
    assert "Tarih bildirilmedi" in page.text
    assert page.text.index("kapanış tarihini") < page.text.index(
        "kullanımdan kaldırdı"
    )  # dated first
