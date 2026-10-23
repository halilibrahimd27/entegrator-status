"""Panel flows for products, the catalog, services, sources, imports and proposals."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path

import httpx2
import pytest
from fastapi import FastAPI
from sqlalchemy import select, update

from org_support import browser, create_org, create_product, create_service, join
from sentinel.accounts.mail import Mail
from sentinel.accounts.totp import code_at
from sentinel.catalog.importer import import_packs
from sentinel.catalog.pack import load_pack
from sentinel.models import Component, Dependency, Service, Source, User
from sentinel.web.limits import MAX_BODY_BYTES
from sentinel.web.services import AppServices
from support import csrf, post_form, signed_in

ALICE = "alice@example.invalid"
EXAMPLES = Path(__file__).resolve().parents[1] / "catalog" / "examples" / "examples.yaml"


async def with_examples(services: AppServices) -> None:
    await import_packs(services.db, [load_pack(EXAMPLES)], apply=True)


async def shared_component(services: AppServices, service_slug: str, component_slug: str) -> str:
    async with services.db.system("test lookup") as s:
        found = await s.scalar(
            select(Component.id)
            .join(Service, Service.id == Component.service_id)
            .where(
                Service.org_id.is_(None),
                Service.slug == service_slug,
                Component.slug == component_slug,
            )
        )
    return str(found)


async def test_products_and_dependencies(
    http: httpx2.AsyncClient, outbox: list[Mail], services: AppServices
) -> None:
    await with_examples(services)
    await signed_in(http, outbox, ALICE)
    slug = await create_org(http, "Alpha")
    product = await create_product(http, slug, "Ödeme modülü")
    base = f"/o/{slug}/urunler/{product}"

    listing = await http.get(f"/o/{slug}/urunler")
    assert "Ödeme modülü" in listing.text

    search = await http.get(f"{base}?q=Ödeme")
    assert "Örnek Ödeme API" in search.text
    api = await shared_component(services, "ornek-odeme", "api")
    added = await post_form(
        http, f"{base}/bagimlilik", {"to_component_id": api, "version": "v2"}, csrf_from=base
    )
    assert added.status_code == 303

    page = await http.get(base)
    assert "<svg" in page.text
    assert "Örnek Ödeme API" in page.text
    assert "eu-west bölgesi" in page.text  # second degree, through the curated edge
    assert "v2" in page.text

    duplicate = await post_form(
        http, f"{base}/bagimlilik", {"to_component_id": api}, csrf_from=base
    )
    assert duplicate.status_code == 400
    assert "zaten var" in duplicate.text

    async with services.db.system("test lookup") as s:
        shared_edge = await s.scalar(select(Dependency.id).where(Dependency.org_id.is_(None)))
        own_edge = await s.scalar(select(Dependency.id).where(Dependency.org_id.is_not(None)))
    refused = await post_form(
        http, f"/o/{slug}/bagimliliklar/{shared_edge}/sil", {}, csrf_from=base
    )
    assert refused.status_code == 403
    removed = await post_form(http, f"/o/{slug}/bagimliliklar/{own_edge}/sil", {}, csrf_from=base)
    assert removed.status_code == 303

    renamed = await post_form(http, f"{base}/ayarlar", {"name": "Tahsilat"}, csrf_from=base)
    assert renamed.status_code == 303
    assert "Tahsilat" in (await http.get(base)).text
    deleted = await post_form(http, f"{base}/sil", {}, csrf_from=base)
    assert deleted.headers["location"] == f"/o/{slug}/urunler"
    assert (await http.get(base)).status_code == 404


async def test_labels_are_escaped_in_the_graph(
    http: httpx2.AsyncClient, outbox: list[Mail], services: AppServices
) -> None:
    await signed_in(http, outbox, ALICE)
    slug = await create_org(http, "Alpha")
    product = await create_product(http, slug, '<img src=x onerror="alert(1)">')
    service = await create_service(http, slug, "<script>alert(1)</script>")
    async with services.db.system("test lookup") as s:
        component = await s.scalar(select(Component.id).where(Component.service_id == service))
    base = f"/o/{slug}/urunler/{product}"
    await post_form(http, f"{base}/bagimlilik", {"to_component_id": str(component)}, csrf_from=base)
    page = (await http.get(base)).text
    svg = page[page.index("<svg") : page.index("</svg>")]
    assert "<script>" not in svg
    assert "<img" not in page
    assert "&lt;script&gt;" in svg


async def test_viewers_cannot_change_the_graph(
    app: FastAPI, http: httpx2.AsyncClient, outbox: list[Mail]
) -> None:
    await signed_in(http, outbox, ALICE)
    slug = await create_org(http, "Alpha")
    product = await create_product(http, slug)
    async with join(app, outbox, http, slug, "viewer@example.invalid", role="viewer") as viewer:
        page = await viewer.get(f"/o/{slug}/urunler/{product}")
        assert page.status_code == 200
        assert "bagimlilik" not in page.text  # no edit controls
        refused = await post_form(
            viewer, f"/o/{slug}/urunler", {"name": "X"}, csrf_from=f"/o/{slug}/urunler"
        )
        assert refused.status_code == 403


async def test_import_through_the_panel(
    http: httpx2.AsyncClient, outbox: list[Mail], services: AppServices
) -> None:
    await signed_in(http, outbox, ALICE)
    slug = await create_org(http, "Alpha")
    product = await create_product(http, slug)
    base = f"/o/{slug}/urunler/{product}"
    token = await csrf(http, base)
    preview = await http.post(
        f"{base}/ice-aktar",
        data={"csrf": token},
        files={"file": ("requirements.txt", b"fastapi==0.142.2\nhttpx==0.28.1\n")},
    )
    assert preview.status_code == 200
    assert "fastapi" in preview.text
    assert "httpx" in preview.text
    import_token = re.search(r'name="token" value="([^"]+)"', preview.text)
    assert import_token
    done = await post_form(
        http, f"{base}/ice-aktar/onayla", {"token": import_token.group(1)}, csrf_from=base
    )
    assert done.status_code == 303
    page = await http.get(base)
    assert "0.142.2" in page.text
    assert "0.28.1" in page.text

    bad = await http.post(
        f"{base}/ice-aktar",
        data={"csrf": await csrf(http, base)},
        files={"file": ("notes.txt", b"hello")},
    )
    assert bad.status_code == 400
    assert "Dosya biçimi tanınmadı" in bad.text


async def test_request_body_limit(http: httpx2.AsyncClient, outbox: list[Mail]) -> None:
    await signed_in(http, outbox, ALICE)
    slug = await create_org(http, "Alpha")
    product = await create_product(http, slug)
    base = f"/o/{slug}/urunler/{product}"
    response = await http.post(
        f"{base}/ice-aktar",
        data={"csrf": await csrf(http, base)},
        files={"file": ("requirements.txt", b"x" * (MAX_BODY_BYTES + 1))},
    )
    assert response.status_code == 413


async def test_private_services_components_and_sources(
    http: httpx2.AsyncClient, outbox: list[Mail], services: AppServices
) -> None:
    await with_examples(services)
    await signed_in(http, outbox, ALICE)
    slug = await create_org(http, "Alpha")
    catalog = await http.get(f"/o/{slug}/katalog")
    assert "Örnek Bulut" in catalog.text
    assert "Örnek Ödeme API" in (await http.get(f"/o/{slug}/katalog?q=odeme")).text

    service = await create_service(http, slug, "Muhasebe API", kind="saas_api")
    page_url = f"/o/{slug}/servisler/{service}"
    added = await post_form(
        http,
        f"{page_url}/bilesen",
        {"name": "Webhook", "tags": "efatura_service"},
        csrf_from=page_url,
    )
    assert added.status_code == 303
    async with services.db.system("test lookup") as s:
        component = await s.scalar(
            select(Component.id).where(Component.service_id == service, Component.slug == "webhook")
        )

    ok = await post_form(
        http,
        f"/o/{slug}/bilesenler/{component}/kaynak",
        {"kind": "http", "url": "https://muhasebe.example.invalid/health", "host": "ignored"},
        csrf_from=page_url,
    )
    assert ok.status_code == 303
    blocked = await post_form(
        http,
        f"/o/{slug}/bilesenler/{component}/kaynak",
        {"kind": "http", "url": "https://10.0.0.5/"},
        csrf_from=page_url,
    )
    assert blocked.status_code == 400
    assert "blocked" in blocked.text  # the technical detail is shown
    page = await http.get(page_url)
    assert "muhasebe.example.invalid/health" in page.text

    async with services.db.system("test lookup") as s:
        source = await s.scalar(select(Source.id).where(Source.component_id == component))
    assert (
        await post_form(http, f"/o/{slug}/kaynaklar/{source}/sil", {}, csrf_from=page_url)
    ).status_code == 303
    assert (
        await post_form(http, f"/o/{slug}/bilesenler/{component}/sil", {}, csrf_from=page_url)
    ).status_code == 303
    assert (await post_form(http, f"{page_url}/sil", {}, csrf_from=page_url)).headers[
        "location"
    ] == f"/o/{slug}/katalog"


async def test_own_check_on_a_shared_component(
    app: FastAPI, http: httpx2.AsyncClient, outbox: list[Mail], services: AppServices
) -> None:
    await with_examples(services)
    await signed_in(http, outbox, ALICE)
    slug = await create_org(http, "Alpha")
    component = await shared_component(services, "ornek-bulut", "eu-west")
    async with services.db.system("test lookup") as s:
        shared_service = await s.scalar(select(Service.id).where(Service.slug == "ornek-bulut"))
    page_url = f"/o/{slug}/servisler/{shared_service}"
    assert "ortak katalogda" in (await http.get(page_url)).text
    own = await post_form(
        http,
        f"/o/{slug}/bilesenler/{component}/kaynak",
        {"kind": "http", "url": "https://eu-west.bulut.example.invalid/"},
        csrf_from=page_url,
    )
    assert own.status_code == 303
    refused = await post_form(http, f"{page_url}/bilesen", {"name": "Yeni"}, csrf_from=page_url)
    assert refused.status_code == 403

    async with browser(app) as other:  # another org does not see Alice's check
        await signed_in(other, outbox, "bob@example.invalid")
        other_slug = await create_org(other, "Beta")
        page = await other.get(f"/o/{other_slug}/servisler/{shared_service}")
        assert "eu-west.bulut.example.invalid" not in page.text


async def test_openapi_shortcut(
    http: httpx2.AsyncClient, outbox: list[Mail], services: AppServices
) -> None:
    await signed_in(http, outbox, ALICE)
    slug = await create_org(http, "Alpha")
    response = await post_form(
        http,
        f"/o/{slug}/servisler/openapi",
        {"name": "Kargo API", "url": "https://api.kargo.example.invalid/openapi.json"},
        csrf_from=f"/o/{slug}/katalog",
    )
    assert response.status_code == 303
    async with services.db.system("test lookup") as s:
        kinds = (await s.scalars(select(Source.kind))).all()
    assert kinds == ["api_contract"]
    bad = await post_form(
        http,
        f"/o/{slug}/servisler/openapi",
        {"name": "X", "url": "ftp://x.example.invalid/"},
        csrf_from=f"/o/{slug}/katalog",
    )
    assert bad.status_code == 400


async def make_admin(
    http: httpx2.AsyncClient, services: AppServices, monkeypatch: pytest.MonkeyPatch, email: str
) -> None:
    async with services.db.session() as s:
        await s.execute(update(User).where(User.email == email).values(is_platform_admin=True))
    page = await post_form(http, "/hesap/2fa/baslat", {}, csrf_from="/hesap")
    secret = page.text.split('<code class="secret">')[1].split("<")[0]
    t0 = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)
    monkeypatch.setattr(services.two_factor, "_clock", lambda: t0)
    await post_form(http, "/hesap/2fa/onayla", {"code": code_at(secret, t0)}, csrf_from="/hesap")


async def test_proposal_review(
    app: FastAPI,
    http: httpx2.AsyncClient,
    outbox: list[Mail],
    services: AppServices,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await signed_in(http, outbox, ALICE)
    slug = await create_org(http, "Alpha")
    first = await create_service(http, slug, "Yerel Ödeme Kuruluşu", kind="saas_api")
    second = await create_service(http, slug, "Başka Servis", kind="saas_api")
    for service in (first, second):
        url = f"/o/{slug}/servisler/{service}"
        assert (
            await post_form(http, f"{url}/oner", {"note": "Herkes kullanıyor"}, csrf_from=url)
        ).status_code == 303
    again = await post_form(
        http, f"/o/{slug}/servisler/{first}/oner", {}, csrf_from=f"/o/{slug}/servisler/{first}"
    )
    assert again.status_code == 400
    assert (await http.get("/yonetim/katalog")).status_code == 404  # not an admin

    async with browser(app) as admin:
        await signed_in(admin, outbox, "admin@example.invalid")
        await make_admin(admin, services, monkeypatch, "admin@example.invalid")
        queue = await admin.get("/yonetim/katalog")
        assert queue.status_code == 200
        assert "Yerel Ödeme Kuruluşu" in queue.text
        ids = re.findall(r"/yonetim/katalog/([0-9a-f-]{36})/kabul", queue.text)
        assert len(ids) == 2
        accept = await post_form(
            admin, f"/yonetim/katalog/{ids[0]}/kabul", {"note": "ok"}, csrf_from="/yonetim/katalog"
        )
        assert accept.status_code == 303
        reject = await post_form(
            admin,
            f"/yonetim/katalog/{ids[1]}/ret",
            {"note": "kapsam dışı"},
            csrf_from="/yonetim/katalog",
        )
        assert reject.status_code == 303
        missing = await post_form(
            admin, f"/yonetim/katalog/{ids[1]}/ret", {}, csrf_from="/yonetim/katalog"
        )
        assert missing.status_code == 404  # already reviewed

    async with services.db.system("test check") as s:
        shared = (await s.scalars(select(Service).where(Service.org_id.is_(None)))).all()
        assert len(shared) == 1
        assert (
            await s.scalars(select(Source).where(Source.org_id.is_(None)))
        ).all() == []  # no URLs copied
    statuses = (await http.get(f"/o/{slug}/servisler/{first}")).text + (
        await http.get(f"/o/{slug}/servisler/{second}")
    ).text
    assert "Kabul edildi" in statuses
    assert "Reddedildi" in statuses
    assert "kapsam dışı" in statuses
