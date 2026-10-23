"""Alert events and deliveries: one message per recipient and channel, per organization."""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any

from sqlalchemy import func, select

from probe_support import two_orgs
from sentinel.alerts.events import run_events
from sentinel.alerts.rules import matches
from sentinel.db.base import utcnow
from sentinel.models import (
    AlertChannel,
    AlertDelivery,
    AlertEvent,
    AlertRecipient,
    AlertRule,
    Change,
    Component,
    Dependency,
    Incident,
    IncidentKind,
    IncidentStatus,
    Product,
    Service,
)
from sentinel.web.services import AppServices


class World:
    def __init__(self, services: AppServices) -> None:
        self.services = services
        self.now = utcnow()

    async def component(self, name: str) -> uuid.UUID:
        async with self.services.db.system("test setup") as s:
            service = Service(slug=f"s-{uuid.uuid4().hex[:6]}", name=name, kind="other", tags=[])
            s.add(service)
            await s.flush()
            component = Component(service_id=service.id, slug="api", name="API", tags=[])
            s.add(component)
            await s.flush()
            return component.id

    async def product(
        self, org: uuid.UUID, component: uuid.UUID, ops: list[str] | None = None
    ) -> uuid.UUID:
        async with self.services.db.system("test setup") as s:
            product = Product(org_id=org, slug=f"p-{uuid.uuid4().hex[:6]}", name="Fatura")
            s.add(product)
            await s.flush()
            usage = {"operations": ops} if ops else {}
            s.add(
                Dependency(
                    org_id=org, from_product_id=product.id, to_component_id=component, usage=usage
                )
            )
            return product.id

    async def channel(self, org: uuid.UUID, kind: str = "webhook", **rule: Any) -> uuid.UUID:
        async with self.services.db.system("test setup") as s:
            channel = AlertChannel(org_id=org, name=f"c-{uuid.uuid4().hex[:6]}", kind=kind)
            s.add(channel)
            await s.flush()
            s.add(
                AlertRule(
                    org_id=org,
                    name="r",
                    channel_id=channel.id,
                    kinds=rule.pop("kinds", ["incident", "change", "risk"]),
                    min_severity=rule.pop("min_severity", "low"),
                    **rule,
                )
            )
            return channel.id

    async def run(self, minutes: float = 0) -> Any:
        return await run_events(
            self.services.db, clock=lambda: self.now + timedelta(minutes=minutes)
        )

    async def deliveries(self) -> list[AlertDelivery]:
        async with self.services.db.system("test check") as s:
            return list((await s.scalars(select(AlertDelivery))).all())


async def incident(world: World, component: uuid.UUID, **extra: Any) -> uuid.UUID:
    async with world.services.db.system("test setup") as s:
        row = Incident(
            kind=IncidentKind.AUTO,
            status=IncidentStatus.OPEN,
            component_id=component,
            started_at=world.now,
            created_at=world.now + timedelta(seconds=1),
            **extra,
        )
        s.add(row)
        await s.flush()
        return row.id


async def test_first_run_does_not_replay_history(services: AppServices) -> None:
    world = World(services)
    api = await world.component("Entegratör X")
    org_a, _ = await two_orgs(services)
    await world.product(org_a, api)
    await world.channel(org_a)
    await incident(world, api)
    assert (await world.run()).events == 0  # only sets the cursor
    assert (await world.run(1)).events == 1  # the overlap window picks it up now


async def test_one_message_per_recipient_and_channel(services: AppServices) -> None:
    world = World(services)
    api = await world.component("Entegratör X")
    org_a, org_b = await two_orgs(services)
    product_a = await world.product(org_a, api)
    product_b = await world.product(org_b, api)
    webhook_a = await world.channel(org_a)
    async with services.db.system("test setup") as s:  # a second rule on the same channel
        s.add(
            AlertRule(
                org_id=org_a,
                name="again",
                channel_id=webhook_a,
                kinds=["incident"],
                min_severity="low",
            )
        )
    email_b = await world.channel(org_b, kind="email")
    async with services.db.system("test setup") as s:
        s.add_all(
            [
                AlertRecipient(
                    org_id=org_b,
                    channel_id=email_b,
                    address="ops@b.example.invalid",
                    confirmed_at=world.now,
                ),
                AlertRecipient(
                    org_id=org_b,
                    channel_id=email_b,
                    address="lead@b.example.invalid",
                    confirmed_at=world.now,
                ),
                AlertRecipient(
                    org_id=org_b, channel_id=email_b, address="new@b.example.invalid"
                ),  # unconfirmed
            ]
        )
    await world.run()
    await incident(world, api)
    report = await world.run(1)
    assert report.events == 2  # one per product
    deliveries = await world.deliveries()
    by_channel: dict[uuid.UUID, list[str]] = {}
    for delivery in deliveries:
        by_channel.setdefault(delivery.channel_id, []).append(delivery.recipient_key)
    assert by_channel[webhook_a] == [""]  # two matching rules, one message
    assert len(by_channel[email_b]) == 2  # the unconfirmed address gets nothing
    async with services.db.system("test check") as s:
        events = {e.product_id: e for e in (await s.scalars(select(AlertEvent))).all()}
    assert set(events) == {product_a, product_b}
    for delivery in deliveries:
        assert (
            delivery.org_id
            == events[product_a if delivery.channel_id == webhook_a else product_b].org_id
        )
    again = await world.run(2)
    assert (again.events, again.deliveries) == (0, 0)  # the overlap never sends twice


