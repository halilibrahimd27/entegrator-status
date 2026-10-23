"""Platform administration: quotas, the platform audit log, notices on shared services."""

from __future__ import annotations

import uuid
from datetime import timedelta

import httpx2
import pytest
from fastapi import FastAPI
from sqlalchemy import select

from org_support import browser, create_org, create_product, create_service
from sentinel.accounts.mail import Mail
from sentinel.db.base import utcnow
from sentinel.models import (
    AuditEvent,
    Change,
    Component,
    MaintenanceWindow,
    Organization,
    Service,
    Source,
)
from sentinel.tenancy.roles import Role
from sentinel.tenancy.scope import TenantScope
from sentinel.web.services import AppServices
from sentinel.web.templating import LOCAL_TZ
from support import post_form, signed_in
from test_catalog_ui import make_admin

ADMIN_PAGES = (
    "/yonetim",
    "/yonetim/orglar",
    "/yonetim/denetim",
    "/yonetim/servisler",
    f"/yonetim/servisler/{uuid.uuid4()}",
)


def local(delta: timedelta) -> str:
    return (utcnow() + delta).astimezone(LOCAL_TZ).strftime("%Y-%m-%dT%H:%M")


async def admin_client(
    admin: httpx2.AsyncClient,
    outbox: list[Mail],
    services: AppServices,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await signed_in(admin, outbox, "admin@example.invalid")
    for path in ADMIN_PAGES:
        assert (await admin.get(path)).status_code == 404, path  # not yet an admin
    await make_admin(admin, services, monkeypatch, "admin@example.invalid")


async def add_source(owner: httpx2.AsyncClient, slug: str, service: str, n: int) -> int:
    base = f"/o/{slug}/servisler/{service}"
    page = await owner.get(base)
    component = page.text.split("/bilesenler/")[1].split("/")[0]
    response = await post_form(
        owner,
        f"/o/{slug}/bilesenler/{component}/kaynak",
        {"kind": "http", "url": f"https://k{n}.example.invalid/"},
        csrf_from=base,
    )
    return response.status_code


async def test_source_quota(
    app: FastAPI,
    outbox: list[Mail],
    services: AppServices,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with browser(app) as owner, browser(app) as admin:
        await signed_in(owner, outbox, "sahip@example.invalid")
        slug = await create_org(owner, "Alfa")
        service = await create_service(owner, slug, "Portal")
        assert await add_source(owner, slug, service, 1) == 303
        await admin_client(admin, outbox, services, monkeypatch)
        page = await admin.get("/yonetim/orglar")
        assert "Alfa" in page.text
        assert "1 / 300" in page.text
        async with services.db.system("test lookup") as s:
            org_id = await s.scalar(select(Organization.id).where(Organization.slug == slug))
        for bad in ("0", "abc", "100001"):
            refused = await post_form(
                admin,
                f"/yonetim/orglar/{org_id}/kota",
                {"max_sources": bad},
                csrf_from="/yonetim/orglar",
            )
            assert refused.status_code == 400, bad
        assert (
            await post_form(
                admin,
                f"/yonetim/orglar/{uuid.uuid4()}/kota",
                {"max_sources": "5"},
                csrf_from="/yonetim/orglar",
            )
        ).status_code == 404
        set_two = await post_form(
            admin,
            f"/yonetim/orglar/{org_id}/kota",
            {"max_sources": "2"},
            csrf_from="/yonetim/orglar",
        )
        assert set_two.status_code == 303
        assert await add_source(owner, slug, service, 2) == 303
        assert await add_source(owner, slug, service, 3) == 400  # full
        page = await owner.get(f"/o/{slug}/servisler/{service}")
        assert (
            "Kaynak kotan doldu"
            in (
                await post_form(
                    owner,
                    f"/o/{slug}/bilesenler/{page.text.split('/bilesenler/')[1].split('/')[0]}/kaynak",
                    {"kind": "http", "url": "https://k4.example.invalid/"},
                    csrf_from=f"/o/{slug}/servisler/{service}",
                )
            ).text
        )

        product = await create_product(owner, slug, "Fatura")
        base = f"/o/{slug}/urunler/{product}"
        preview = await owner.post(
            f"{base}/ice-aktar",
            data={
                "csrf": (await owner.get(base)).text.split('name="csrf" value="')[1].split('"')[0]
            },
            files={"file": ("requirements.txt", b"httpx==0.28.1\nrequests==2.32.0\n")},
        )
        assert preview.status_code == 200, preview.text
        token = preview.text.split('name="token" value="')[1].split('"')[0]
        done = await post_form(owner, f"{base}/ice-aktar/onayla", {"token": token}, csrf_from=base)
        assert done.status_code == 303
        assert done.headers["location"].endswith("?atlanan=4")  # release + advisory, x2
        assert "Kaynak kotan doldu: 4" in (await owner.get(done.headers["location"])).text

        reset = await post_form(
            admin,
            f"/yonetim/orglar/{org_id}/kota",
            {"max_sources": ""},
            csrf_from="/yonetim/orglar",
        )
        assert reset.status_code == 303
        assert await add_source(owner, slug, service, 5) == 303  # back to the default
        audit = await admin.get("/yonetim/denetim")
        assert "platform.quota_set" in audit.text
        assert "max_sources=2" in audit.text


async def test_quota_counts_only_the_organizations_own_sources(services: AppServices) -> None:
    async with services.db.system("test setup") as s:
        org = Organization(slug="q", name="Q", max_sources=1)
        s.add(org)
        service = Service(slug="ortak", name="Ortak", kind="other", tags=[])
        s.add(service)
        await s.flush()
        component = Component(service_id=service.id, slug="api", name="API", tags=[])
        s.add(component)
        await s.flush()
        s.add(
            Source(
                component_id=component.id,
                kind="http",
                identity="shared",
                config={"url": "https://ortak.example.invalid/"},
                interval_s=120,
                timeout_s=10,
            )
        )
        org_id = org.id
    async with services.db.tenant(TenantScope(org_id, None, Role.OWNER)) as s:
        assert await services.catalog.source_room(s, org_id) == 1


async def test_shared_notices(
    app: FastAPI,
    outbox: list[Mail],
    services: AppServices,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with services.db.system("test setup") as s:
        service = Service(slug="gib", name="GİB", kind="authority", tags=[])
        s.add(service)
        await s.flush()
        component = Component(service_id=service.id, slug="efatura", name="e-Fatura", tags=[])
        s.add(component)
        await s.flush()
        service_id, component_id = service.id, component.id
    async with browser(app) as admin:
        await admin_client(admin, outbox, services, monkeypatch)
        listing = await admin.get("/yonetim/servisler?q=gİ")
        assert f"/yonetim/servisler/{service_id}" in listing.text
        base = f"/yonetim/servisler/{service_id}"
        assert (await admin.get(base)).status_code == 200
        past = await post_form(
            admin,
            f"{base}/bakim",
            {
                "component_id": str(component_id),
                "title": "Planlı bakım",
                "starts_at": local(-timedelta(hours=2)),
                "ends_at": local(timedelta(hours=1)),
            },
            csrf_from=base,
        )
        assert past.status_code == 400  # maintenance is declared before it starts
        added = await post_form(
            admin,
            f"{base}/bakim",
            {
                "component_id": str(component_id),
                "title": "Planlı bakım",
                "starts_at": local(timedelta(hours=2)),
                "ends_at": local(timedelta(hours=3)),
            },
            csrf_from=base,
        )
        assert added.status_code == 303
        announced = await post_form(
            admin,
            f"{base}/duyuru",
            {
                "title": "UBL-TR kod listesi güncellemesi",
                "category": "package_update",
                "component_ids": str(component_id),
            },
            csrf_from=base,
        )
        assert announced.status_code == 303
        async with services.db.system("test check") as s:
            window = await s.scalar(select(MaintenanceWindow))
            change = await s.scalar(select(Change))
        assert window is not None
        assert change is not None
        assert (window.org_id, change.org_id, change.origin) == (None, None, "manual")

        async with browser(app) as owner:
            await signed_in(owner, outbox, "sahip@example.invalid")
            slug = await create_org(owner, "Alfa")
            product = await create_product(owner, slug, "Fatura")
            pbase = f"/o/{slug}/urunler/{product}"
            await post_form(
                owner,
                f"{pbase}/bagimlilik",
                {"to_component_id": str(component_id)},
                csrf_from=pbase,
            )
            assert "UBL-TR kod listesi güncellemesi" in (await owner.get(pbase)).text

        ended = await post_form(
            admin,
            f"/yonetim/bakimlar/{window.id}/bitir",
            {"service_id": str(service_id)},
            csrf_from=base,
        )
        assert ended.status_code == 303
        deleted = await post_form(
            admin, f"/yonetim/degisiklikler/{change.id}/sil", {}, csrf_from=base
        )
        assert deleted.status_code == 303
        async with services.db.system("test check") as s:
            assert await s.scalar(select(MaintenanceWindow)) is None  # had not started
            assert await s.scalar(select(Change)) is None
        audit = (await admin.get("/yonetim/denetim")).text
        for action in (
            "platform.maintenance_added",
            "platform.announcement_added",
            "platform.maintenance_ended",
            "platform.announcement_deleted",
        ):
            assert action in audit
        assert (
            await post_form(admin, f"/yonetim/degisiklikler/{uuid.uuid4()}/sil", {}, csrf_from=base)
        ).status_code == 404


async def test_admin_cli(app: FastAPI, outbox: list[Mail], services: AppServices) -> None:
    from sentinel.admin.__main__ import admins, change

    for email in ("ilk@example.invalid", "ikinci@example.invalid"):
        async with browser(app) as user:
            await signed_in(user, outbox, email)
    with pytest.raises(LookupError):
        await change(services.db, "yok@example.invalid", True)
    assert await change(services.db, "Ilk@Example.invalid", True) == "promoted"
    assert await change(services.db, "ilk@example.invalid", True) == "unchanged"
    with pytest.raises(LookupError):
        await change(services.db, "ilk@example.invalid", False)  # the last one stays
    assert await change(services.db, "ikinci@example.invalid", True) == "promoted"
    assert await change(services.db, "ilk@example.invalid", False) == "demoted"
    assert await admins(services.db) == ["ikinci@example.invalid"]
    async with services.db.system("test check") as s:
        actions = (
            await s.scalars(select(AuditEvent.action).where(AuditEvent.org_id.is_(None)))
        ).all()
    assert actions.count("platform.admin_granted") == 2
    assert "platform.admin_revoked" in actions


@pytest.mark.usefixtures("app")
def test_admin_cli_entrypoint(capsys: pytest.CaptureFixture[str]) -> None:
    from sentinel.admin.__main__ import main

    assert main(["promote", "yok@example.invalid"]) == 1
    assert "no verified account" in capsys.readouterr().err
    assert main(["list"]) == 0
