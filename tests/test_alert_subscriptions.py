"""Opting in and out: e-mail double opt-in, Telegram linking and the bot's commands."""

from __future__ import annotations

import ipaddress
import json
import re
import uuid
from collections.abc import Sequence
from datetime import timedelta
from typing import Any

import httpx
from redis.asyncio import Redis
from sqlalchemy import select

from probe_support import two_orgs
from sentinel.accounts.mail import Mail, MemoryMailer
from sentinel.alerts.channels import Senders
from sentinel.alerts.subscriptions import PENDING, SubscriptionService
from sentinel.alerts.telegram import OFFSET_KEY, TelegramBot, parse_command
from sentinel.db.base import utcnow
from sentinel.models import AlertChannel, AlertRecipient
from sentinel.security.addresses import IPAddress
from sentinel.security.http import OutboundClient
from sentinel.security.ratelimit import HostLimiter
from sentinel.web.services import AppServices

LINK = re.compile(r"https://status\.example\.invalid(/abone/onay/\S+)")


async def channel(services: AppServices, kind: str) -> tuple[uuid.UUID, uuid.UUID]:
    org, _ = await two_orgs(services)
    async with services.db.system("test setup") as s:
        row = AlertChannel(org_id=org, name="Operasyon", kind=kind)
        s.add(row)
        await s.flush()
        return org, row.id


async def external(services: AppServices, org: uuid.UUID, channel_id: uuid.UUID) -> uuid.UUID:
    async with services.db.system("test setup") as s:
        row = AlertRecipient(org_id=org, channel_id=channel_id, address="dis@ortak.example.invalid")
        s.add(row)
        await s.flush()
        return row.id


async def test_double_opt_in(services: AppServices, outbox: list[Mail], http: Any) -> None:
    org, email = await channel(services, "email")
    recipient = await external(services, org, email)
    await services.subscriptions.send_confirmation(recipient)
    (mail,) = outbox
    assert mail.to == "dis@ortak.example.invalid"
    assert '"A" organizasyonu' in mail.text
    found = LINK.search(mail.text)
    assert found is not None
    path = found.group(1)

    page = await http.get(path)
    assert page.status_code == 200
    assert "Aboneliği onayla" in page.text
    assert page.headers["cache-control"] == "no-store"
    async with services.db.system("test check") as s:
        row = await s.get(AlertRecipient, recipient)
        assert row is not None
        assert row.confirmed_at is None  # opening the link is not consent

    done = await http.post(path)  # no session, no form token: the token is the secret
    assert done.status_code == 200
    assert "Onaylandı" in done.text
    async with services.db.system("test check") as s:
        row = await s.get(AlertRecipient, recipient)
        assert row is not None
        assert row.confirmed_at is not None
        assert row.confirm_hash is None
    assert (await http.post(path)).status_code == 404  # one-time
    assert (await http.get("/abone/onay/" + "x" * 43)).status_code == 404


async def test_confirmation_expires_after_48_hours(services: AppServices) -> None:
    org, email = await channel(services, "email")
    recipient = await external(services, org, email)
    mailer = MemoryMailer()
    later = SubscriptionService(
        services.db, services.settings, mailer, clock=lambda: utcnow() - timedelta(hours=49)
    )
    await later.send_confirmation(recipient)  # sent 49 hours ago
    found = LINK.search(mailer.outbox[0].text)
    assert found is not None
    token = found.group(1).rsplit("/", 1)[1]
    assert await services.subscriptions.preview_confirmation(token) is None
    assert await services.subscriptions.confirm(token) is None


async def test_confirmation_is_only_for_email_channels(
    services: AppServices, outbox: list[Mail]
) -> None:
    org, telegram = await channel(services, "telegram")
    await services.subscriptions.send_confirmation(await external(services, org, telegram))
    await services.subscriptions.send_confirmation(uuid.uuid4())
    assert outbox == []


async def test_unknown_unsubscribe_link(http: Any) -> None:
    token = uuid.uuid4().hex + "." + "0" * 32
    assert (await http.get(f"/abone/iptal/{token}")).status_code == 404
    assert (await http.get("/abone/iptal/../../etc")).status_code == 404


def bot_service(services: AppServices) -> SubscriptionService:
    settings = services.settings.model_copy(update={"telegram_bot_username": "EntegratorTestBot"})
    return SubscriptionService(services.db, settings, MemoryMailer())


