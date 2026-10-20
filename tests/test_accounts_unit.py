"""Unit tests for the account building blocks (no HTTP)."""

from __future__ import annotations

import os
import smtplib
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from argon2 import PasswordHasher
from redis.asyncio import Redis

from sentinel.accounts import totp
from sentinel.accounts.mail import Mail, MemoryMailer, SmtpMailer, build_mailer
from sentinel.accounts.passwords import PasswordPolicyError, Passwords, check_policy, make_hasher
from sentinel.accounts.service import AccountError, normalize_email
from sentinel.accounts.throttle import Limit, Throttle
from sentinel.accounts.tokens import digest, equal, new_token
from sentinel.audit import ip_prefix
from sentinel.config import load_settings

T0 = datetime(2026, 10, 20, 12, 0, 0, tzinfo=UTC)


# --- passwords -------------------------------------------------------------


@pytest.mark.parametrize(
    ("password", "key"),
    [
        ("short", "password.too_short"),
        ("x" * 129, "password.too_long"),
        ("abababababababab", "password.too_simple"),
        ("my-ayse.yilmaz-pass", "password.contains_email"),
    ],
)
def test_password_policy(password: str, key: str) -> None:
    with pytest.raises(PasswordPolicyError) as caught:
        check_policy(password, email="ayse.yilmaz@example.invalid")
    assert caught.value.key == key


def test_password_policy_accepts_a_good_password() -> None:
    check_policy("uzun ve farklı bir cümle 42", email="ayse@example.invalid")


def test_verify_and_rehash() -> None:
    weak = Passwords(PasswordHasher(time_cost=1, memory_cost=1024, parallelism=1))
    stored = weak.hash("dogru-parola-12345")
    assert weak.verify(stored, "dogru-parola-12345") == (True, False)
    assert weak.verify(stored, "yanlis-parola-1234") == (False, False)
    assert weak.verify(None, "anything-at-all-1") == (False, False)  # unknown account
    assert weak.verify("not-a-hash", "anything-at-all-1") == (False, False)
    stronger = Passwords(PasswordHasher(time_cost=2, memory_cost=2048, parallelism=1))
    assert stronger.verify(stored, "dogru-parola-12345") == (True, True)


def test_production_hasher_uses_argon2id_defaults() -> None:
    assert make_hasher("prod").type.name == "ID"
    assert make_hasher("prod").memory_cost >= 64 * 1024


# --- tokens, e-mail addresses, IP prefixes ---------------------------------------


def test_digests_are_separated_by_purpose_and_key() -> None:
    token = new_token()
    assert digest("k" * 32, token, purpose="a") != digest("k" * 32, token, purpose="b")
    assert digest("k" * 32, token, purpose="a") != digest("j" * 32, token, purpose="a")
    assert len(digest("k" * 32, token, purpose="a")) == 32
    assert equal(token, token)
    assert not equal(token, new_token())


@pytest.mark.parametrize(
    ("raw", "expected"), [(" Ayse@Example.Invalid ", "ayse@example.invalid"), ("a@b.co", "a@b.co")]
)
def test_email_normalisation(raw: str, expected: str) -> None:
    assert normalize_email(raw) == expected


@pytest.mark.parametrize("raw", ["", "no-at-sign", "a@b", "<a>@b.co", "a@" + "b" * 300 + ".co"])
def test_invalid_email(raw: str) -> None:
    with pytest.raises(AccountError):
        normalize_email(raw)


@pytest.mark.parametrize(
    ("ip", "expected"),
    [
        ("203.0.113.77", "203.0.113.0/24"),
        ("2001:db8:abcd:12::1", "2001:db8:abcd::/48"),
        ("", None),
        (None, None),
        ("not-an-ip", None),
    ],
)
def test_ip_prefix(ip: str | None, expected: str | None) -> None:
    assert ip_prefix(ip) == expected


# --- TOTP ----------------------------------------------------------------------


def test_totp_window_and_replay() -> None:
    secret = totp.new_secret()
    code = totp.code_at(secret, T0)
    step = totp.accept(secret, code, last_step=None, now=T0)
    assert step is not None
    assert totp.accept(secret, code, last_step=step, now=T0) is None  # replay
    drift = totp.code_at(secret, T0 - timedelta(seconds=30))
    assert totp.accept(secret, drift, last_step=None, now=T0) is not None  # one step of drift
    stale = totp.code_at(secret, T0 - timedelta(seconds=90))
    assert totp.accept(secret, stale, last_step=None, now=T0) is None


@pytest.mark.parametrize("code", ["", "12345", "1234567", "abcdef", "12 34 5x"])
def test_totp_rejects_malformed_codes(code: str) -> None:
    assert totp.accept(totp.new_secret(), code, last_step=None, now=T0) is None


def test_totp_code_with_spaces_is_accepted() -> None:
    secret = totp.new_secret()
    code = totp.code_at(secret, T0)
    assert totp.accept(secret, f"{code[:3]} {code[3:]}", last_step=None, now=T0) is not None


