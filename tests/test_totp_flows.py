"""TOTP enrolment, the second factor at sign-in, replay protection, disabling."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

import httpx2
import pytest
from sqlalchemy import update

from sentinel.accounts.mail import Mail
from sentinel.accounts.totp import code_at
from sentinel.models import User
from sentinel.web.services import AppServices
from support import PASSWORD, login, logout, post_form, signed_in

EMAIL = "iki-adim@example.invalid"
T0 = datetime(2026, 10, 20, 12, 0, 0, tzinfo=UTC)
SECRET_RE = re.compile(r'<code class="secret">([A-Z2-7]+)</code>')


def at(monkeypatch: pytest.MonkeyPatch, services: AppServices, when: datetime) -> None:
    monkeypatch.setattr(services.two_factor, "_clock", lambda: when)


async def start_enrolment(http: httpx2.AsyncClient) -> str:
    page = await post_form(http, "/hesap/2fa/baslat", {}, csrf_from="/hesap")
    assert page.status_code == 200
    assert "<svg" in page.text
    match = SECRET_RE.search(page.text)
    assert match
    return match.group(1)


async def enable(
    http: httpx2.AsyncClient, services: AppServices, monkeypatch: pytest.MonkeyPatch
) -> str:
    secret = await start_enrolment(http)
    at(monkeypatch, services, T0)
    done = await post_form(
        http, "/hesap/2fa/onayla", {"code": code_at(secret, T0)}, csrf_from="/hesap"
    )
    assert done.status_code == 303
    assert done.headers["location"] == "/hesap?ok=totp_enabled"
    return secret


async def test_enrolment_needs_a_valid_code(
    http: httpx2.AsyncClient,
    outbox: list[Mail],
    services: AppServices,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await signed_in(http, outbox, EMAIL)
    secret = await start_enrolment(http)
    at(monkeypatch, services, T0)
    wrong = await post_form(http, "/hesap/2fa/onayla", {"code": "000000"}, csrf_from="/hesap")
    assert wrong.status_code == 400
    assert "Kod doğrulanamadı" in wrong.text
    assert secret in wrong.text  # the same enrolment is shown again
    page = await http.get("/hesap?ok=totp_enabled")
    assert "Kapalı" in page.text


async def test_second_factor_at_sign_in_with_replay_protection(
    http: httpx2.AsyncClient,
    outbox: list[Mail],
    services: AppServices,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await signed_in(http, outbox, EMAIL)
    secret = await enable(http, services, monkeypatch)
    assert "İki adımlı doğrulama açıldı." in (await http.get("/hesap?ok=totp_enabled")).text
    await logout(http)

    first = await login(http, EMAIL)
    assert first.headers["location"] == "/giris/2fa"
    gated = await http.get("/hesap")
    assert gated.headers["location"] == "/giris/2fa"

    replay = await post_form(http, "/giris/2fa", {"code": code_at(secret, T0)})
    assert replay.status_code == 401  # the enrolment code was already used

    later = T0 + timedelta(seconds=30)
    at(monkeypatch, services, later)
    ok = await post_form(http, "/giris/2fa", {"code": code_at(secret, later)})
    assert ok.status_code == 303
    assert ok.headers["location"] == "/orglar"
    assert (await http.get("/hesap")).status_code == 200


async def test_disabling_needs_password_and_a_fresh_code(
    http: httpx2.AsyncClient,
    outbox: list[Mail],
    services: AppServices,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await signed_in(http, outbox, EMAIL)
    secret = await enable(http, services, monkeypatch)
    later = T0 + timedelta(seconds=60)
    at(monkeypatch, services, later)

    wrong = await post_form(
        http,
        "/hesap/2fa/kapat",
        {"password": "yanlis-parola-0000", "code": code_at(secret, later)},
        csrf_from="/hesap",
    )
    assert wrong.status_code == 400
    done = await post_form(
        http,
        "/hesap/2fa/kapat",
        {"password": PASSWORD, "code": code_at(secret, later)},
        csrf_from="/hesap",
    )
    assert done.headers["location"] == "/hesap?ok=totp_disabled"
    assert "Kapalı" in (await http.get("/hesap")).text


async def test_platform_admins_must_keep_two_factor(
    http: httpx2.AsyncClient,
    outbox: list[Mail],
    services: AppServices,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await signed_in(http, outbox, EMAIL)
    async with services.db.session() as s:
        await s.execute(update(User).values(is_platform_admin=True))
    banner = await http.get("/hesap")
    assert "Platform yöneticileri için iki adımlı doğrulama zorunlu" in banner.text

    secret = await enable(http, services, monkeypatch)
    page = await http.get("/hesap")
    assert "/hesap/2fa/kapat" not in page.text
    later = T0 + timedelta(seconds=60)
    at(monkeypatch, services, later)
    refused = await post_form(
        http,
        "/hesap/2fa/kapat",
        {"password": PASSWORD, "code": code_at(secret, later)},
        csrf_from="/hesap",
    )
    assert refused.status_code == 400
    assert "kapatamaz" in refused.text


async def test_second_factor_attempts_are_throttled(
    http: httpx2.AsyncClient,
    outbox: list[Mail],
    services: AppServices,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await signed_in(http, outbox, EMAIL)
    await enable(http, services, monkeypatch)
    await logout(http)
    await login(http, EMAIL)
    statuses = [
        (await post_form(http, "/giris/2fa", {"code": "000000"})).status_code for _ in range(11)
    ]
    assert statuses[:10] == [401] * 10
    assert statuses[10] == 429
