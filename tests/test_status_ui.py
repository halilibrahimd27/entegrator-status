"""Panel flows for status: dependency states, manual incidents, maintenance windows."""

from __future__ import annotations

import uuid
from datetime import timedelta

import httpx2
import pytest
from fastapi import FastAPI
from sqlalchemy import select

from org_support import create_org, create_product, create_service, join, new_token
from sentinel.accounts.mail import Mail
from sentinel.db.base import utcnow
from sentinel.engine.notices import NoticeError
from sentinel.models import (
    AuditEvent,
    Component,
    ComponentState,
    Incident,
    IncidentKind,
    IncidentStatus,
    MaintenanceWindow,
    Organization,
)
from sentinel.tenancy.roles import Role
from sentinel.tenancy.scope import TenantScope
from sentinel.web.services import AppServices
from sentinel.web.templating import LOCAL_TZ
from support import post_form, signed_in
from test_catalog_ui import shared_component, with_examples

ALICE = "alice@example.invalid"


def local_input(delta: timedelta) -> str:
    return (utcnow() + delta).astimezone(LOCAL_TZ).strftime("%Y-%m-%dT%H:%M")


async def own_component(services: AppServices, service_id: str) -> str:
    async with services.db.system("test lookup") as s:
        found = await s.scalar(
            select(Component.id).where(Component.service_id == uuid.UUID(service_id))
        )
    return str(found)


async def set_state(
    services: AppServices, component_id: str, state: str, cause: str | None
) -> None:
    async with services.db.system("test setup") as s:
        component = await s.get(Component, uuid.UUID(component_id))
        assert component is not None
        s.add(
            ComponentState(
                org_id=component.org_id,
                component_id=component.id,
                state=state,
                started_at=utcnow() - timedelta(minutes=5),
                cause=cause,
            )
        )


async def test_dependency_states_show_on_the_product_page(
    http: httpx2.AsyncClient, outbox: list[Mail], services: AppServices
) -> None:
    await with_examples(services)
    await signed_in(http, outbox, ALICE)
    slug = await create_org(http, "Alpha")
    product = await create_product(http, slug)
    base = f"/o/{slug}/urunler/{product}"
    api = await shared_component(services, "ornek-odeme", "api")
    await post_form(http, f"{base}/bagimlilik", {"to_component_id": api}, csrf_from=base)

    page = await http.get(base)
    assert "Veri yok" in page.text  # no measurement yet
    await set_state(services, api, "down", "read_timeout")
    page = await http.get(base)
    assert "state-down" in page.text
    assert "Erişim sorunu" in page.text
    assert "Yanıt zaman aşımı" in page.text  # the cause hint, worded as a hint


async def test_manual_incident_lifecycle(
    app: FastAPI, http: httpx2.AsyncClient, outbox: list[Mail], services: AppServices
) -> None:
    await signed_in(http, outbox, ALICE)
    slug = await create_org(http, "Alpha")
    product = await create_product(http, slug, "Faturalama")
    base = f"/o/{slug}/urunler/{product}"

    bad = await post_form(
        http,
        f"{base}/olaylar",
        {"title": " ", "body": "x", "status": "investigating"},
        csrf_from=base,
    )
    assert bad.status_code == 400
    assert "Başlık 1 ile 200" in bad.text
    wrong = await post_form(
        http, f"{base}/olaylar", {"title": "T", "body": "x", "status": "open"}, csrf_from=base
    )
    assert wrong.status_code == 400  # "open" belongs to engine incidents

    opened = await post_form(
        http,
        f"{base}/olaylar",
        {
            "title": "Gönderim gecikmesi",
            "body": "Kuyruk birikti, inceliyoruz.",
            "status": "investigating",
        },
        csrf_from=base,
    )
    assert opened.status_code == 303
    page = await http.get(base)
    assert "Gönderim gecikmesi" in page.text
    assert "Kuyruk birikti" in page.text
    async with services.db.system("test lookup") as s:
        incident = await s.scalar(select(Incident).where(Incident.kind == IncidentKind.MANUAL))
        assert incident is not None
        assert incident.resolved_at is None

    resolved = await post_form(
        http,
        f"/o/{slug}/olaylar/{incident.id}/guncelle",
        {"status": "resolved", "body": "Kuyruk boşaldı."},
        csrf_from=base,
    )
    assert resolved.status_code == 303
    async with services.db.system("test lookup") as s:
        again = await s.get(Incident, incident.id)
        assert again is not None
        assert again.status == IncidentStatus.RESOLVED
        assert again.resolved_at is not None
        actions = set((await s.scalars(select(AuditEvent.action))).all())
    assert {"incident.opened", "incident.updated"} <= actions
    assert "Kuyruk boşaldı." in (await http.get(base)).text

    # a viewer sees incidents but cannot write them
    async with join(app, outbox, http, slug, "viewer@example.invalid", role="viewer") as viewer:
        page = await viewer.get(base)
        assert "Gönderim gecikmesi" in page.text
        assert f"{base}/olaylar" not in page.text  # no form
        refused = await post_form(
            viewer,
            f"/o/{slug}/olaylar/{incident.id}/guncelle",
            {"status": "monitoring", "body": "x"},
            csrf_from=base,
        )
        assert refused.status_code == 403