def test_sealed_secret_is_bound_to_its_user() -> None:
    box = totp.TotpBox("k" * 40)
    alice, bob = uuid.uuid4(), uuid.uuid4()
    sealed = box.seal("JBSWY3DPEHPK3PXP", alice)
    assert box.open(sealed, alice) == "JBSWY3DPEHPK3PXP"
    with pytest.raises(totp.TotpSecretError):
        box.open(sealed, bob)
    with pytest.raises(totp.TotpSecretError):
        totp.TotpBox("j" * 40).open(sealed, alice)
    with pytest.raises(totp.TotpSecretError):
        box.open(b"\x02" + sealed[1:], alice)


def test_provisioning_uri_and_qr() -> None:
    uri = totp.provisioning_uri("JBSWY3DPEHPK3PXP", "ayse@example.invalid")
    assert uri.startswith("otpauth://totp/")
    assert "issuer=Entegrat%C3%B6r%20Status" in uri
    svg = totp.qr_svg(uri)
    assert svg.startswith("<svg")
    assert "<script" not in svg
    assert "style=" not in svg


# --- mail --------------------------------------------------------------------------


class FakeSMTP:
    instances: list[FakeSMTP] = []  # noqa: RUF012

    def __init__(self, host: str, port: int, **kwargs: Any) -> None:
        self.host, self.port, self.kwargs = host, port, kwargs
        self.calls: list[str] = []
        self.sent: list[Any] = []
        FakeSMTP.instances.append(self)

    def __enter__(self) -> FakeSMTP:
        return self

    def __exit__(self, *exc: object) -> None:
        self.calls.append("quit")

    def starttls(self, context: Any = None) -> None:
        self.calls.append("starttls")

    def login(self, user: str, password: str) -> None:
        self.calls.append(f"login:{user}")

    def send_message(self, message: Any) -> None:
        self.sent.append(message)


class FakeSMTPSSL(FakeSMTP):
    pass


@pytest.mark.parametrize(
    ("security", "username", "expected_class", "expected_calls"),
    [
        ("starttls", "bot", FakeSMTP, ["starttls", "login:bot", "quit"]),
        ("ssl", "bot", FakeSMTPSSL, ["login:bot", "quit"]),
        ("none", "", FakeSMTP, ["quit"]),
    ],
)
async def test_smtp_mailer(
    monkeypatch: pytest.MonkeyPatch,
    valid_env: dict[str, str],
    security: str,
    username: str,
    expected_class: type,
    expected_calls: list[str],
) -> None:
    FakeSMTP.instances = []
    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(smtplib, "SMTP_SSL", FakeSMTPSSL)
    for key, value in {
        "MAIL_BACKEND": "smtp",
        "SMTP_HOST": "smtp.example.invalid",
        "MAIL_FROM": "bildirim@example.invalid",
        "SMTP_SECURITY": security,
        "SMTP_USERNAME": username,
        "SMTP_PASSWORD": "smtp-secret",
    }.items():
        monkeypatch.setenv(key, value)
    mailer = build_mailer(load_settings())
    assert isinstance(mailer, SmtpMailer)
    await mailer.send(Mail(to="ayse@example.invalid", subject="Konu", text="Gövde"))
    (smtp,) = FakeSMTP.instances
    assert type(smtp) is expected_class
    assert smtp.calls == expected_calls
    message = smtp.sent[0]
    assert message["From"] == "Entegratör Status <bildirim@example.invalid>"
    assert message["To"] == "ayse@example.invalid"
    assert message.get_content().strip() == "Gövde"


async def test_memory_mailer_keeps_messages() -> None:
    mailer = MemoryMailer()
    await mailer.send(Mail(to="a@example.invalid", subject="s", text="t"))
    assert [m.to for m in mailer.outbox] == ["a@example.invalid"]


# --- throttle ---------------------------------------------------------------------------


@pytest.fixture
async def redis() -> AsyncIterator[Redis]:
    url = os.environ.get("TEST_REDIS_URL")
    if not url:
        pytest.fail("TEST_REDIS_URL is not set; run tests with `make test`")
    client = Redis.from_url(url)
    await client.flushdb()
    try:
        yield client
    finally:
        await client.flushdb()
        await client.aclose()


async def test_throttle_window(redis: Redis) -> None:
    throttle = Throttle(redis)
    limit = Limit("test", 3, 60)
    assert [await throttle.allow(limit, "ayse@example.invalid") for _ in range(4)] == [
        True,
        True,
        True,
        False,
    ]
    assert await throttle.allow(limit, "baska@example.invalid")  # separate key
    keys = [k.decode() if isinstance(k, bytes) else k for k in await redis.keys("rl:*")]
    assert keys
    assert all("example" not in k for k in keys)  # stored hashed
    assert 0 < await redis.ttl(keys[0]) <= 60
