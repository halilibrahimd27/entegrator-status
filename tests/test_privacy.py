"""KVKK: data export, account deletion, closing organizations, retention."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import httpx2
import pytest
from fastapi import FastAPI
from sqlalchemy import func, select, update

from org_support import browser, create_org, join, new_token
from sentinel.accounts.mail import Mail
from sentinel.accounts.totp import code_at
from sentinel.db.base import utcnow
from sentinel.engine.retention import run_retention
from sentinel.models import (
    AuditEvent,
    Membership,
    OneTimeToken,
    Organization,
    User,
    UserSession,
)
from sentinel.web.services import AppServices
from support import PASSWORD, login, post_form, signed_in, signup

ALICE = "alice@example.invalid"
BOB = "bob@example.invalid"


async def test_export_contains_my_data_and_no_secrets(
    http: httpx2.AsyncClient, outbox: list[Mail], services: AppServices
) -> None:
    await signed_in(http, outbox, ALICE)
    await create_org(http, "Alpha")
    response = await http.get("/hesap/veri")
    assert response.status_code == 200
    assert response.headers["content-disposition"].startswith("attachment;")
    data = response.json()
    assert data["account"]["email"] == ALICE
    assert data["memberships"] == [
        {
            "organization": "Alpha",
            "slug": "alpha",
            "role": "owner",
            "joined_at": data["memberships"][0]["joined_at"],
        }
    ]
    assert {e["action"] for e in data["audit_events"]} >= {
        "account.signup",
        "account.login",
        "org.created",
    }
    assert len(data["sessions"]) == 1

    async with services.db.session() as s:
        user = (await s.scalars(select(User))).one()
    text = json.dumps(data)
    for forbidden in ("password_hash", "totp_secret", "token_hash", user.password_hash):
        assert forbidden not in text


async def test_delete_account_closes_orgs_it_was_alone_in(
    http: httpx2.AsyncClient, outbox: list[Mail], services: AppServices
) -> None:
    await signed_in(http, outbox, ALICE)
    slug = await create_org(http, "Solo")

    wrong = await post_form(
        http, "/hesap/sil", {"password": "yanlis-parola-0000"}, csrf_from="/hesap"
    )
    assert wrong.status_code == 400
    assert "Parola hatalı" in wrong.text

    done = await post_form(http, "/hesap/sil", {"password": PASSWORD}, csrf_from="/hesap")
    assert done.status_code == 200
    assert "Hesabın silindi" in done.text
    assert "Hesabım" not in done.text  # rendered signed out
    assert (await http.get("/hesap")).headers["location"] == "/giris"
    assert (await login(http, ALICE)).status_code == 401

    async with services.db.system("test check") as s:
        assert await s.scalar(select(func.count()).select_from(User)) == 0
        org = (await s.scalars(select(Organization).where(Organization.slug == slug))).one()
        assert org.deleted_at is not None
        assert org.purge_after is not None
        assert timedelta(days=29) < org.purge_after - org.deleted_at <= timedelta(days=30)
        actors = (await s.scalars(select(AuditEvent.actor_user_id))).all()
        actions = (await s.scalars(select(AuditEvent.action))).all()
    assert set(actors) == {None}  # the history stays, the person does not
    assert "account.deleted" in actions


async def test_sole_owner_with_members_must_hand_over_first(
    app: FastAPI, http: httpx2.AsyncClient, outbox: list[Mail], services: AppServices
) -> None:
    await signed_in(http, outbox, ALICE)
    slug = await create_org(http, "Alpha")
    async with join(app, outbox, http, slug, BOB):
        pass
    refused = await post_form(http, "/hesap/sil", {"password": PASSWORD}, csrf_from="/hesap")
    assert refused.status_code == 400
    assert "devret" in refused.text

    async with services.db.system("test change") as s:
        await s.execute(update(Membership).values(role="owner"))
    done = await post_form(http, "/hesap/sil", {"password": PASSWORD}, csrf_from="/hesap")
    assert done.status_code == 200
    async with services.db.system("test check") as s:
        org = (await s.scalars(select(Organization).where(Organization.slug == slug))).one()
        assert org.deleted_at is None  # Bob still owns it
        assert await s.scalar(select(func.count()).select_from(Membership)) == 1


async def test_delete_account_with_two_factor_needs_a_code(
    http: httpx2.AsyncClient,
    outbox: list[Mail],
    services: AppServices,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await signed_in(http, outbox, ALICE)
    page = await post_form(http, "/hesap/2fa/baslat", {}, csrf_from="/hesap")
    secret = page.text.split('<code class="secret">')[1].split("<")[0]
    t0 = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)
    monkeypatch.setattr(services.two_factor, "_clock", lambda: t0)
    await post_form(http, "/hesap/2fa/onayla", {"code": code_at(secret, t0)}, csrf_from="/hesap")

    later = t0 + timedelta(seconds=30)
    monkeypatch.setattr(services.privacy, "_clock", lambda: later)
    no_code = await post_form(http, "/hesap/sil", {"password": PASSWORD}, csrf_from="/hesap")
    assert no_code.status_code == 400
    assert "Doğrulama kodu hatalı" in no_code.text
    done = await post_form(
        http,
        "/hesap/sil",
        {"password": PASSWORD, "code": code_at(secret, later)},
        csrf_from="/hesap",
    )
    assert done.status_code == 200


async def test_close_organization(
    app: FastAPI, http: httpx2.AsyncClient, outbox: list[Mail], services: AppServices
) -> None:
    await signed_in(http, outbox, ALICE)
    slug = await create_org(http, "Alpha")
    token = await new_token(http, slug)
    settings = f"/o/{slug}/ayarlar"
    async with join(app, outbox, http, slug, BOB, role="admin") as bob:
        forbidden = await post_form(
            bob, f"/o/{slug}/sil", {"confirm": slug, "password": PASSWORD}, csrf_from=settings
        )
        assert forbidden.status_code == 403

        mismatch = await post_form(
            http, f"/o/{slug}/sil", {"confirm": "baska", "password": PASSWORD}, csrf_from=settings
        )
        assert mismatch.status_code == 400
        assert "aynen yazmalısın" in mismatch.text
        wrong = await post_form(
            http,
            f"/o/{slug}/sil",
            {"confirm": slug, "password": "yanlis-parola-0000"},
            csrf_from=settings,
        )
        assert wrong.status_code == 400

        closed = await post_form(
            http, f"/o/{slug}/sil", {"confirm": slug, "password": PASSWORD}, csrf_from=settings
        )
        assert closed.headers["location"] == "/orglar"
        assert (await bob.get(f"/o/{slug}")).status_code == 404
        assert "Alpha" not in (await http.get("/orglar")).text
    async with browser(app) as api:
        assert (
            await api.get("/api/v1/org", headers={"Authorization": f"Bearer {token}"})
        ).status_code == 401


async def test_retention_jobs(
    app: FastAPI, http: httpx2.AsyncClient, outbox: list[Mail], services: AppServices
) -> None:
    await signed_in(http, outbox, ALICE)
    slug = await create_org(http, "Alpha")
    async with browser(app) as fresh:
        await signup(fresh, "recent-unverified@example.invalid")
        await signup(fresh, "old-unverified@example.invalid")
    now = utcnow()
    async with services.db.system("test setup") as s:
        await s.execute(
            update(User)
            .where(User.email == "old-unverified@example.invalid")
            .values(created_at=now - timedelta(hours=49))
        )
        await s.execute(
            update(Organization)
            .where(Organization.slug == slug)
            .values(deleted_at=now - timedelta(days=31), purge_after=now - timedelta(days=1))
        )
        s.add(
            AuditEvent(
                org_id=None, action="ancient.platform", occurred_at=now - timedelta(days=400)
            )
        )
        await s.execute(update(OneTimeToken).values(expires_at=now - timedelta(days=2)))
        await s.execute(update(UserSession).values(expires_at=now - timedelta(days=2)))

    report = await run_retention(services.db)
    assert report.unverified_users == 1
    assert report.purged_orgs == 1
    assert report.platform_audit_events == 1
    assert report.sessions >= 1
    assert report.one_time_tokens >= 1

    async with services.db.system("test check") as s:
        emails = set((await s.scalars(select(User.email))).all())
        assert emails == {ALICE, "recent-unverified@example.invalid"}
        assert await s.scalar(select(func.count()).select_from(Organization)) == 0
        assert await s.scalar(select(func.count()).select_from(Membership)) == 0  # cascaded
        assert "ancient.platform" not in set((await s.scalars(select(AuditEvent.action))).all())

    again = await run_retention(services.db)
    assert again.unverified_users == again.purged_orgs == 0  # idempotent
