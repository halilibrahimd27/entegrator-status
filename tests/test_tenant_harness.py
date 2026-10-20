"""Two-tenant harness: every organization route refuses another tenant's objects.

Routes are discovered from the app, so a new ``/o/{slug}`` route is covered without
editing this file. Expected answer for anything foreign: 404.
"""

from __future__ import annotations

import re
import uuid
from datetime import timedelta

import httpx2
from fastapi import FastAPI
from sqlalchemy import select

from org_support import (
    any_csrf,
    browser,
    create_org,
    create_product,
    create_service,
    join,
    new_token,
)
from sentinel.accounts.mail import Mail
from sentinel.db.base import utcnow
from sentinel.models import (
    ApiToken,
    Change,
    Component,
    Credential,
    Dependency,
    Incident,
    Invitation,
    MaintenanceWindow,
    Membership,
    Organization,
    Product,
    Service,
    Source,
)
from sentinel.web.services import AppServices
from sentinel.web.templating import LOCAL_TZ
from support import api_routes, post_form, signed_in


def local_input(delta: timedelta) -> str:
    return (utcnow() + delta).astimezone(LOCAL_TZ).strftime("%Y-%m-%dT%H:%M")


FORM = {
    "name": "Yeni Ad",
    "email": "x@example.invalid",
    "role": "viewer",
    "days": "30",
    "kind": "http",
    "url": "https://x.example.invalid/",
    "token": "t" * 24,
    "confirm": "x",
    "password": "x",
    "title": "Başlık",
    "body": "Açıklama",
    "status": "investigating",
    "category": "maintenance",
    "starts_at": local_input(timedelta(days=1)),
    "ends_at": local_input(timedelta(days=1, hours=2)),
}
UPLOAD = {"file": ("requirements.txt", b"httpx==0.28.1\n")}
Model = (
    type[Membership]
    | type[Invitation]
    | type[ApiToken]
    | type[Product]
    | type[Service]
    | type[Component]
    | type[Source]
    | type[Dependency]
    | type[Incident]
    | type[MaintenanceWindow]
    | type[Change]
    | type[Credential]
)
ID_PARAMS: dict[str, Model] = {
    "membership_id": Membership,
    "invitation_id": Invitation,
    "token_id": ApiToken,
    "product_id": Product,
    "service_id": Service,
    "component_id": Component,
    "source_id": Source,
    "dependency_id": Dependency,
    "incident_id": Incident,
    "window_id": MaintenanceWindow,
    "change_id": Change,
    "credential_id": Credential,
}


async def ids_of(services: AppServices, slug: str) -> dict[str, uuid.UUID]:
    async with services.db.system("test harness lookup") as s:
        org_id = await s.scalar(select(Organization.id).where(Organization.slug == slug))
        found: dict[str, uuid.UUID] = {}
        for param, model in ID_PARAMS.items():
            value = await s.scalar(select(model.id).where(model.org_id == org_id).limit(1))
            assert value is not None, f"fixture has no {model.__name__} in {slug}"
            found[param] = value
    return found


def org_routes(app: FastAPI) -> list[tuple[str, str]]:
    return sorted(
        (method, path)
        for path, route in api_routes(app)
        if path.startswith("/o/{slug}")
        for method in (route.methods or set())
    )


def fill(path: str, slug: str, ids: dict[str, uuid.UUID]) -> str:
    path = path.replace("{slug}", slug)
    return re.sub(r"\{(\w+)\}", lambda m: str(ids[m.group(1)]), path)


