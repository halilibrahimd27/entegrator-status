"""Sending alerts: signed webhooks, SSRF, retries, quiet hours, daily batches, digests."""

from __future__ import annotations

import ipaddress
import json
import re
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy import select

from probe_support import two_orgs
from sentinel.accounts.mail import MemoryMailer
from sentinel.alerts.channels import (
    SIGNATURE_HEADER,
    TIMESTAMP_HEADER,
    Senders,
    check_unsubscribe,
    render_digest,
    sign,
    unsubscribe_token,
)
from sentinel.alerts.delivery import (
    BACKOFF,
    DISABLE_AFTER,
    WEEKLY,
    DeliveryWorker,
    daily_slot,
    quiet_until,
    weekly_slot,
)
from sentinel.models import (
    AlertChannel,
    AlertDelivery,
    AlertEvent,
    AlertRecipient,
    DeliveryStatus,
    JobCursor,
    Product,
    Risk,
)
from sentinel.risk.digest import Digest, Outage
from sentinel.risk.impact import Impact
from sentinel.risk.register import CalendarItem, RegisterEntry
from sentinel.security.addresses import IPAddress
from sentinel.security.http import OutboundClient
from sentinel.security.ratelimit import HostLimiter
from sentinel.web.services import AppServices

# 2026-10-19 is a Monday; Turkey is UTC+3 all year
MONDAY_NOON = datetime(2026, 10, 19, 9, 0, tzinfo=UTC)


def resolver(answer: str) -> Any:
    async def resolve(host: str, port: int) -> Sequence[IPAddress]:
        return [ipaddress.ip_address(answer)]

    return resolve


class Hooks:
    """A fake receiver for every outbound POST; records requests and answers ``status``."""

    def __init__(self, status: int = 200, address: str = "1.1.1.1") -> None:
        self.status = status
        self.requests: list[httpx.Request] = []
        self.client = OutboundClient(
            user_agent="EntegratorStatus/test",
            resolver=resolver(address),
            transport=httpx.MockTransport(self.handle),
            limiter=HostLimiter(rate_per_s=1000, burst=100),
        )

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(self.status, json={"ok": True})


class Setup:
    def __init__(self, services: AppServices, hooks: Hooks | None = None) -> None:
        self.services = services
        self.hooks = hooks or Hooks()
        self.mailer = MemoryMailer()
        self.now = MONDAY_NOON

    def worker(self) -> DeliveryWorker:
        return DeliveryWorker(
            self.services.db,
            Senders(self.hooks.client, self.mailer, telegram_token="123:abc"),
            self.services.credentials,
            public_base_url="https://status.example.invalid",
            unsubscribe_key=self.services.subscriptions.unsubscribe_key,
            clock=lambda: self.now,
        )

    async def org(self) -> tuple[uuid.UUID, uuid.UUID]:
        org_a, _ = await two_orgs(self.services)
        async with self.services.db.system("test setup") as s:
            product = Product(org_id=org_a, slug="fatura", name="Fatura")
            s.add(product)
            await s.flush()
            return org_a, product.id

    async def channel(
        self,
        org: uuid.UUID,
        kind: str = "webhook",
        secret: dict[str, str] | None = None,
        **extra: Any,
    ) -> uuid.UUID:
        async with self.services.db.system("test setup") as s:
            channel = AlertChannel(org_id=org, name=f"c-{uuid.uuid4().hex[:6]}", kind=kind, **extra)
            s.add(channel)
            await s.flush()
            if secret is not None:
                channel.sealed = await self.services.credentials.seal_payload(
                    s, org, channel.id, secret
                )
            return channel.id

    async def recipient(self, org: uuid.UUID, channel: uuid.UUID, address: str) -> uuid.UUID:
        async with self.services.db.system("test setup") as s:
            row = AlertRecipient(
                org_id=org, channel_id=channel, address=address, confirmed_at=self.now
            )
            s.add(row)
            await s.flush()
            return row.id

    async def delivery(
        self,
        org: uuid.UUID,
        product: uuid.UUID,
        channel: uuid.UUID,
        *,
        severity: str = "medium",
        recipient: uuid.UUID | None = None,
    ) -> uuid.UUID:
        async with self.services.db.system("test setup") as s:
            event = AlertEvent(
                org_id=org,
                product_id=product,
                subject_type="incident",
                subject_id=uuid.uuid4(),
                phase="opened",
                severity=severity,
                reason="upstream_down",
                title="GİB — e-Fatura",
                path=["Fatura", "Entegratör X — API", "GİB — e-Fatura"],
                detail={},
                occurred_at=self.now,
            )
            s.add(event)
            await s.flush()
            row = AlertDelivery(
                org_id=org,
                event_id=event.id,
                channel_id=channel,
                recipient_key=str(recipient) if recipient else "",
                next_attempt_at=self.now,
                created_at=self.now,
            )
            s.add(row)
            await s.flush()
            return row.id

    async def get(self, model: Any, row_id: uuid.UUID) -> Any:
        async with self.services.db.system("test check") as s:
            return await s.get(model, row_id)


