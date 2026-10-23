"""Alert settings in the panel: channels, recipients, rules, test messages, retention."""

from __future__ import annotations

import re
import uuid
from datetime import timedelta

import httpx2
from fastapi import FastAPI
from sqlalchemy import select

from org_support import browser, create_org, create_product, join
from sentinel.accounts.mail import Mail
from sentinel.db.base import utcnow
from sentinel.engine.retention import run_retention
from sentinel.models import (
    AlertChannel,
    AlertDelivery,
    AlertEvent,
    AlertRecipient,
    AlertRule,
    Organization,
)
from sentinel.web.services import AppServices
from support import post_form, signed_in

SECRET = re.compile(r"<code>([A-Za-z0-9_-]{40,})</code>")


async def owner_org(
    app: FastAPI, outbox: list[Mail], client: httpx2.AsyncClient
) -> tuple[str, uuid.UUID]:
    await signed_in(client, outbox, "sahip@example.invalid")
    slug = await create_org(client, "Alfa")
    return slug, await org_id(app, slug)


async def org_id(app: FastAPI, slug: str) -> uuid.UUID:
    services: AppServices = app.state.services
    async with services.db.system("test lookup") as s:
        found = await s.scalar(select(Organization.id).where(Organization.slug == slug))
    assert found is not None
    return found


async def add_channel(client: httpx2.AsyncClient, slug: str, **form: str) -> httpx2.Response:
    return await post_form(
        client, f"/o/{slug}/uyarilar/kanal", form, csrf_from=f"/o/{slug}/uyarilar"
    )


async def channel_named(services: AppServices, name: str) -> AlertChannel:
    async with services.db.system("test lookup") as s:
        channel = await s.scalar(select(AlertChannel).where(AlertChannel.name == name))
    assert channel is not None
    return channel


async def test_webhook_secret_is_shown_once_and_url_never(
    app: FastAPI, outbox: list[Mail], services: AppServices
) -> None:
    async with browser(app) as owner:
        slug, org = await owner_org(app, outbox, owner)
        created = await add_channel(
            owner, slug, name="Ops", kind="webhook", url="https://hooks.example/in/s3cr3t-path"
        )
        assert created.status_code == 200
        assert created.headers["cache-control"] == "no-store"
        found = SECRET.search(created.text)
        assert found is not None
        signing = found.group(1)
        page = await owner.get(f"/o/{slug}/uyarilar")
        assert signing not in page.text
        assert "s3cr3t-path" not in page.text
        assert "hooks.example" in page.text  # the host only
        channel = await channel_named(services, "Ops")
        async with services.db.system("test open") as s:
            assert channel.sealed is not None
            opened = await services.credentials.open_payload(s, org, channel.id, channel.sealed)
        assert opened == {"url": "https://hooks.example/in/s3cr3t-path", "secret": signing}

        rotated = await post_form(
            owner,
            f"/o/{slug}/uyarilar/kanal/{channel.id}/imza",
            {},
            csrf_from=f"/o/{slug}/uyarilar",
        )
        again = SECRET.search(rotated.text)
        assert again is not None
        assert again.group(1) != signing
        moved = await post_form(
            owner,
            f"/o/{slug}/uyarilar/kanal/{channel.id}/adres",
            {"url": "https://other.example/hook"},
            csrf_from=f"/o/{slug}/uyarilar",
        )
        assert moved.status_code == 303
        channel = await channel_named(services, "Ops")
        async with services.db.system("test open") as s:
            assert channel.sealed is not None
            opened = await services.credentials.open_payload(s, org, channel.id, channel.sealed)
        assert opened == {"url": "https://other.example/hook", "secret": again.group(1)}
        assert channel.target_host == "other.example"


async def test_only_public_https_targets(app: FastAPI, outbox: list[Mail]) -> None:
    async with browser(app) as owner:
        slug, _ = await owner_org(app, outbox, owner)
        for url in (
            "http://hooks.example/in",
            "https://10.0.0.5/in",
            "https://127.0.0.1/in",
            "https://localhost/in",
            "https://intranet/in",
            "https://hooks.internal/in",
            "https://user:pw@hooks.example/in",
            "",
        ):
            response = await add_channel(owner, slug, name="Ops", kind="slack", url=url)
            assert response.status_code == 400, url
            assert "herkese açık bir https adresi" in response.text
        for form in (
            {"name": "", "kind": "email"},
            {"name": "Ops", "kind": "sms"},
            {"name": "Ops", "kind": "email", "quiet_start": "22:00"},
            {"name": "Ops", "kind": "email", "quiet_start": "25:00", "quiet_end": "08:00"},
            {"name": "Ops", "kind": "telegram"},  # no bot in this install
        ):
            assert (await add_channel(owner, slug, **form)).status_code == 400, form