async def test_engine_incidents_cannot_be_edited(
    http: httpx2.AsyncClient, outbox: list[Mail], services: AppServices
) -> None:
    await with_examples(services)
    await signed_in(http, outbox, ALICE)
    slug = await create_org(http, "Alpha")
    product = await create_product(http, slug)
    api = await shared_component(services, "ornek-odeme", "api")
    async with services.db.system("test setup") as s:
        auto = Incident(
            kind=IncidentKind.AUTO,
            status=IncidentStatus.OPEN,
            component_id=uuid.UUID(api),
            started_at=utcnow(),
        )
        s.add(auto)
    response = await post_form(
        http,
        f"/o/{slug}/olaylar/{auto.id}/guncelle",
        {"status": "resolved", "body": "x"},
        csrf_from=f"/o/{slug}/urunler/{product}",
    )
    assert response.status_code == 404


async def test_maintenance_windows(
    http: httpx2.AsyncClient, outbox: list[Mail], services: AppServices
) -> None:
    await with_examples(services)
    await signed_in(http, outbox, ALICE)
    slug = await create_org(http, "Alpha")
    service = await create_service(http, slug)
    component = await own_component(services, service)
    page_url = f"/o/{slug}/servisler/{service}"
    add_url = f"/o/{slug}/bilesenler/{component}/bakim"

    async def add(starts: timedelta, ends: timedelta, title: str = "DB bakımı") -> httpx2.Response:
        form = {"title": title, "starts_at": local_input(starts), "ends_at": local_input(ends)}
        return await post_form(http, add_url, form, csrf_from=page_url)

    for starts, ends, message in (
        (timedelta(hours=-2), timedelta(hours=-1), "geçmişe dönük"),
        (timedelta(hours=2), timedelta(hours=1), "Bitiş, başlangıçtan sonra"),
        (timedelta(hours=1), timedelta(days=20), "en fazla 14 gün"),
    ):
        refused = await add(starts, ends)
        assert refused.status_code == 400
        assert message in refused.text
    garbled = await post_form(
        http, add_url, {"title": "x", "starts_at": "yarın", "ends_at": "x"}, csrf_from=page_url
    )
    assert garbled.status_code == 400
    assert "Tarih ve saat geçersiz" in garbled.text

    assert (
        await add(timedelta(days=1), timedelta(days=1, hours=2), "Planlı iş")
    ).status_code == 303
    page = await http.get(page_url)
    assert "Planlı iş" in page.text
    assert "Planlandı" in page.text
    async with services.db.system("test lookup") as s:
        planned = await s.scalar(
            select(MaintenanceWindow).where(MaintenanceWindow.title == "Planlı iş")
        )
        assert planned is not None
    cancelled = await post_form(
        http, f"/o/{slug}/bakimlar/{planned.id}/sil", {}, csrf_from=page_url
    )
    assert cancelled.status_code == 303
    async with services.db.system("test lookup") as s:
        assert await s.get(MaintenanceWindow, planned.id) is None  # never started: removed

    assert (await add(timedelta(minutes=-1), timedelta(hours=1), "Acil yama")).status_code == 303
    page = await http.get(page_url)
    assert "Sürüyor" in page.text
    assert "state-maintenance" in page.text  # the component shows maintenance meanwhile
    async with services.db.system("test lookup") as s:
        running = await s.scalar(
            select(MaintenanceWindow).where(MaintenanceWindow.title == "Acil yama")
        )
        assert running is not None
    ended = await post_form(http, f"/o/{slug}/bakimlar/{running.id}/sil", {}, csrf_from=page_url)
    assert ended.status_code == 303
    async with services.db.system("test lookup") as s:
        kept = await s.get(MaintenanceWindow, running.id)
        assert kept is not None  # started: kept as history, ended now
        assert kept.ends_at <= utcnow()
    over = await post_form(http, f"/o/{slug}/bakimlar/{running.id}/sil", {}, csrf_from=page_url)
    assert over.status_code == 400
    assert "Bitmiş bir bakım" in over.text

    # shared components: platform admins only
    api = await shared_component(services, "ornek-odeme", "api")
    shared = await post_form(
        http,
        f"/o/{slug}/bilesenler/{api}/bakim",
        {
            "title": "x",
            "starts_at": local_input(timedelta(days=1)),
            "ends_at": local_input(timedelta(days=2)),
        },
        csrf_from=page_url,
    )
    assert shared.status_code == 403
    assert "yalnızca platform yöneticileri" in shared.text