async def test_webhook_is_signed_over_the_exact_bytes(services: AppServices) -> None:
    setup = Setup(services)
    org, product = await setup.org()
    secret = {"url": "https://hooks.example/in", "secret": "s3cr3t-signing"}
    channel = await setup.channel(org, secret=secret)
    delivery = await setup.delivery(org, product, channel)
    report = await setup.worker().run()
    assert report.sent == 1
    (request,) = setup.hooks.requests
    timestamp = int(request.headers[TIMESTAMP_HEADER])
    assert request.headers[SIGNATURE_HEADER] == sign(secret["secret"], timestamp, request.content)
    assert request.headers[SIGNATURE_HEADER] != sign("wrong", timestamp, request.content)
    payload = json.loads(request.content)
    assert payload["severity"] == "medium"
    assert payload["path"][-1] == "GİB — e-Fatura"
    row = await setup.get(AlertDelivery, delivery)
    assert (row.status, row.attempts, row.sent_at) == (DeliveryStatus.SENT, 1, setup.now)
    assert (await setup.worker().run()).sent == 0  # sent once


async def test_private_address_is_refused_and_retried_later(services: AppServices) -> None:
    setup = Setup(services, Hooks(address="10.0.0.5"))
    org, product = await setup.org()
    channel = await setup.channel(org, secret={"url": "https://hooks.example/in", "secret": "x"})
    delivery = await setup.delivery(org, product, channel)
    report = await setup.worker().run()
    assert (report.sent, report.retried) == (0, 1)
    assert setup.hooks.requests == []  # never connected
    row = await setup.get(AlertDelivery, delivery)
    assert row.last_error == "blocked_address"  # a code, never the URL
    assert row.next_attempt_at == setup.now + BACKOFF[0]


async def test_retries_back_off_then_give_up(services: AppServices) -> None:
    setup = Setup(services, Hooks(status=500))
    org, product = await setup.org()
    channel = await setup.channel(org, secret={"url": "https://hooks.example/in", "secret": "x"})
    delivery = await setup.delivery(org, product, channel)
    for wait in BACKOFF:
        await setup.worker().run()
        row = await setup.get(AlertDelivery, delivery)
        assert (row.status, row.last_error) == (DeliveryStatus.QUEUED, "http_500")
        assert row.next_attempt_at == setup.now + wait
        assert (await setup.worker().run()).retried == 0  # not due yet
        setup.now += wait
    report = await setup.worker().run()
    assert report.failed == 1
    row = await setup.get(AlertDelivery, delivery)
    assert (row.status, row.attempts) == (DeliveryStatus.FAILED, len(BACKOFF) + 1)
    assert len(setup.hooks.requests) == len(BACKOFF) + 1


async def test_a_channel_failing_ten_times_in_a_row_is_disabled(services: AppServices) -> None:
    setup = Setup(services, Hooks(status=404))
    org, product = await setup.org()
    channel = await setup.channel(org, secret={"url": "https://hooks.example/in", "secret": "x"})
    for _ in range(DISABLE_AFTER + 2):
        await setup.delivery(org, product, channel)
    report = await setup.worker().run()
    assert (report.retried, report.skipped) == (DISABLE_AFTER, 2)
    row = await setup.get(AlertChannel, channel)
    assert (row.enabled, row.disabled_reason) == (False, "too_many_failures")


async def test_a_success_resets_the_failure_count(services: AppServices) -> None:
    setup = Setup(services)
    org, product = await setup.org()
    channel = await setup.channel(
        org, secret={"url": "https://hooks.example/in", "secret": "x"}, failures=7
    )
    await setup.delivery(org, product, channel)
    await setup.worker().run()
    assert (await setup.get(AlertChannel, channel)).failures == 0


async def test_slack_without_a_url_is_not_configured(services: AppServices) -> None:
    setup = Setup(services)
    org, product = await setup.org()
    channel = await setup.channel(org, kind="slack")
    delivery = await setup.delivery(org, product, channel)
    await setup.worker().run()
    assert (await setup.get(AlertDelivery, delivery)).last_error == "not_configured"


