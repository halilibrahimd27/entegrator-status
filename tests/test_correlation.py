"""Correlated-failure hints (at the threshold, not below) and the weekly digest."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import httpx2
import pytest
from sqlalchemy import select

from org_support import create_org, create_product
from sentinel.accounts.mail import Mail
from sentinel.config import ConfigError, load_settings
from sentinel.db.base import utcnow
from sentinel.models import (
    Change,
    Component,
    Dependency,
    Incident,
    IncidentKind,
    IncidentStatus,
    Organization,
    Risk,
    Service,
)
from sentinel.risk.correlation import find_hints
from sentinel.tenancy.roles import Role
from sentinel.tenancy.scope import TenantScope
from sentinel.web.services import AppServices
from support import post_form, signed_in

T0 = datetime(2026, 10, 23, 9, 0, tzinfo=UTC)
UP = uuid.uuid4()
KIDS = [uuid.uuid4() for _ in range(5)]


def down(*offsets: float) -> dict[uuid.UUID, datetime]:
    return {kid: T0 + timedelta(minutes=m) for kid, m in zip(KIDS, offsets, strict=False)}


@pytest.mark.parametrize(
    ("offsets", "ratio", "fires"),
    [
        ((0, 4), 0.4, True),  # 2 of 5 within ten minutes: exactly at the threshold
        ((0,), 0.4, False),  # 1 of 5: below
        ((0, 15), 0.4, False),  # 2 of 5 but not together
        ((0, 4, 9, 30), 0.4, True),  # 3 together, one later
        ((0, 4), 0.5, False),  # a stricter threshold
    ],
)
def test_threshold(offsets: tuple[float, ...], ratio: float, fires: bool) -> None:
    hints = find_hints({UP: set(KIDS)}, down(*offsets), ratio=ratio)
    assert bool(hints) is fires
    if fires:
        assert hints[0].first_down == T0
        assert hints[0].ratio >= ratio


def test_one_dependant_is_not_a_pattern() -> None:
    lonely = uuid.uuid4()
    assert find_hints({UP: {lonely}}, {lonely: T0}) == []


def test_ratio_is_configurable_and_bounded(
    valid_env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CORRELATED_FAILURE_RATIO", "0.6")
    assert load_settings().correlated_failure_ratio == 0.6
    monkeypatch.setenv("CORRELATED_FAILURE_RATIO", "0.01")
    with pytest.raises(ConfigError):
        load_settings()


async def shared(services: AppServices, name: str) -> uuid.UUID:
    async with services.db.system("test setup") as s:
        service = Service(slug=f"s-{uuid.uuid4().hex[:6]}", name=name, kind="other", tags=[])
        s.add(service)
        await s.flush()
        component = Component(service_id=service.id, slug="c", name="API", tags=[])
        s.add(component)
        await s.flush()
        return component.id


async def test_status_page_shows_the_hint(
    http: httpx2.AsyncClient, outbox: list[Mail], services: AppServices
) -> None:
    upstream = await shared(services, "GİB")
    integrators = [await shared(services, f"Entegratör {i}") for i in range(5)]
    now = utcnow()
    async with services.db.system("test setup") as s:
        for child in integrators:
            s.add(Dependency(from_component_id=child, to_component_id=upstream, usage={}))
        for child, minutes in zip(integrators, (0, 3), strict=False):
            s.add(
                Incident(
                    kind=IncidentKind.AUTO,
                    status=IncidentStatus.OPEN,
                    component_id=child,
                    started_at=now - timedelta(minutes=minutes),
                )
            )
    await signed_in(http, outbox, "alice@example.invalid")
    slug = await create_org(http, "Alpha")
    page = await http.get(f"/o/{slug}/durum")
    assert "GİB — API bileşenine bağlı 5 bileşenden 2 tanesi (%40)" in page.text
    assert "ortak bir kaynaktan kaynaklanıyor olabilir" in page.text  # a hint, worded as one


async def test_weekly_digest(
    http: httpx2.AsyncClient, outbox: list[Mail], services: AppServices
) -> None:
    await signed_in(http, outbox, "alice@example.invalid")
    slug = await create_org(http, "Alpha")
    assert "dikkat gerektiren bir şey olmadı" in (await http.get(f"/o/{slug}/ozet")).text
    component = await shared(services, "Entegratör X")
    for name in ("Fatura", "Mutabakat"):  # the same change reaches two products: listed once
        product = await create_product(http, slug, name)
        base = f"/o/{slug}/urunler/{product}"
        await post_form(
            http, f"{base}/bagimlilik", {"to_component_id": str(component)}, csrf_from=base
        )
    now = utcnow()
    async with services.db.system("test setup") as s:
        s.add(
            Change(
                component_id=component,
                kind="announcement",
                category="deprecation",
                title="SOAP uç noktası kapanıyor",
                detected_at=now - timedelta(days=2),
                effective_at=now + timedelta(days=20),
                fingerprint=uuid.uuid4().hex * 2,
            )
        )
        s.add(
            Change(
                component_id=component,
                kind="announcement",
                category="other",
                title="Eski haber",
                detected_at=now - timedelta(days=20),
                fingerprint=uuid.uuid4().hex * 2,
            )
        )
        s.add(
            Incident(
                kind=IncidentKind.AUTO,
                status=IncidentStatus.RESOLVED,
                component_id=component,
                started_at=now - timedelta(days=1),
                resolved_at=now - timedelta(days=1) + timedelta(minutes=40),
                cause="http_5xx",
            )
        )
        s.add(
            Risk(
                component_id=component,
                kind="cert_expiry",
                due_at=now + timedelta(days=10),
                first_seen_at=now,
                last_seen_at=now,
            )
        )
    scope = TenantScope(org_id=await org_id(services, slug), user_id=None, role=Role.VIEWER)
    digest = await services.digest.weekly(scope)
    assert [c.title for c in digest.changes] == ["SOAP uç noktası kapanıyor"]
    assert [o.cause for o in digest.outages] == ["http_5xx"]
    assert [e.risk.kind for e in digest.due] == ["cert_expiry"]
    assert {i.kind for i in digest.planned} == {"risk", "change"}
    page = await http.get(f"/o/{slug}/ozet")
    for text in (
        "SOAP uç noktası kapanıyor",
        "HTTP 5xx yanıtı",
        "TLS sertifikasının süresi doluyor",
    ):
        assert text in page.text
    assert "Eski haber" not in page.text


async def org_id(services: AppServices, slug: str) -> uuid.UUID:
    async with services.db.system("test lookup") as s:
        found = await s.scalar(select(Organization.id).where(Organization.slug == slug))
    assert found is not None
    return found
