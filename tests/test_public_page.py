"""Public status pages: off by default, chosen products only, cacheable, subscribable."""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timedelta
from xml.etree import ElementTree

import httpx2
from fastapi import FastAPI
from sqlalchemy import select

from org_support import browser, create_org, create_product, join
from sentinel.accounts.mail import Mail, MemoryMailer
from sentinel.alerts.channels import Senders
from sentinel.alerts.delivery import DeliveryWorker
from sentinel.alerts.events import run_events
from sentinel.db.base import utcnow
from sentinel.models import (
    AlertChannel,
    AlertRecipient,
    Incident,
    IncidentKind,
    IncidentStatus,
    Organization,
)
from sentinel.security.http import OutboundClient
from sentinel.web.services import AppServices
from support import post_form, signed_in
from test_impact import chain

ATOM = "{http://www.w3.org/2005/Atom}"


async def setup_page(
    app: FastAPI, outbox: list[Mail], owner: httpx2.AsyncClient, **extra: str
) -> tuple[str, str, str]:
    """An org with two products; the first one on an enabled page. (slug, shown, hidden)"""
    await signed_in(owner, outbox, "sahip@example.invalid")
    slug = await create_org(owner, "Alfa Yazılım")
    shown = await create_product(owner, slug, "Fatura <b>Pro</b>")
    hidden = await create_product(owner, slug, "Gizli Ürün")
    saved = await post_form(
        owner,
        f"/o/{slug}/durum-sayfasi",
        {
            "title": "Alfa durum",
            "intro": "Ürünlerimizin durumu",
            "product_ids": shown,
            "enabled": "true",
            "subscriptions": "true",
            **extra,
        },
        csrf_from=f"/o/{slug}/durum-sayfasi",
    )
    assert saved.status_code == 303, saved.text
    return slug, shown, hidden


async def org_id(services: AppServices, slug: str) -> uuid.UUID:
    async with services.db.system("test lookup") as s:
        found = await s.scalar(select(Organization.id).where(Organization.slug == slug))
    assert found is not None
    return found


async def test_off_until_switched_on(
    app: FastAPI, outbox: list[Mail], services: AppServices
) -> None:
    async with browser(app) as owner, browser(app) as visitor:
        await signed_in(owner, outbox, "sahip@example.invalid")
        slug = await create_org(owner, "Alfa")
        for path in (
            f"/s/{slug}",
            f"/s/{slug}/akis.atom",
            f"/s/{slug}/rozet.svg",
            f"/s/{slug}/abone",
        ):
            assert (await visitor.get(path)).status_code == 404, path
        assert (await visitor.get("/s/olmayan")).status_code == 404
        refused = await post_form(
            owner,
            f"/o/{slug}/durum-sayfasi",
            {"title": "Alfa", "enabled": "true"},
            csrf_from=f"/o/{slug}/durum-sayfasi",
        )
        assert refused.status_code == 400  # nothing to show yet
        foreign = await post_form(
            owner,
            f"/o/{slug}/durum-sayfasi",
            {"title": "Alfa", "product_ids": str(uuid.uuid4())},
            csrf_from=f"/o/{slug}/durum-sayfasi",
        )
        assert foreign.status_code == 404
        async with join(app, outbox, owner, slug, "uye@example.invalid") as member:
            assert (await member.get(f"/o/{slug}/durum-sayfasi")).status_code == 403
            assert "/durum-sayfasi" not in (await member.get(f"/o/{slug}")).text


async def test_page_shows_chosen_products_and_own_incidents(
    app: FastAPI, outbox: list[Mail], services: AppServices
) -> None:
    async with browser(app) as owner, browser(app) as visitor:
        slug, shown, hidden = await setup_page(app, outbox, owner)
        page = await visitor.get(f"/s/{slug}")
        assert page.status_code == 200
        assert "Fatura &lt;b&gt;Pro&lt;/b&gt;" in page.text  # escaped
        assert "Gizli Ürün" not in page.text
        assert "Tüm ürünler çalışıyor" in page.text
        assert "set-cookie" not in page.headers  # same page for everyone: cacheable
        assert page.headers["cache-control"] == "public, max-age=60"
        again = await visitor.get(f"/s/{slug}", headers={"If-None-Match": page.headers["etag"]})
        assert again.status_code == 304
        assert f'href="https://status.example.invalid/s/{slug}"' in page.text  # canonical

        opened = await post_form(
            owner,
            f"/o/{slug}/urunler/{shown}/olaylar",
            {
                "title": "Fatura gönderimi yavaş <script>",
                "body": "İnceliyoruz.",
                "status": "investigating",
            },
            csrf_from=f"/o/{slug}/urunler/{shown}",
        )
        assert opened.status_code == 303
        await post_form(
            owner,
            f"/o/{slug}/urunler/{hidden}/olaylar",
            {"title": "Gizli olay", "body": "x", "status": "investigating"},
            csrf_from=f"/o/{slug}/urunler/{hidden}",
        )
        page = await visitor.get(f"/s/{slug}")
        assert "Devam eden bir olay var" in page.text
        assert "Fatura gönderimi yavaş &lt;script&gt;" in page.text
        assert "Gizli olay" not in page.text

        feed = await visitor.get(f"/s/{slug}/akis.atom")
        assert feed.headers["content-type"].startswith("application/atom+xml")
        root = ElementTree.fromstring(feed.content)  # noqa: S314 - our own output
        titles = [e.findtext(f"{ATOM}title") for e in root.iter(f"{ATOM}entry")]
        assert titles == ["Fatura <b>Pro</b>: Fatura gönderimi yavaş <script> — İnceleniyor"]

        badge = await visitor.get(f"/s/{slug}/rozet.svg")
        assert badge.headers["content-type"] == "image/svg+xml"
        assert "Olay var" in badge.text
        assert "<script" not in badge.text