async def test_slack_message(services: AppServices) -> None:
    setup = Setup(services)
    org, product = await setup.org()
    channel = await setup.channel(org, kind="slack", secret={"url": "https://hooks.example/s"})
    await setup.delivery(org, product, channel, severity="high")
    await setup.worker().run()
    (request,) = setup.hooks.requests
    text = json.loads(request.content)["text"]
    assert text.startswith("*[Yüksek] Bağımlılıkta erişim sorunu: GİB — e-Fatura*")
    assert "Fatura → Entegratör X — API → GİB — e-Fatura" in text
    assert f"/o/a/urunler/{product}" in text
    assert SIGNATURE_HEADER not in request.headers


async def test_quiet_hours_hold_all_but_high(services: AppServices) -> None:
    setup = Setup(services)
    setup.now = datetime(2026, 10, 20, 20, 0, tzinfo=UTC)  # 23:00 in Turkey
    org, product = await setup.org()
    channel = await setup.channel(
        org,
        secret={"url": "https://hooks.example/in", "secret": "x"},
        quiet_start="22:00",
        quiet_end="08:00",
    )
    medium = await setup.delivery(org, product, channel)
    await setup.delivery(org, product, channel, severity="high")
    report = await setup.worker().run()
    assert (report.sent, report.deferred) == (1, 1)
    row = await setup.get(AlertDelivery, medium)
    assert row.next_attempt_at == datetime(2026, 10, 21, 5, 0, tzinfo=UTC)  # 08:00 in Turkey
    assert row.status == DeliveryStatus.QUEUED


async def test_daily_mode_sends_one_message_at_nine(services: AppServices) -> None:
    setup = Setup(services)
    setup.now = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)  # 15:00 in Turkey
    org, product = await setup.org()
    channel = await setup.channel(
        org, secret={"url": "https://hooks.example/in", "secret": "x"}, daily=True
    )
    first = await setup.delivery(org, product, channel)
    await setup.delivery(org, product, channel, severity="high")
    assert (await setup.worker().run()).deferred == 2
    assert (await setup.get(AlertDelivery, first)).next_attempt_at == datetime(
        2026, 10, 21, 6, 0, tzinfo=UTC
    )
    setup.now = datetime(2026, 10, 21, 6, 0, tzinfo=UTC)
    report = await setup.worker().run()
    assert report.sent == 2
    (request,) = setup.hooks.requests  # one message for both
    assert len(json.loads(request.content)["events"]) == 2