async def test_roles(app: FastAPI, outbox: list[Mail], services: AppServices) -> None:
    async with browser(app) as owner:
        slug, _ = await owner_org(app, outbox, owner)
        assert (await add_channel(owner, slug, name="Posta", kind="email")).status_code == 303
        channel = await channel_named(services, "Posta")
        invited = await post_form(
            owner,
            f"/o/{slug}/uyarilar/kanal/{channel.id}/alici",
            {"email": "Dis@Ortak.example.invalid"},
            csrf_from=f"/o/{slug}/uyarilar",
        )
        assert invited.status_code == 303
        assert outbox[-1].to == "dis@ortak.example.invalid"
        assert "/abone/onay/" in outbox[-1].text

        async with join(app, outbox, owner, slug, "uye@example.invalid") as member:
            page = await member.get(f"/o/{slug}/uyarilar")
            assert page.status_code == 200
            assert "dis@ortak.example.invalid" not in page.text  # others' addresses: admins only
            assert "Kanal ekle" not in page.text
            denied = await add_channel(member, slug, name="Benim", kind="email")
            assert denied.status_code == 403
            mine = await post_form(
                member,
                f"/o/{slug}/uyarilar/kanal/{channel.id}/ben",
                {},
                csrf_from=f"/o/{slug}/uyarilar",
            )
            assert mine.status_code == 303
            async with services.db.system("test check") as s:
                rows = {r.address: r for r in (await s.scalars(select(AlertRecipient))).all()}
            assert rows["uye@example.invalid"].confirmed_at is not None  # own verified address
            assert rows["dis@ortak.example.invalid"].confirmed_at is None
            other = rows["dis@ortak.example.invalid"].id
            refused = await post_form(
                member, f"/o/{slug}/uyarilar/alici/{other}/sil", {}, csrf_from=f"/o/{slug}/uyarilar"
            )
            assert refused.status_code == 403
            gone = await post_form(
                member,
                f"/o/{slug}/uyarilar/alici/{rows['uye@example.invalid'].id}/sil",
                {},
                csrf_from=f"/o/{slug}/uyarilar",
            )
            assert gone.status_code == 303

        async with join(app, outbox, owner, slug, "izleyici@example.invalid", "viewer") as viewer:
            assert (await viewer.get(f"/o/{slug}/uyarilar")).status_code == 403
            assert "/uyarilar" not in (await viewer.get(f"/o/{slug}")).text


async def test_rules_and_test_messages(
    app: FastAPI, outbox: list[Mail], services: AppServices
) -> None:
    async with browser(app) as owner:
        slug, _ = await owner_org(app, outbox, owner)
        product = await create_product(owner, slug, "Fatura")
        await add_channel(owner, slug, name="Kanca", kind="slack", url="https://hooks.example/s")
        channel = await channel_named(services, "Kanca")
        base = f"/o/{slug}/uyarilar"
        bad = await post_form(
            owner,
            f"{base}/kural",
            {"name": "Hiçbiri", "channel_id": str(channel.id), "min_severity": "low"},
            csrf_from=base,
        )
        assert bad.status_code == 400  # no kinds chosen
        for form in (
            {"channel_id": "not-a-uuid"},
            {"channel_id": str(uuid.uuid4())},
            {"channel_id": str(channel.id), "product_id": str(uuid.uuid4())},
            {"channel_id": str(channel.id), "service_id": str(uuid.uuid4())},
        ):
            response = await post_form(
                owner,
                f"{base}/kural",
                {"name": "x", "kinds": "incident", "min_severity": "low", **form},
                csrf_from=base,
            )
            assert response.status_code == 404, form
        token = (await owner.get(base)).text
        csrf = re.search(r'name="csrf" value="([^"]+)"', token)
        assert csrf is not None
        made = await owner.post(
            f"{base}/kural",
            data={
                "name": "Fatura kırılırsa",
                "channel_id": str(channel.id),
                "kinds": ["change", "incident"],
                "min_severity": "high",
                "product_id": product,
                "csrf": csrf.group(1),
            },
        )
        assert made.status_code == 303, made.text
        async with services.db.system("test check") as s:
            rule = await s.scalar(select(AlertRule))
        assert rule is not None
        assert (rule.kinds, rule.min_severity, str(rule.product_id)) == (
            ["change", "incident"],
            "high",
            product,
        )
        page = await owner.get(base)
        assert "Fatura kırılırsa" in page.text
        paused = await post_form(owner, f"{base}/kural/{rule.id}", {}, csrf_from=base)
        assert paused.status_code == 303
        async with services.db.system("test check") as s:
            row = await s.get(AlertRule, rule.id)
            assert row is not None
            assert row.enabled is False

        sent = await post_form(owner, f"{base}/kanal/{channel.id}/test", {}, csrf_from=base)
        assert sent.headers["location"] == f"{base}?ok=test"
        assert "Test mesajı sıraya alındı" in (await owner.get(sent.headers["location"])).text
        again = await post_form(owner, f"{base}/kanal/{channel.id}/test", {}, csrf_from=base)
        assert again.status_code == 400  # once a minute
        async with services.db.system("test check") as s:
            delivery = await s.scalar(select(AlertDelivery))
            event = await s.scalar(select(AlertEvent))
        assert delivery is not None
        assert event is not None
        assert (event.subject_type, event.product_id, event.title) == ("test", None, "Kanca")
        log = await owner.get(base)
        assert "Sırada" in log.text

        email = await add_channel(owner, slug, name="Posta", kind="email")
        assert email.status_code == 303
        posta = await channel_named(services, "Posta")
        lonely = await post_form(owner, f"{base}/kanal/{posta.id}/test", {}, csrf_from=base)
        assert lonely.status_code == 400  # nobody to send to

        assert (
            await post_form(owner, f"{base}/kural/{rule.id}/sil", {}, csrf_from=base)
        ).status_code == 303
        assert (
            await post_form(owner, f"{base}/kanal/{channel.id}/sil", {}, csrf_from=base)
        ).status_code == 303
        assert "Kanca" not in (await owner.get(base)).text