async def test_telegram_linking(services: AppServices) -> None:
    subscriptions = bot_service(services)
    org, telegram = await channel(services, "telegram")
    _, other_org = await two_orgs_again(services)
    assert await subscriptions.telegram_link(other_org, telegram) is None  # not its channel
    link = await subscriptions.telegram_link(org, telegram)
    assert link is not None
    assert link.startswith("https://t.me/EntegratorTestBot?start=")
    token = link.rsplit("=", 1)[1]
    assert len(token) <= 64
    assert re.fullmatch(r"[A-Za-z0-9_-]+", token)  # Telegram's limits

    linked = await subscriptions.link_telegram(token, "4242")
    assert linked is not None
    assert (linked.org, linked.channel) == ("A", "Operasyon")
    assert await subscriptions.link_telegram(token, "4243") is None  # one-time
    assert [s.channel for s in await subscriptions.telegram_subscriptions("4242")] == ["Operasyon"]

    again = await subscriptions.telegram_link(org, telegram)  # the same chat links again
    assert again is not None
    assert await subscriptions.link_telegram(again.rsplit("=", 1)[1], "4242") is not None
    async with services.db.system("test check") as s:
        rows = (await s.scalars(select(AlertRecipient))).all()
    assert [r.address for r in rows] == ["4242"]  # no duplicate, no pending row left

    assert await subscriptions.telegram_stop("4242") == 1
    assert await subscriptions.telegram_subscriptions("4242") == []
    assert await subscriptions.telegram_stop("4242") == 0


async def two_orgs_again(services: AppServices) -> tuple[uuid.UUID, uuid.UUID]:
    from sentinel.models import Organization

    async with services.db.system("test setup") as s:
        c, d = Organization(slug="c", name="C"), Organization(slug="d", name="D")
        s.add_all([c, d])
        await s.flush()
        return c.id, d.id


async def test_telegram_link_expires_in_an_hour(services: AppServices) -> None:
    subscriptions = bot_service(services)
    org, telegram = await channel(services, "telegram")
    link = await subscriptions.telegram_link(org, telegram)
    assert link is not None
    async with services.db.system("test age") as s:
        row = await s.scalar(select(AlertRecipient))
        assert row is not None
        assert row.address.startswith(PENDING)
        row.created_at = utcnow() - timedelta(minutes=61)
    assert await subscriptions.link_telegram(link.rsplit("=", 1)[1], "4242") is None


async def test_no_telegram_links_without_a_bot(services: AppServices) -> None:
    org, telegram = await channel(services, "telegram")
    assert await services.subscriptions.telegram_link(org, telegram) is None


def test_commands() -> None:
    assert parse_command("/start@EntegratorTestBot abc") == ("/start", "abc")
    assert parse_command("  /IPTAL ") == ("/iptal", "")
    assert parse_command("merhaba") == ("merhaba", "")


async def anywhere(host: str, port: int) -> Sequence[IPAddress]:
    return [ipaddress.ip_address("1.1.1.1")]


async def test_bot_polls_and_answers(services: AppServices, redis_client: Redis) -> None:
    subscriptions = bot_service(services)
    org, telegram = await channel(services, "telegram")
    link = await subscriptions.telegram_link(org, telegram)
    assert link is not None
    token = link.rsplit("=", 1)[1]
    updates = [
        {"update_id": 10, "message": {"chat": {"id": 4242}, "text": f"/start {token}"}},
        {"update_id": 11, "message": {"chat": {"id": 4242}, "text": "/abonelikler"}},
        {"update_id": 12, "message": {"chat": {"id": 7}, "text": "/start yanlis"}},
        {"update_id": 13, "message": {"chat": {"id": 7}, "text": "selam"}},
        {"update_id": 14, "edited_message": {}},
        {"update_id": "broken"},
    ]
    seen: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path.endswith("/getUpdates"):
            return httpx.Response(200, json={"ok": True, "result": updates})
        return httpx.Response(200, json={"ok": True})

    client = OutboundClient(
        user_agent="EntegratorStatus/test",
        resolver=anywhere,
        transport=httpx.MockTransport(handle),
        limiter=HostLimiter(rate_per_s=1000, burst=100),
    )
    async with client:
        senders = Senders(client, MemoryMailer(), telegram_token="123:abc")
        bot = TelegramBot(client, senders, subscriptions, redis_client, token="123:abc")
        report = await bot.poll()
    assert (report.updates, report.replies) == (5, 4)
    assert seen[0].url.params["offset"] == "0"
    assert int(await redis_client.get(OFFSET_KEY) or 0) == 15
    replies = [json.loads(r.content) for r in seen[1:]]
    assert [r["chat_id"] for r in replies] == ["4242", "4242", "7", "7"]
    assert replies[0]["text"].startswith('Bağlandı: "A" organizasyonunun "Operasyon"')
    assert "- A / Operasyon" in replies[1]["text"]
    assert "geçersiz" in replies[2]["text"]
    assert "/abonelikler" in replies[3]["text"]  # help
    assert all(r.method == "POST" for r in seen[1:])


async def test_bot_survives_a_bad_poll(services: AppServices, redis_client: Redis) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"not json")

    client = OutboundClient(
        user_agent="EntegratorStatus/test",
        resolver=anywhere,
        transport=httpx.MockTransport(handle),
        limiter=HostLimiter(rate_per_s=1000, burst=100),
    )
    async with client:
        senders = Senders(client, MemoryMailer(), telegram_token="123:abc")
        bot = TelegramBot(client, senders, bot_service(services), redis_client, token="123:abc")
        assert (await bot.poll()).updates == 0
    assert await redis_client.get(OFFSET_KEY) is None