async def test_platform_windows_on_shared_components(services: AppServices) -> None:
    await with_examples(services)
    api = uuid.UUID(await shared_component(services, "ornek-odeme", "api"))
    async with services.db.system("test setup") as s:
        org = Organization(slug="a", name="A")
        s.add(org)
        await s.flush()
        s.add(
            MaintenanceWindow(
                component_id=api,
                title="Sağlayıcı bakımı",
                starts_at=utcnow() - timedelta(minutes=1),
                ends_at=utcnow() + timedelta(hours=1),
            )
        )
    scope = TenantScope(org_id=org.id, user_id=None, role=Role.MEMBER)
    status = (await services.status.current(scope, [api]))[api]
    assert status.state == "maintenance"
    assert status.maintenance is not None
    windows = await services.notices.component_windows(scope, [api])
    assert [w.title for w in windows[api]] == ["Sağlayıcı bakımı"]
    with pytest.raises(NoticeError, match=r"status\.shared_component"):
        await services.notices.end_window(scope, windows[api][0].id)
    with pytest.raises(NoticeError, match=r"status\.shared_component"):
        await services.notices.add_window(
            scope,
            api,
            title="x",
            starts_at=utcnow() + timedelta(hours=1),
            ends_at=utcnow() + timedelta(hours=2),
        )


async def test_status_overview_page_and_api(
    http: httpx2.AsyncClient, outbox: list[Mail], services: AppServices
) -> None:
    await with_examples(services)
    await signed_in(http, outbox, ALICE)
    slug = await create_org(http, "Alpha")
    product = await create_product(http, slug, "Ödeme modülü")
    base = f"/o/{slug}/urunler/{product}"
    api = await shared_component(services, "ornek-odeme", "api")
    await post_form(http, f"{base}/bagimlilik", {"to_component_id": api}, csrf_from=base)

    empty = await create_org(http, "Beta")
    assert "Henüz izlenen bir bağımlılık yok" in (await http.get(f"/o/{empty}/durum")).text

    await set_state(services, api, "down", "tls")
    page = await http.get(f"/o/{slug}/durum")
    assert page.status_code == 200
    assert 'aria-current="page"' in page.text
    assert "state-down" in page.text
    assert "TLS sertifika hatası" in page.text
    assert "2. derece" in page.text  # the curated upstream of the vendor
    assert "Ödeme modülü" in page.text
    assert page.text.index("state-down") < page.text.index("state-unknown")  # worst first

    token = await new_token(http, slug)
    response = await http.get("/api/v1/status", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200
    components = response.json()["components"]
    first = components[0]
    assert (first["component_id"], first["state"], first["cause"]) == (api, "down", "tls")
    assert first["depth"] == 1
    assert first["products"] == ["Ödeme modülü"]
    assert set(first["uptime"]) == {"24h", "7d", "30d", "90d"}
    assert {c["depth"] for c in components} >= {1, 2}
    assert (await http.get("/api/v1/status")).status_code == 401


async def test_service_page_shows_uptime_and_bars(
    http: httpx2.AsyncClient, outbox: list[Mail], services: AppServices
) -> None:
    await signed_in(http, outbox, ALICE)
    slug = await create_org(http, "Alpha")
    service = await create_service(http, slug)
    component = await own_component(services, service)
    page_url = f"/o/{slug}/servisler/{service}"
    assert 'class="bars"' not in (await http.get(page_url)).text  # nothing measured yet
    await set_state(services, component, "operational", None)
    page = await http.get(page_url)
    assert 'class="bars"' in page.text
    assert "%100,00" in page.text
    assert "Erişilebilirlik" in page.text
