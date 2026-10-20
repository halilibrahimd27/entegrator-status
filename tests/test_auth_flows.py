"""Sign-up, verification, sign-in, lockout, throttling, password reset, sessions."""

from __future__ import annotations

import secrets
from datetime import timedelta

import httpx2
import pytest
from fastapi import FastAPI
from sqlalchemy import select, update

from sentinel.accounts.mail import Mail
from sentinel.db.base import utcnow
from sentinel.models import AuditEvent, OneTimeToken, User, UserSession
from sentinel.web.services import AppServices
from support import (
    PASSWORD,
    link_in,
    login,
    logout,
    post_form,
    registered,
    signed_in,
    signup,
    verify,
)

EMAIL = "kisi@example.invalid"
NEW_PASSWORD = f"new-{secrets.token_urlsafe(12)}"


async def test_sign_up_verify_and_sign_in(
    http: httpx2.AsyncClient, outbox: list[Mail], services: AppServices
) -> None:
    response = await signup(http, " Kisi@Example.Invalid ")
    assert response.status_code == 200
    assert len(outbox) == 1
    assert outbox[0].to == EMAIL
    assert "/dogrula/" in outbox[0].text

    unverified = await login(http, EMAIL)
    assert unverified.status_code == 403
    assert "doğrulaman gerekiyor" in unverified.text

    page = await http.get(link_in(outbox[0], "dogrula"))
    assert page.headers["referrer-policy"] == "no-referrer"
    assert (await verify(http, outbox)).status_code == 200

    signed = await login(http, EMAIL)
    assert signed.status_code == 303
    assert signed.headers["location"] == "/hesap"
    account = await http.get("/hesap")
    assert account.status_code == 200
    assert EMAIL in account.text

    async with services.db.system("test check") as s:
        actions = set((await s.scalars(select(AuditEvent.action))).all())
    assert {"account.signup", "account.email_verified", "account.login"} <= actions


async def test_verification_link_works_once(http: httpx2.AsyncClient, outbox: list[Mail]) -> None:
    await signup(http, EMAIL)
    assert (await verify(http, outbox)).status_code == 200
    again = await verify(http, outbox)
    assert again.status_code == 400
    assert "geçersiz" in again.text


async def test_sign_up_does_not_reveal_existing_accounts(
    http: httpx2.AsyncClient, outbox: list[Mail]
) -> None:
    first = await signup(http, EMAIL)
    second = await signup(http, EMAIL, name="Başka Biri")
    assert first.status_code == second.status_code == 200
    assert first.text.count("E-postanı kontrol et") == second.text.count("E-postanı kontrol et")
    assert "zaten kayıtlı" in outbox[1].text  # the owner is told, the visitor is not


@pytest.mark.parametrize(
    ("data", "message"),
    [
        ({"privacy_ack": ""}, "aydınlatma metnini okuduğunu"),
        ({"password": "kısa"}, "en az 12 karakter"),
        ({"password": "aaaaaaaaaaaaaaaa"}, "çok basit"),
        ({"password": "kisi-parolasi-2026"}, "e-posta adresini içermemeli"),
        ({"email": "not-an-email"}, "Geçerli bir e-posta"),
        ({"display_name": "   "}, "Adını gir"),
    ],
)
async def test_sign_up_validation(
    http: httpx2.AsyncClient, outbox: list[Mail], data: dict[str, str], message: str
) -> None:
    form = {"email": EMAIL, "display_name": "Kişi", "password": PASSWORD, "privacy_ack": "true"}
    form.update(data)
    if not form["privacy_ack"]:
        del form["privacy_ack"]
    response = await post_form(http, "/kayit", form)
    assert response.status_code == 400
    assert message in response.text
    assert outbox == []


async def test_wrong_password_and_unknown_account_look_the_same(
    http: httpx2.AsyncClient, outbox: list[Mail]
) -> None:
    await registered(http, outbox, EMAIL)
    wrong = await login(http, EMAIL, password="yanlis-parola-0000")
    unknown = await login(http, "yok@example.invalid")
    assert wrong.status_code == unknown.status_code == 401
    assert "E-posta veya parola hatalı." in wrong.text
    assert "E-posta veya parola hatalı." in unknown.text