async def test_reenabling_a_channel_clears_its_failures(
    app: FastAPI, outbox: list[Mail], services: AppServices
) -> None:
    async with browser(app) as owner:
        slug, _ = await owner_org(app, outbox, owner)
        await add_channel(owner, slug, name="Kanca", kind="slack", url="https://hooks.example/s")
        channel = await channel_named(services, "Kanca")
        async with services.db.system("test setup") as s:
            row = await s.get(AlertChannel, channel.id)
            assert row is not None
            row.enabled, row.failures, row.disabled_reason = False, 10, "too_many_failures"
        page = await owner.get(f"/o/{slug}/uyarilar")
        assert "üst üste 10 gönderim başarısız oldu" in page.text
        saved = await post_form(
            owner,
            f"/o/{slug}/uyarilar/kanal/{channel.id}",
            {"quiet_start": "22:00", "quiet_end": "07:30", "enabled": "true", "daily": "true"},
            csrf_from=f"/o/{slug}/uyarilar",
        )
        assert saved.status_code == 303
        channel = await channel_named(services, "Kanca")
        assert (channel.enabled, channel.failures, channel.disabled_reason) == (True, 0, None)
        assert (channel.quiet_start, channel.quiet_end, channel.daily) == ("22:00", "07:30", True)
        assert channel.quiet_bypass_high is False  # unchecked box


async def test_retention_drops_unconfirmed_and_unsubscribed(
    app: FastAPI, outbox: list[Mail], services: AppServices
) -> None:
    async with browser(app) as owner:
        slug, org = await owner_org(app, outbox, owner)
        await add_channel(owner, slug, name="Posta", kind="email")
        channel = await channel_named(services, "Posta")
    now = utcnow()
    async with services.db.system("test setup") as s:
        s.add_all(
            [
                AlertRecipient(
                    org_id=org,
                    channel_id=channel.id,
                    address="eski@x.example.invalid",
                    created_at=now - timedelta(hours=49),
                ),
                AlertRecipient(
                    org_id=org,
                    channel_id=channel.id,
                    address="yeni@x.example.invalid",
                    created_at=now - timedelta(hours=47),
                ),
                AlertRecipient(
                    org_id=org,
                    channel_id=channel.id,
                    address="cikti@x.example.invalid",
                    confirmed_at=now - timedelta(days=60),
                    unsubscribed_at=now - timedelta(days=31),
                ),
                AlertRecipient(
                    org_id=org,
                    channel_id=channel.id,
                    address="kalir@x.example.invalid",
                    confirmed_at=now - timedelta(days=60),
                ),
            ]
        )
    report = await run_retention(services.db)
    assert report.alert_recipients == 2
    async with services.db.system("test check") as s:
        left = sorted(r.address for r in (await s.scalars(select(AlertRecipient))).all())
    assert left == ["kalir@x.example.invalid", "yeni@x.example.invalid"]