async def test_rules_filter_and_never_cross_organizations(services: AppServices) -> None:
    world = World(services)
    api = await world.component("Entegratör X")
    upstream = await world.component("GİB")
    async with services.db.system("test setup") as s:
        s.add(Dependency(from_component_id=api, to_component_id=upstream, usage={}))
    org_a, org_b = await two_orgs(services)
    product_a = await world.product(org_a, api, ["POST /invoices"])
    await world.product(org_b, api)
    strict = await world.channel(org_a, min_severity="high")
    # a rule of org B that names org A's product must still only ever see org B's events
    forged = await world.channel(org_b, product_id=product_a)
    await world.run()
    await incident(world, upstream)  # depth 2: medium
    async with services.db.system("test setup") as s:
        for category, ops in (
            ("non_breaking", ["POST /invoices"]),
            ("breaking", ["POST /invoices"]),
        ):
            s.add(
                Change(
                    component_id=api,
                    kind="contract",
                    category=category,
                    title="openapi",
                    detected_at=world.now,
                    created_at=world.now + timedelta(seconds=1),
                    fingerprint=uuid.uuid4().hex * 2,
                    detail={"operations": ops},
                )
            )
    await world.run(1)
    deliveries = await world.deliveries()
    assert {d.channel_id for d in deliveries} == {strict}  # nothing for the forged rule
    async with services.db.system("test check") as s:
        delivered = (
            await s.scalars(
                select(AlertEvent).where(AlertEvent.id.in_([d.event_id for d in deliveries]))
            )
        ).all()
        total = await s.scalar(select(func.count()).select_from(AlertEvent))
    assert [(e.subject_type, e.severity, e.reason) for e in delivered] == [
        (
            "change",
            "high",
            "used_operation_breaking",
        )  # medium incident filtered; non-breaking left out
    ]
    assert total == 6  # 3 subjects x 2 products: rules decide who hears about them
    assert forged not in {d.channel_id for d in deliveries}


async def test_resolution_is_its_own_event(services: AppServices) -> None:
    world = World(services)
    api = await world.component("Entegratör X")
    org_a, _ = await two_orgs(services)
    await world.product(org_a, api)
    await world.channel(org_a)
    await world.run()
    incident_id = await incident(world, api)
    await world.run(1)
    async with services.db.system("test change") as s:
        row = await s.get(Incident, incident_id)
        assert row is not None
        row.resolved_at = world.now + timedelta(minutes=2)
        row.status = IncidentStatus.RESOLVED
    report = await world.run(3)
    async with services.db.system("test check") as s:
        phases = sorted((e.phase, e.reason) for e in (await s.scalars(select(AlertEvent))).all())
    assert phases == [("opened", "upstream_down"), ("resolved", "upstream_recovered")]
    assert report.events == 1


def test_rule_matching() -> None:
    product = uuid.uuid4()
    event = AlertEvent(
        product_id=product,
        subject_type="change",
        severity="medium",
        detail={"kind": "release", "service_ids": ["s1"]},
    )
    rule = AlertRule(
        enabled=True,
        product_id=None,
        service_id=None,
        kinds=["change"],
        min_severity="medium",
        include_non_breaking=False,
    )
    assert matches(rule, event)
    for change in (
        {"enabled": False},
        {"product_id": uuid.uuid4()},
        {"kinds": ["risk"]},
        {"min_severity": "high"},
    ):
        other = AlertRule(
            **{
                **{
                    k: getattr(rule, k)
                    for k in (
                        "enabled",
                        "product_id",
                        "service_id",
                        "kinds",
                        "min_severity",
                        "include_non_breaking",
                    )
                },
                **change,
            }
        )
        assert not matches(other, event)
    by_service = AlertRule(
        enabled=True,
        product_id=None,
        service_id="s2",
        kinds=[],
        min_severity="low",
        include_non_breaking=False,
    )
    assert not matches(by_service, event)
    contract = AlertEvent(
        product_id=product,
        subject_type="change",
        severity="low",
        detail={"kind": "contract", "category": "non_breaking"},
    )
    lenient = AlertRule(
        enabled=True,
        product_id=None,
        service_id=None,
        kinds=[],
        min_severity="low",
        include_non_breaking=True,
    )
    assert matches(lenient, contract)
    assert not matches(
        AlertRule(
            enabled=True,
            product_id=None,
            service_id=None,
            kinds=[],
            min_severity="low",
            include_non_breaking=False,
        ),
        contract,
    )