async def test_account_locks_after_five_failures(
    http: httpx2.AsyncClient, outbox: list[Mail], services: AppServices
) -> None:
    await registered(http, outbox, EMAIL)
    for _ in range(5):
        assert (await login(http, EMAIL, password="yanlis-parola-0000")).status_code == 401
    assert (await login(http, EMAIL)).status_code == 401  # right password, but locked

    async with services.db.session() as s:
        await s.execute(update(User).values(locked_until=utcnow() - timedelta(seconds=1)))
    assert (await login(http, EMAIL)).status_code == 303


async def test_sign_in_is_throttled_per_account(
    http: httpx2.AsyncClient, outbox: list[Mail]
) -> None:
    await registered(http, outbox, EMAIL)
    statuses = [
        (await login(http, EMAIL, password="yanlis-parola-0000")).status_code for _ in range(11)
    ]
    assert statuses[:10] == [401] * 10
    assert statuses[10] == 429


async def test_sign_out_ends_the_session(
    http: httpx2.AsyncClient, outbox: list[Mail], services: AppServices
) -> None:
    await signed_in(http, outbox, EMAIL)
    response = await logout(http)
    assert response.status_code == 303
    after = await http.get("/hesap")
    assert after.status_code == 303
    assert after.headers["location"] == "/giris"
    async with services.db.session() as s:
        assert (await s.scalars(select(UserSession))).all() == []


async def test_idle_session_expires(
    http: httpx2.AsyncClient, outbox: list[Mail], services: AppServices
) -> None:
    await signed_in(http, outbox, EMAIL)
    async with services.db.session() as s:
        await s.execute(update(UserSession).values(last_seen_at=utcnow() - timedelta(hours=3)))
    assert (await http.get("/hesap")).headers["location"] == "/giris"


async def test_password_reset_flow(
    http: httpx2.AsyncClient, outbox: list[Mail], app: FastAPI, services: AppServices
) -> None:
    await signed_in(http, outbox, EMAIL)
    other = httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url=str(http.base_url))
    async with other:
        sent = await post_form(other, "/sifre-sifirla", {"email": EMAIL})
        assert sent.status_code == 200
        reset_path = link_in(outbox[-1], "sifre-sifirla")

        form = await other.get(reset_path)
        assert form.status_code == 200
        assert form.headers["referrer-policy"] == "no-referrer"

        weak = await post_form(other, reset_path, {"password": "kısa"})
        assert weak.status_code == 400  # a weak attempt does not spend the token
        done = await post_form(other, reset_path, {"password": NEW_PASSWORD})
        assert done.status_code == 200
        reused = await other.get(reset_path)
        assert reused.status_code == 400

    # The session opened before the reset is gone; the new password works.
    assert (await http.get("/hesap")).headers["location"] == "/giris"
    assert (await login(http, EMAIL)).status_code == 401
    assert (await login(http, EMAIL, password=NEW_PASSWORD)).status_code == 303


async def test_reset_for_unknown_address_sends_nothing(
    http: httpx2.AsyncClient, outbox: list[Mail]
) -> None:
    response = await post_form(http, "/sifre-sifirla", {"email": "yok@example.invalid"})
    assert response.status_code == 200
    assert "E-postanı kontrol et" in response.text
    assert outbox == []


async def test_expired_reset_link_is_refused(
    http: httpx2.AsyncClient, outbox: list[Mail], services: AppServices
) -> None:
    await registered(http, outbox, EMAIL)
    await post_form(http, "/sifre-sifirla", {"email": EMAIL})
    async with services.db.session() as s:
        await s.execute(update(OneTimeToken).values(expires_at=utcnow() - timedelta(seconds=1)))
    assert (await http.get(link_in(outbox[-1], "sifre-sifirla"))).status_code == 400


async def test_resend_verification(http: httpx2.AsyncClient, outbox: list[Mail]) -> None:
    await signup(http, EMAIL)
    response = await post_form(http, "/dogrula-tekrar", {"email": EMAIL})
    assert response.status_code == 200
    assert len(outbox) == 2
    assert (await verify(http, outbox)).status_code == 200
    # The first link was replaced by the second one.
    first = link_in(outbox[0], "dogrula")
    assert (await post_form(http, first, {})).status_code == 400


async def test_sign_up_can_be_disabled(
    http: httpx2.AsyncClient,
    outbox: list[Mail],
    monkeypatch: pytest.MonkeyPatch,
    services: AppServices,
) -> None:
    monkeypatch.setattr(
        services.accounts,
        "_settings",
        services.settings.model_copy(update={"signup_enabled": False}),
    )
    response = await signup(http, EMAIL)
    assert response.status_code == 400
    assert "yeni kayıt alınmıyor" in response.text
