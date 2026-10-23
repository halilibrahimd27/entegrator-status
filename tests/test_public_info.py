"""Methodology, bot page, corrections, robots.txt and the sitemap."""

from __future__ import annotations

from datetime import timedelta

import httpx2
import pytest
from fastapi import FastAPI
from sqlalchemy import select, update

from org_support import browser, create_org, create_product
from sentinel.accounts.mail import Mail
from sentinel.db.base import utcnow
from sentinel.engine.retention import run_retention
from sentinel.models import AuditEvent, CorrectionRequest
from sentinel.web.services import AppServices
from support import post_form, signed_in
from test_catalog_ui import make_admin


async def test_method_and_bot_pages(http: httpx2.AsyncClient, services: AppServices) -> None:
    method = await http.get("/yontem")
    assert method.status_code == 200
    assert "en az iki sağlıklı ölçüm noktasının" in method.text
    bot = await http.get("/hakkinda/bot")
    assert bot.status_code == 200
    assert "EntegratorStatus/" in bot.text
    assert f"{services.settings.public_base}/hakkinda/bot" in bot.text  # this install's UA
    assert "User-agent: entegratorstatus" in bot.text
    home = await http.get("/")
    for link in ('href="/yontem"', 'href="/hakkinda/bot"', 'href="/duzeltme"'):
        assert link in home.text


async def test_robots_and_sitemap(app: FastAPI, outbox: list[Mail], services: AppServices) -> None:
    async with browser(app) as owner, browser(app) as visitor:
        robots = await visitor.get("/robots.txt")
        assert "Disallow: /o/" in robots.text
        assert f"Sitemap: {services.settings.public_base}/sitemap.xml" in robots.text
        await signed_in(owner, outbox, "sahip@example.invalid")
        slug = await create_org(owner, "Alfa")
        product = await create_product(owner, slug, "Fatura")
        assert f"/s/{slug}<" not in (await visitor.get("/sitemap.xml")).text
        await post_form(
            owner,
            f"/o/{slug}/durum-sayfasi",
            {"title": "Alfa", "product_ids": product, "enabled": "true"},
            csrf_from=f"/o/{slug}/durum-sayfasi",
        )
        sitemap = await visitor.get("/sitemap.xml")
        assert sitemap.headers["content-type"].startswith("application/xml")
        assert f"{services.settings.public_base}/s/{slug}</loc>" in sitemap.text


async def test_correction_flow(
    app: FastAPI,
    http: httpx2.AsyncClient,
    outbox: list[Mail],
    services: AppServices,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    form = await http.get("/duzeltme")
    assert form.status_code == 200
    trap = await post_form(
        http,
        "/duzeltme",
        {"subject": "x", "body": "bir şey yanlış gibi", "website": "spam"},
        csrf_from="/duzeltme",
    )
    assert "Teşekkürler" in trap.text
    assert outbox == []
    short = await post_form(
        http, "/duzeltme", {"subject": "GİB", "body": "kısa"}, csrf_from="/duzeltme"
    )
    assert short.status_code == 400
    assert 'value="GİB"' in short.text  # the form keeps what was typed
    bad = await post_form(
        http,
        "/duzeltme",
        {"subject": "GİB", "body": "Bu ölçüm yanlış.", "contact": "not an address"},
        csrf_from="/duzeltme",
    )
    assert bad.status_code == 400
    sent = await post_form(
        http,
        "/duzeltme",
        {
            "subject": "Entegratör X durum sayfası",
            "body": "Dün 14:00'teki kesinti bizim bakımımızdı, önceden duyurmuştuk.",
            "contact": "Destek@Saglayici.example.invalid",
        },
        csrf_from="/duzeltme",
    )
    assert sent.status_code == 200
    (operator,) = outbox
    assert operator.to == services.settings.contact_email
    assert "bizim bakımımızdı" in operator.text
    assert (
        await http.post("/duzeltme", data={"subject": "x", "body": "y" * 20})
    ).status_code == 403

    assert (await http.get("/yonetim/duzeltmeler")).status_code in (303, 404)  # not signed in
    async with browser(app) as admin:
        await signed_in(admin, outbox, "admin@example.invalid")
        assert (await admin.get("/yonetim/duzeltmeler")).status_code == 404  # not an admin
        await make_admin(admin, services, monkeypatch, "admin@example.invalid")
        queue = await admin.get("/yonetim/duzeltmeler")
        assert "Entegratör X durum sayfası" in queue.text
        async with services.db.system("test lookup") as s:
            row = await s.scalar(select(CorrectionRequest))
        assert row is not None
        assert row.contact == "destek@saglayici.example.invalid"
        wrong = await post_form(
            admin,
            f"/yonetim/duzeltmeler/{row.id}/karar",
            {"decision": "maybe"},
            csrf_from="/yonetim/duzeltmeler",
        )
        assert wrong.status_code == 400
        decided = await post_form(
            admin,
            f"/yonetim/duzeltmeler/{row.id}/karar",
            {"decision": "accepted", "note": "Bakım penceresi eklendi."},
            csrf_from="/yonetim/duzeltmeler",
        )
        assert decided.status_code == 303
        again = await post_form(
            admin,
            f"/yonetim/duzeltmeler/{row.id}/karar",
            {"decision": "rejected"},
            csrf_from="/yonetim/duzeltmeler",
        )
        assert again.status_code == 404  # decided once
    answer = outbox[-1]
    assert answer.to == "destek@saglayici.example.invalid"
    assert "kabul edildi" in answer.text
    assert "Bakım penceresi eklendi." in answer.text
    async with services.db.system("test check") as s:
        actions = (await s.scalars(select(AuditEvent.action))).all()
    assert "correction.accepted" in actions

    async with services.db.system("test age") as s:
        await s.execute(update(CorrectionRequest).values(decided_at=utcnow() - timedelta(days=366)))
    assert (await run_retention(services.db)).corrections == 1


async def test_corrections_are_throttled(http: httpx2.AsyncClient) -> None:
    codes = []
    for n in range(6):
        response = await post_form(
            http,
            "/duzeltme",
            {"subject": f"Konu {n}", "body": "Bu ölçüm yanlış görünüyor."},
            csrf_from="/duzeltme",
        )
        codes.append(response.status_code)
    assert codes == [200] * 5 + [429]