async def test_email_carries_a_one_click_unsubscribe(services: AppServices, http: Any) -> None:
    setup = Setup(services)
    org, product = await setup.org()
    channel = await setup.channel(org, kind="email")
    recipient = await setup.recipient(org, channel, "ops@a.example.invalid")
    await setup.delivery(org, product, channel, recipient=recipient)
    assert (await setup.worker().run()).sent == 1
    (mail,) = setup.mailer.outbox
    assert mail.to == "ops@a.example.invalid"
    link = mail.headers["List-Unsubscribe"].strip("<>")
    assert mail.headers["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"
    assert link in mail.text
    path = link.removeprefix("https://status.example.invalid")
    assert path.startswith("/abone/iptal/")

    page = await http.get(path)
    assert page.status_code == 200
    assert (await setup.get(AlertRecipient, recipient)).unsubscribed_at is None  # GET only shows
    # a mail client's one-click POST: no session, no form token
    for _ in range(2):  # idempotent
        done = await http.post(path, content=b"List-Unsubscribe=One-Click")
        assert done.status_code == 200
        assert done.headers["referrer-policy"] == "no-referrer"
    assert (await setup.get(AlertRecipient, recipient)).unsubscribed_at is not None

    await setup.delivery(org, product, channel, recipient=recipient)
    assert (await setup.worker().run()).skipped == 1  # nothing for an unsubscribed address
    assert len(setup.mailer.outbox) == 1

    forged = path[:-1] + ("0" if path[-1] != "0" else "1")
    assert (await http.post(forged)).status_code == 404


def test_unsubscribe_tokens_are_keyed() -> None:
    recipient = uuid.uuid4()
    token = unsubscribe_token("key-one", recipient)
    assert check_unsubscribe("key-one", token) == recipient
    assert check_unsubscribe("key-two", token) is None
    assert check_unsubscribe("key-one", "not-a-token") is None
    assert check_unsubscribe("key-one", uuid.uuid4().hex + token[32:]) is None


async def test_weekly_digest_goes_out_once_on_monday_morning(services: AppServices) -> None:
    setup = Setup(services)
    setup.now = datetime(2026, 10, 18, 12, 0, tzinfo=UTC)  # Sunday
    org, _ = await setup.org()
    channel = await setup.channel(org, kind="email", weekly_digest=True)
    await setup.recipient(org, channel, "lead@a.example.invalid")
    await setup.channel(org, kind="email")  # no digest asked
    worker = setup.worker()
    assert await worker.run_weekly(services.digest) == 0  # first run: cursor only
    async with services.db.system("test check") as s:
        cursor = await s.get(JobCursor, WEEKLY)
        assert cursor is not None
        assert cursor.position == datetime(2026, 10, 12, 6, 0, tzinfo=UTC)
    setup.now = datetime(2026, 10, 19, 5, 59, tzinfo=UTC)  # Monday 08:59
    assert await worker.run_weekly(services.digest) == 0
    setup.now = MONDAY_NOON
    assert await worker.run_weekly(services.digest) == 1
    (mail,) = setup.mailer.outbox
    assert mail.subject == "Entegratör Status: A haftalık özeti"
    assert "Geçen hafta bağımlılıklarında dikkat gerektiren bir şey olmadı" in mail.text
    assert "/o/a/ozet" in mail.text
    assert "List-Unsubscribe" in mail.headers
    setup.now += timedelta(hours=5)
    assert await worker.run_weekly(services.digest) == 0  # once a week


def test_time_slots_are_turkey_time() -> None:
    channel = AlertChannel(quiet_start="22:00", quiet_end="08:00")
    assert quiet_until(channel, datetime(2026, 10, 20, 18, 59, tzinfo=UTC)) is None
    assert quiet_until(channel, datetime(2026, 10, 20, 19, 0, tzinfo=UTC)) == datetime(
        2026, 10, 21, 5, 0, tzinfo=UTC
    )
    assert quiet_until(channel, datetime(2026, 10, 21, 2, 0, tzinfo=UTC)) == datetime(
        2026, 10, 21, 5, 0, tzinfo=UTC
    )
    assert quiet_until(channel, datetime(2026, 10, 21, 5, 0, tzinfo=UTC)) is None
    day = AlertChannel(quiet_start="12:00", quiet_end="13:30")
    assert quiet_until(day, datetime(2026, 10, 21, 9, 15, tzinfo=UTC)) == datetime(
        2026, 10, 21, 10, 30, tzinfo=UTC
    )
    for broken in (("", "08:00"), ("25:00", "08:00"), ("08:00", "08:00"), (None, None)):
        assert (
            quiet_until(AlertChannel(quiet_start=broken[0], quiet_end=broken[1]), MONDAY_NOON)
            is None
        )
    assert daily_slot(datetime(2026, 10, 20, 5, 59, tzinfo=UTC)) == datetime(
        2026, 10, 20, 6, 0, tzinfo=UTC
    )
    assert daily_slot(datetime(2026, 10, 20, 6, 0, tzinfo=UTC)) == datetime(
        2026, 10, 21, 6, 0, tzinfo=UTC
    )
    assert weekly_slot(MONDAY_NOON) == datetime(2026, 10, 19, 6, 0, tzinfo=UTC)
    assert weekly_slot(datetime(2026, 10, 19, 5, 0, tzinfo=UTC)) == datetime(
        2026, 10, 12, 6, 0, tzinfo=UTC
    )
    assert weekly_slot(datetime(2026, 10, 25, 20, 0, tzinfo=UTC)) == datetime(
        2026, 10, 19, 6, 0, tzinfo=UTC
    )


async def test_deliveries_of_a_channel_stay_in_its_organization(services: AppServices) -> None:
    """The sealed URL opens only with the channel's own organization and id (AAD)."""
    setup = Setup(services)
    org, product = await setup.org()
    channel = await setup.channel(org, secret={"url": "https://hooks.example/in", "secret": "x"})
    other = await setup.channel(org)
    async with services.db.system("test tamper") as s:
        row = await s.get(AlertChannel, channel)
        moved = await s.get(AlertChannel, other)
        assert row is not None
        assert moved is not None
        moved.sealed = row.sealed  # copied onto another channel
    delivery = await setup.delivery(org, product, other)
    await setup.worker().run()
    assert (await setup.get(AlertDelivery, delivery)).last_error == "vault"
    assert setup.hooks.requests == []
    async with services.db.system("test check") as s:
        assert (await s.scalars(select(AlertDelivery))).all()


class BrokenMailer:
    async def send(self, mail: Any) -> None:
        raise ConnectionRefusedError


async def test_mail_and_telegram_failures_are_codes(services: AppServices) -> None:
    setup = Setup(services)
    org, product = await setup.org()
    email = await setup.channel(org, kind="email")
    telegram = await setup.channel(org, kind="telegram")
    first = await setup.delivery(
        org, product, email, recipient=await setup.recipient(org, email, "ops@a.example.invalid")
    )
    second = await setup.delivery(
        org, product, telegram, recipient=await setup.recipient(org, telegram, "4242")
    )
    worker = DeliveryWorker(
        services.db,
        Senders(setup.hooks.client, BrokenMailer()),  # no bot token
        services.credentials,
        public_base_url="https://status.example.invalid",
        unsubscribe_key="k" * 32,
        clock=lambda: setup.now,
    )
    assert (await worker.run()).retried == 2
    assert (await setup.get(AlertDelivery, first)).last_error == "smtp_ConnectionRefusedError"
    assert (await setup.get(AlertDelivery, second)).last_error == "telegram_disabled"


def test_digest_text_has_every_section() -> None:
    now = MONDAY_NOON
    impact = Impact(
        kind="change",
        subject_id=uuid.uuid4(),
        component_id=uuid.uuid4(),
        depth=1,
        path=("Fatura", "Entegratör X — API"),
        severity="high",
        reason="used_operation_breaking",
        title="openapi",
        when=now,
    )
    risk = Risk(kind="cert_expiry", due_at=now + timedelta(days=10))
    digest = Digest(
        start=now - timedelta(days=7),
        end=now,
        changes=[impact],
        outages=[
            Outage(
                "GİB — e-Fatura", now - timedelta(days=2), now - timedelta(days=2, hours=-1), None
            ),
            Outage("GİB — e-Arşiv", now - timedelta(hours=1), None, "timeout"),
        ],
        due=[
            RegisterEntry(risk=risk, severity="medium", component="Entegratör X — API", products=[])
        ],
        planned=[
            CalendarItem(when=now + timedelta(days=3), kind="change", title="v2", component="API"),
            CalendarItem(when=now + timedelta(days=9), kind="risk", title="eol", component="API"),
        ],
    )
    message = render_digest(digest, "A", "https://status.example.invalid/o/a/ozet")
    text = message.text
    for expected in (
        "Geçen hafta değişenler",
        "- [Yüksek] Kullandığın bir işlemde kırıcı değişiklik: openapi",
        "Fatura → Entegratör X — API",
        "GİB — e-Arşiv: 19.10.2026 11:00 - Devam ediyor",
        "TLS sertifikasının süresi doluyor · Entegratör X — API",
        "v2 · API",
        "Destek süresi bitiyor · API",
    ):
        assert expected in text
    assert "dikkat gerektiren bir şey olmadı" not in text
    assert message.payload == {
        "type": "weekly_digest",
        "start": digest.start.isoformat(),
        "end": digest.end.isoformat(),
        "changes": 1,
        "outages": 2,
        "due": 1,
        "planned": 2,
    }


def reference_verify() -> Any:
    """The Python block of docs/WEBHOOKS.md, run as written."""
    doc = (Path(__file__).parent.parent / "docs" / "WEBHOOKS.md").read_text()
    match = re.search(r"```python\n(.*?)```", doc, re.DOTALL)
    assert match is not None
    namespace: dict[str, Any] = {}
    exec(compile(match.group(1), "WEBHOOKS.md", "exec"), namespace)  # noqa: S102 - our own doc
    return namespace["verify"]


async def test_the_documented_snippet_verifies_real_requests(services: AppServices) -> None:
    verify = reference_verify()
    setup = Setup(services)
    org, product = await setup.org()
    secret = {"url": "https://hooks.example/in", "secret": "doc-check-secret"}
    channel = await setup.channel(org, secret=secret)
    await setup.delivery(org, product, channel)
    await setup.worker().run()
    (request,) = setup.hooks.requests
    timestamp = request.headers[TIMESTAMP_HEADER]
    signature = request.headers[SIGNATURE_HEADER]
    assert verify("doc-check-secret", request.content, timestamp, signature)
    assert not verify("wrong-secret", request.content, timestamp, signature)
    assert not verify("doc-check-secret", request.content + b" ", timestamp, signature)
    assert not verify("doc-check-secret", request.content, "not-a-number", signature)
    late = int(timestamp) + 301
    assert not verify("doc-check-secret", request.content, timestamp, signature, now=late)
    assert verify("doc-check-secret", request.content, timestamp, signature, now=late - 2)