async def test_upstream_trouble_is_shown_with_a_disclaimer(
    app: FastAPI, outbox: list[Mail], services: AppServices
) -> None:
    ids = await chain(services)
    async with browser(app) as owner, browser(app) as visitor:
        slug, shown, _ = await setup_page(app, outbox, owner, show_dependencies="true")
        base = f"/o/{slug}/urunler/{shown}"
        await post_form(
            owner, f"{base}/bagimlilik", {"to_component_id": str(ids["api"])}, csrf_from=base
        )
        await post_form(
            owner, f"{base}/bagimlilik", {"to_component_id": str(ids["central"])}, csrf_from=base
        )
        page = await visitor.get(f"/s/{slug}")
        assert "Entegratör X — e-Fatura API" in page.text  # dependencies shown on request
        assert 'class="bars"' in page.text
        async with services.db.system("test setup") as s:
            s.add(
                Incident(
                    kind=IncidentKind.AUTO,
                    status=IncidentStatus.OPEN,
                    component_id=ids["ca"],  # third degree: not shown publicly
                    started_at=utcnow(),
                )
            )
        assert "Tüm ürünler çalışıyor" in (await visitor.get(f"/s/{slug}")).text
        async with services.db.system("test setup") as s:
            s.add(
                Incident(
                    kind=IncidentKind.AUTO,
                    status=IncidentStatus.OPEN,
                    component_id=ids["central"],
                    started_at=utcnow(),
                )
            )
        page = await visitor.get(f"/s/{slug}")
        assert "Kullanılan servislerden birinde sorun gözleniyor" in page.text
        assert "bağımsız ölçümlerdir" in page.text
        assert "Bağımlı bir serviste sorun" in (await visitor.get(f"/s/{slug}/rozet.svg")).text


def worker_for(services: AppServices, mailer: MemoryMailer, now: datetime) -> DeliveryWorker:
    http = OutboundClient(user_agent="EntegratorStatus/test")
    return DeliveryWorker(
        services.db,
        Senders(http, mailer),
        services.credentials,
        public_base_url="https://status.example.invalid",
        unsubscribe_key=services.subscriptions.unsubscribe_key,
        clock=lambda: now,
    )