async def build_tenant(
    app: FastAPI,
    services: AppServices,
    outbox: list[Mail],
    owner: httpx2.AsyncClient,
    email: str,
    name: str,
) -> str:
    await signed_in(owner, outbox, email)
    slug = await create_org(owner, name)
    async with join(app, outbox, owner, slug, f"member-of-{slug}@example.invalid"):
        pass
    await post_invite_only(owner, slug)
    await new_token(owner, slug)
    product_id = await create_product(owner, slug)
    service_id = await create_service(owner, slug)
    async with services.db.system("test harness lookup") as s:
        component_id = await s.scalar(
            select(Component.id).where(Component.service_id == uuid.UUID(service_id))
        )
    base = f"/o/{slug}"
    added = await post_form(
        owner,
        f"{base}/bilesenler/{component_id}/kaynak",
        {"kind": "http", "url": f"https://{slug}.example.invalid/"},
        csrf_from=f"{base}/servisler/{service_id}",
    )
    assert added.status_code == 303, added.text
    linked = await post_form(
        owner,
        f"{base}/urunler/{product_id}/bagimlilik",
        {"to_component_id": str(component_id)},
        csrf_from=f"{base}/urunler/{product_id}",
    )
    assert linked.status_code == 303, linked.text
    opened = await post_form(
        owner,
        f"{base}/urunler/{product_id}/olaylar",
        {"title": "Fatura gönderimi yavaş", "body": "İnceliyoruz.", "status": "investigating"},
        csrf_from=f"{base}/urunler/{product_id}",
    )
    assert opened.status_code == 303, opened.text
    planned = await post_form(
        owner,
        f"{base}/bilesenler/{component_id}/bakim",
        {
            "title": "Veritabanı bakımı",
            "starts_at": local_input(timedelta(days=2)),
            "ends_at": local_input(timedelta(days=2, hours=1)),
        },
        csrf_from=f"{base}/servisler/{service_id}",
    )
    assert planned.status_code == 303, planned.text
    announced = await post_form(
        owner,
        f"{base}/servisler/{service_id}/duyuru",
        {
            "title": "Sağlayıcı duyurusu",
            "category": "maintenance",
            "component_ids": str(component_id),
        },
        csrf_from=f"{base}/servisler/{service_id}",
    )
    assert announced.status_code == 303, announced.text
    vault = await post_form(
        owner,
        f"{base}/kimlik-bilgileri",
        {
            "name": "Portal",
            "kind": "bearer",
            "host": f"{slug}.example.invalid",
            "token": "t" + uuid.uuid4().hex,
            "entitled": "true",
        },
        csrf_from=f"{base}/kimlik-bilgileri",
    )
    assert vault.status_code == 303, vault.text
    return slug


async def post_invite_only(owner: httpx2.AsyncClient, slug: str) -> None:
    from org_support import invite

    assert (await invite(owner, slug, f"pending-{slug}@example.invalid")).status_code == 303


async def test_every_org_route_hides_other_tenants(
    app: FastAPI, outbox: list[Mail], services: AppServices
) -> None:
    async with browser(app) as alice, browser(app) as bob:
        a = await build_tenant(app, services, outbox, alice, "alice@example.invalid", "Alpha")
        b = await build_tenant(app, services, outbox, bob, "bob@example.invalid", "Beta")
        ids_b = await ids_of(services, b)
        routes = org_routes(app)
        assert len(routes) >= 34  # the discovery itself works (phase 4b: status routes)

        failures = []
        for method, template in routes:
            targets = [fill(template, b, ids_b)]  # the other tenant's organization
            if "_id}" in template:
                targets.append(fill(template, a, ids_b))  # own org, foreign object id
            for url in targets:
                if method == "GET":
                    response = await alice.get(url)
                else:
                    form = {
                        **FORM,
                        "to_component_id": str(ids_b["component_id"]),
                        "csrf": await any_csrf(alice),
                    }
                    response = await alice.request(method, url, data=form, files=UPLOAD)
                if response.status_code != 404:
                    failures.append(f"{method} {url} -> {response.status_code}")
        assert failures == []

        # Nothing of Beta changed.
        assert (await bob.get(f"/o/{b}")).status_code == 200
        assert "Beta" in (await bob.get(f"/o/{b}/ayarlar")).text


async def test_api_token_sees_only_its_own_tenant(
    app: FastAPI, outbox: list[Mail], services: AppServices
) -> None:
    async with browser(app) as alice, browser(app) as bob:
        await signed_in(alice, outbox, "alice@example.invalid")
        await signed_in(bob, outbox, "bob@example.invalid")
        a = await create_org(alice, "Alpha")
        b = await create_org(bob, "Beta")
        token_a, token_b = await new_token(alice, a), await new_token(bob, b)
        async with browser(app) as api:
            got_a = (
                await api.get("/api/v1/org", headers={"Authorization": f"Bearer {token_a}"})
            ).json()
            got_b = (
                await api.get("/api/v1/org", headers={"Authorization": f"Bearer {token_b}"})
            ).json()
        assert got_a["slug"] == a
        assert got_b["slug"] == b
