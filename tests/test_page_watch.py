"""Watched pages (CSS selectors) and manual announcements."""

from __future__ import annotations

import uuid
from typing import Any

import httpx
import httpx2
import pytest
from fastapi import FastAPI
from sqlalchemy import select

from org_support import create_org, create_service, join
from probe_support import env_for, job, outbound
from sentinel.accounts.mail import Mail
from sentinel.models import AuditEvent, Change, Component
from sentinel.sources.adapters import adapter_for
from sentinel.sources.config import validate_source
from sentinel.sources.pages import PageError, parse_page
from sentinel.web.services import AppServices
from support import post_form, signed_in

BASE = "https://vendor.test/duyurular/"
PAGE = """<!doctype html><html><body>
<!-- a comment -->
<ul class="news">
  <li class="item"><h3><a href="/duyuru/1">Planlı bakım 15.11.2026</a></h3>
      <time datetime="2026-10-19T09:00:00+03:00">19 Ekim</time><p>Uzun metin saklanmaz.</p></li>
  <li class="item"><h3>Şema güncellemesi</h3><span class="date">20 Ekim 2026</span>
      <a class="more" href="https://vendor.test/duyuru/2">Devamı</a></li>
  <li class="item"><h3><a href="javascript:alert(1)">Tuzak</a></h3></li>
  <li class="item"><p>Başlıksız öğe</p></li>
</ul></body></html>""".encode()
CONFIG = {"item_selector": "li.item", "title_selector": "h3", "date_selector": "time, .date"}


def test_parse_page() -> None:
    items = parse_page(PAGE, BASE, **CONFIG)
    assert [i["title"] for i in items] == ["Planlı bakım 15.11.2026", "Şema güncellemesi", "Tuzak"]
    first, second, trap = items
    assert first["link"] == "https://vendor.test/duyuru/1"
    assert first["published"] == "2026-10-19T06:00:00+00:00"  # the datetime attribute
    assert second["link"] == "https://vendor.test/duyuru/2"  # first link in the item
    assert second["published"] == "2026-10-19T21:00:00+00:00"  # 20 Ekim, Turkey midnight
    assert trap["link"] is None
    assert all(set(i) == {"id", "title", "link", "published"} for i in items)


def test_selectors_are_validated() -> None:
    with pytest.raises(PageError):
        parse_page(PAGE, BASE, item_selector="li[", title_selector="h3")
    with pytest.raises(ValueError, match="invalid selector"):
        validate_source("page_watch", {"url": BASE, "item_selector": "::", "title_selector": "h3"})
    assert parse_page(b"<html></html>", BASE, item_selector="li", title_selector="h3") == []
    with pytest.raises(PageError):
        parse_page(b"", BASE, item_selector="li", title_selector="h3")


async def watch(page: bytes, robots: str = "", **config: Any) -> Any:
    def site(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text=robots) if robots else httpx.Response(404)
        return httpx.Response(200, content=page, headers={"Content-Type": "text/html"})

    adapter = adapter_for("page_watch")
    assert adapter is not None
    async with outbound(site, rate_per_s=100, burst=10) as http:
        return await adapter.run(
            job(kind="page_watch", url=BASE, config={"url": BASE, **(config or CONFIG)}),
            env_for(http),
        )


async def test_page_watch_adapter() -> None:
    found = await watch(PAGE)
    assert found.ok
    assert found.snapshot is not None
    assert len(found.snapshot.items) == 3
    moved = await watch(PAGE, item_selector="article.post", title_selector="h2")
    assert moved.error_class == "no_items"  # the site changed: needs attention, not "no news"
    blocked = await watch(PAGE, robots="User-agent: *\nDisallow: /duyurular/\n")
    assert blocked.error_class == "blocked_by_robots"


# --- manual announcements ------------------------------------------------------------


async def test_manual_announcements(
    app: FastAPI, http: httpx2.AsyncClient, outbox: list[Mail], services: AppServices
) -> None:
    await signed_in(http, outbox, "alice@example.invalid")
    slug = await create_org(http, "Alpha")
    service = await create_service(http, slug)
    page_url = f"/o/{slug}/servisler/{service}"
    add_url = f"{page_url}/duyuru"
    async with services.db.system("test lookup") as s:
        component = await s.scalar(
            select(Component.id).where(Component.service_id == uuid.UUID(service))
        )
    assert "Duyuru ekle" in (await http.get(page_url)).text

    for form, message in (
        ({"title": "x", "category": "maintenance"}, "En az bir bileşen"),
        (
            {"title": "x", "category": "rumour", "component_ids": str(component)},
            "Geçersiz duyuru türü",
        ),
        (
            {
                "title": "x",
                "category": "other",
                "link": "javascript:alert(1)",
                "component_ids": str(component),
            },
            "http:// ya da https://",
        ),
        (
            {
                "title": "x",
                "category": "other",
                "effective_on": "yarın",
                "component_ids": str(component),
            },
            "Tarih ve saat geçersiz",
        ),
    ):
        refused = await post_form(http, add_url, form, csrf_from=page_url)
        assert refused.status_code == 400
        assert message in refused.text

    added = await post_form(
        http,
        add_url,
        {
            "title": "GİB: e-Arşiv rapor süresi değişiyor",
            "category": "deprecation",
            "link": "https://vendor.test/duyuru/9",
            "effective_on": "2027-01-01",
            "component_ids": str(component),
        },
        csrf_from=page_url,
    )
    assert added.status_code == 303
    page = await http.get(page_url)
    assert "e-Arşiv rapor süresi değişiyor" in page.text
    assert "Elle girildi" in page.text
    async with services.db.system("test lookup") as s:
        change = await s.scalar(select(Change))
        assert change is not None
        assert (change.origin, change.category, change.source_id) == ("manual", "deprecation", None)
        assert change.effective_at is not None
        assert change.effective_at.isoformat() == "2026-12-31T21:00:00+00:00"
        assert "announcement.added" in set((await s.scalars(select(AuditEvent.action))).all())

    async with join(app, outbox, http, slug, "viewer@example.invalid", role="viewer") as viewer:
        assert "Duyuru ekle" not in (await viewer.get(page_url)).text
        refused = await post_form(
            viewer, f"/o/{slug}/degisiklikler/{change.id}/sil", {}, csrf_from=page_url
        )
        assert refused.status_code == 403

    removed = await post_form(
        http, f"/o/{slug}/degisiklikler/{change.id}/sil", {}, csrf_from=page_url
    )
    assert removed.status_code == 303
    async with services.db.system("test lookup") as s:
        assert await s.get(Change, change.id) is None


def test_encodings() -> None:
    from sentinel.sources.pages import charset_of, decode_html

    turkish = "<p>Şubat ayında bakım</p>"
    assert decode_html(turkish.encode("cp1254"), "windows-1254") == turkish
    assert decode_html(f'<meta charset="iso-8859-9">{turkish}'.encode("iso-8859-9")).endswith(
        turkish
    )
    assert decode_html(turkish.encode(), "no-such-charset") == turkish  # falls back to UTF-8
    assert charset_of('text/html; charset="utf-8"') == "utf-8"
    assert charset_of(None) is None