async def test_subscribers_get_own_incident_updates_only(
    app: FastAPI, outbox: list[Mail], services: AppServices
) -> None:
    ids = await chain(services)
    async with browser(app) as owner, browser(app) as visitor:
        slug, shown, hidden = await setup_page(app, outbox, owner)
        org = await org_id(services, slug)
        await post_form(
            owner,
            f"/o/{slug}/urunler/{shown}/bagimlilik",
            {"to_component_id": str(ids["api"])},
            csrf_from=f"/o/{slug}/urunler/{shown}",
        )
        form = await visitor.get(f"/s/{slug}/abone")
        assert form.status_code == 200
        assert form.headers["cache-control"] == "no-store"
        trap = await post_form(
            visitor,
            f"/s/{slug}/abone",
            {"email": "bot@x.example.invalid", "website": "spam"},
            csrf_from=f"/s/{slug}/abone",
        )
        assert "onay bağlantısı gönderdik" in trap.text
        sent_before = len(outbox)
        done = await post_form(
            visitor,
            f"/s/{slug}/abone",
            {"email": "Musteri@Ornek.example.invalid"},
            csrf_from=f"/s/{slug}/abone",
        )
        assert done.status_code == 200
        assert len(outbox) == sent_before + 1  # the honeypot sent nothing
        mail = outbox[-1]
        assert mail.to == "musteri@ornek.example.invalid"
        assert "durum sayfasının e-posta bildirimlerine" in mail.text
        found = re.search(r"https://status\.example\.invalid(/abone/onay/\S+)", mail.text)
        assert found is not None
        confirm_page = await visitor.get(found.group(1))
        assert "durum sayfasının e-posta bildirimlerine" in confirm_page.text
        assert (await visitor.post(found.group(1))).status_code == 200
        again = await post_form(
            visitor,
            f"/s/{slug}/abone",
            {"email": "musteri@ornek.example.invalid"},
            csrf_from=f"/s/{slug}/abone",
        )
        assert again.status_code == 200
        assert len(outbox) == sent_before + 1  # already subscribed: no new mail

        alerts = await owner.get(f"/o/{slug}/uyarilar")
        assert "Durum sayfası aboneleri" not in alerts.text  # hidden from alert settings
        async with services.db.system("test lookup") as s:
            channel = await s.scalar(select(AlertChannel).where(AlertChannel.public.is_(True)))
        assert channel is not None
        assert (
            await post_form(
                owner,
                f"/o/{slug}/uyarilar/kanal/{channel.id}/test",
                {},
                csrf_from=f"/o/{slug}/uyarilar",
            )
        ).status_code == 404

        await run_events(services.db)  # sets the cursor
        later = utcnow() + timedelta(seconds=1)
        await post_form(
            owner,
            f"/o/{slug}/urunler/{shown}/olaylar",
            {
                "title": "Gönderim gecikmesi",
                "body": "Kuyruk birikti, eritiyoruz.",
                "status": "identified",
            },
            csrf_from=f"/o/{slug}/urunler/{shown}",
        )
        await post_form(
            owner,
            f"/o/{slug}/urunler/{hidden}/olaylar",
            {"title": "Gizli", "body": "x", "status": "investigating"},
            csrf_from=f"/o/{slug}/urunler/{hidden}",
        )
        async with services.db.system("test setup") as s:
            s.add(
                Incident(
                    kind=IncidentKind.AUTO,
                    status=IncidentStatus.OPEN,
                    component_id=ids["api"],
                    started_at=later,
                    created_at=later,
                )
            )
        report = await run_events(services.db, clock=lambda: later + timedelta(minutes=1))
        assert report.deliveries == 1  # the notice; the upstream incident has no rules here
        mailer = MemoryMailer()
        sent = await worker_for(services, mailer, later + timedelta(minutes=2)).run()
        assert sent.sent == 1
        (notice,) = mailer.outbox
        assert notice.to == "musteri@ornek.example.invalid"
        assert notice.subject == "Fatura <b>Pro</b>: Gönderim gecikmesi — Neden bulundu"
        assert "Kuyruk birikti, eritiyoruz." in notice.text
        assert f"https://status.example.invalid/s/{slug}" in notice.text
        assert "/o/" not in notice.text  # never a panel link for the public
        assert "List-Unsubscribe" in notice.headers
        async with services.db.system("test check") as s:
            recipients = (
                await s.scalars(select(AlertRecipient).where(AlertRecipient.org_id == org))
            ).all()
        assert [r.address for r in recipients] == ["musteri@ornek.example.invalid"]


async def test_subscribing_is_throttled_and_validated(
    app: FastAPI, outbox: list[Mail], services: AppServices
) -> None:
    async with browser(app) as owner, browser(app) as visitor:
        slug, _, _ = await setup_page(app, outbox, owner)
        bad = await post_form(
            visitor, f"/s/{slug}/abone", {"email": "not-an-address"}, csrf_from=f"/s/{slug}/abone"
        )
        assert bad.status_code == 400
        no_token = await visitor.post(f"/s/{slug}/abone", data={"email": "a@b.example.invalid"})
        assert no_token.status_code == 403  # the form token is required
        codes: list[int] = []
        for n in range(6):
            response = await post_form(
                visitor,
                f"/s/{slug}/abone",
                {"email": f"kisi{n}@x.example.invalid"},
                csrf_from=f"/s/{slug}/abone",
            )
            codes.append(response.status_code)
        assert codes[-1] == 429
        assert 200 in codes


async def test_subscriptions_can_be_switched_off(
    app: FastAPI, outbox: list[Mail], services: AppServices
) -> None:
    async with browser(app) as owner, browser(app) as visitor:
        slug, shown, _ = await setup_page(app, outbox, owner)
        await post_form(
            owner,
            f"/o/{slug}/durum-sayfasi",
            {"title": "Alfa durum", "product_ids": shown, "enabled": "true"},
            csrf_from=f"/o/{slug}/durum-sayfasi",
        )
        page = await visitor.get(f"/s/{slug}")
        assert "E-postayla haber al" not in page.text
        assert (await visitor.get(f"/s/{slug}/abone")).status_code == 404
        settings = await owner.get(f"/o/{slug}/durum-sayfasi")
        assert f"https://status.example.invalid/s/{slug}/rozet.svg" in settings.text
