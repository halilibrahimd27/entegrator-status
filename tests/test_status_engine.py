"""The status engine against real rows: rounds, timelines, incidents, maintenance, tenancy.

Patterns describe one character per minute and node: ``O`` ok, ``F`` failing (HTTP 5xx),
``x`` failing on an unhealthy node, ``-`` no result.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta

import pytest
from sqlalchemy import func, select, update

from probe_support import add_source, two_orgs
from sentinel.db.base import utcnow
from sentinel.engine.consensus import round_start
from sentinel.engine.status import GRACE, StatusEngine
from sentinel.models import (
    ComponentState,
    Incident,
    MaintenanceWindow,
    Observation,
    Source,
    SourceRound,
)
from sentinel.tenancy.roles import Role
from sentinel.tenancy.scope import TenantScope
from sentinel.web.services import AppServices

T0 = round_start(utcnow(), 60) + timedelta(minutes=5)
Timeline = list[tuple[str, int, int | None]]


def minute(at: datetime | None) -> int | None:
    return None if at is None else int((at - T0).total_seconds() // 60)


class World:
    def __init__(self, services: AppServices) -> None:
        self.services = services
        self.nodes: dict[str, uuid.UUID] = {}

    async def node(self, name: str) -> uuid.UUID:
        if name not in self.nodes:
            creds = await self.services.nodes.register(f"node-{name}", "test")
            self.nodes[name] = creds.node_id
        return self.nodes[name]

    async def source(
        self, org_id: uuid.UUID | None = None, url: str = "https://portal.example.invalid/"
    ) -> Source:
        source_id = await add_source(self.services, org_id, url)
        async with self.services.db.system("test setup") as s:
            source = await s.get(Source, source_id)
            assert source is not None
            source.interval_s = 60
            source.created_at = T0 - timedelta(minutes=2)
            return source

    async def feed(self, source: Source, **patterns: str) -> None:
        rows = []
        for name, pattern in patterns.items():
            node_id = await self.node(name)
            for i, char in enumerate(pattern):
                if char == "-":
                    continue
                rows.append(
                    Observation(
                        org_id=source.org_id,
                        source_id=source.id,
                        node_id=node_id,
                        observed_at=T0 + timedelta(minutes=i, seconds=30),
                        ok=char == "O",
                        error_class=None if char == "O" else "http_5xx",
                        status_code=200 if char == "O" else 503,
                        latency_ms=120,
                        node_healthy=char != "x",
                    )
                )
        async with self.services.db.system("test setup") as s:
            s.add_all(rows)

    async def run(self, minutes: float) -> StatusEngine:
        engine = StatusEngine(
            self.services.db, clock=lambda: T0 + timedelta(minutes=minutes) + GRACE
        )
        self.report = await engine.run()
        return engine

    async def timeline(self, source: Source) -> Timeline:
        async with self.services.db.system("test check") as s:
            rows = (
                await s.scalars(
                    select(ComponentState)
                    .where(ComponentState.component_id == source.component_id)
                    .order_by(ComponentState.started_at)
                )
            ).all()
        return [(r.state, minute(r.started_at) or 0, minute(r.ended_at)) for r in rows]

    async def incidents(self, source: Source) -> list[tuple[int | None, int | None, str | None]]:
        async with self.services.db.system("test check") as s:
            rows = (
                await s.scalars(
                    select(Incident)
                    .where(Incident.component_id == source.component_id)
                    .order_by(Incident.started_at)
                )
            ).all()
        return [(minute(r.started_at), minute(r.resolved_at), r.cause) for r in rows]


@pytest.fixture
def world(services: AppServices) -> World:
    return World(services)


async def test_two_nodes_failing_three_rounds_open_and_resolve_an_incident(world: World) -> None:
    source = await world.source()
    await world.feed(source, a="OOFFFOOOO", b="OOFFFOOOO")
    await world.run(9)
    assert await world.timeline(source) == [
        ("operational", 0, 2),
        ("down", 2, 5),  # dated back to the first failing round
        ("operational", 5, None),  # dated back to the first of three successes
    ]
    assert await world.incidents(source) == [(2, 5, "http_5xx")]
    assert (world.report.opened, world.report.resolved) == (1, 1)


@pytest.mark.parametrize(
    "patterns",
    [
        {"a": "OOFFFFFF", "b": "OOOOOOOO"},  # the nodes disagree
        {"a": "OOFFFFFF"},  # a single node
        {"a": "OOxxxxxx", "b": "OOFFFFFF"},  # the other failing node is unhealthy
    ],
)
async def test_unconfirmed_failures_are_unverified_never_down(
    world: World, patterns: dict[str, str]
) -> None:
    source = await world.source()
    await world.feed(source, **patterns)
    await world.run(8)
    assert await world.timeline(source) == [("operational", 0, 2), ("unverified", 2, None)]
    assert await world.incidents(source) == []


async def test_missing_results_do_not_break_a_confirmed_outage(world: World) -> None:
    source = await world.source()
    # node b skipped minute 3 (schedule jitter); its minute-2 result still speaks for it
    await world.feed(source, a="OOFFFF", b="OOF-FF")
    await world.run(6)
    assert await world.timeline(source) == [("operational", 0, 2), ("down", 2, None)]


async def test_flaps_within_ten_minutes_reuse_the_incident(world: World) -> None:
    source = await world.source()
    pattern = "OFFFOOO" + "FFFOOO" + "O" * 12 + "FFFOOO"
    await world.feed(source, a=pattern, b=pattern)
    await world.run(len(pattern))
    # down 1 to 4, again 7 to 10 (3 minutes after resolving: same incident), 25 to 28
    assert await world.incidents(source) == [(1, 10, "http_5xx"), (25, 28, "http_5xx")]
    assert world.report.reopened == 1
    states = [state for state, _, _ in await world.timeline(source)]
    assert states == [
        "operational",
        "down",
        "operational",
        "down",
        "operational",
        "down",
        "operational",
    ]


async def test_alternating_results_open_no_incident(world: World) -> None:
    source = await world.source()
    await world.feed(source, a="FO" * 15, b="FO" * 15)
    await world.run(30)
    assert await world.incidents(source) == []
    assert {state for state, _, _ in await world.timeline(source)} == {"operational", "unverified"}


async def test_maintenance_suppresses_incidents_of_its_owner_only(
    world: World, services: AppServices
) -> None:
    org_a, _ = await two_orgs(services)
    shared = await world.source(None, "https://shared.example.invalid/")
    private = await world.source(org_a, "https://private.example.invalid/")
    async with services.db.system("test setup") as s:
        for source, org in ((shared, None), (private, org_a)):
            s.add(
                MaintenanceWindow(
                    org_id=org,
                    component_id=source.component_id,
                    title="Planlı bakım",
                    starts_at=T0,
                    ends_at=T0 + timedelta(minutes=30),
                )
            )
        # an organization's own window must not silence a shared component
        s.add(
            MaintenanceWindow(
                org_id=org_a,
                component_id=shared.component_id,
                title="x",
                starts_at=T0 + timedelta(minutes=40),
                ends_at=T0 + timedelta(minutes=60),
            )
        )
    pattern = "OFFFOOO" + "O" * 40 + "FFFOOO"
    for source in (shared, private):
        await world.feed(source, a=pattern, b=pattern)
    await world.run(len(pattern))
    # the first outage falls into each owner's window; the second only into the
    # organization's window on the shared component, which does not count
    assert await world.incidents(private) == [(47, 50, "http_5xx")]
    assert await world.incidents(shared) == [(47, 50, "http_5xx")]
    # the timeline still shows what happened; maintenance is an overlay at read time
    assert ("down", 1, 4) in await world.timeline(private)


async def test_runs_are_incremental(world: World) -> None:
    source = await world.source()
    pattern = "OOFFFOOOO"
    await world.feed(source, a=pattern, b=pattern)
    await world.run(3)
    await world.run(3)
    assert world.report.rounds == 0  # nothing new to judge
    await world.run(9)
    assert await world.timeline(source) == [
        ("operational", 0, 2),
        ("down", 2, 5),
        ("operational", 5, None),
    ]
    async with world.services.db.system("test check") as s:
        stored = await s.scalar(select(func.count()).select_from(SourceRound))
    assert stored == len(pattern)  # known rounds only, each once


async def test_catch_up_is_bounded(world: World) -> None:
    source = await world.source()
    async with world.services.db.system("test setup") as s:
        await s.execute(
            update(Source).where(Source.id == source.id).values(created_at=T0 - timedelta(days=3))
        )
    await world.run(0)
    assert 24 * 60 - 1 <= world.report.rounds <= 24 * 60


async def test_latency_threshold_marks_degraded(world: World, services: AppServices) -> None:
    source = await world.source()
    async with services.db.system("test setup") as s:
        await s.execute(
            update(Source)
            .where(Source.id == source.id)
            .values(config={**source.config, "latency_degraded_ms": 100})
        )
    await world.feed(source, a="O" * 16, b="O" * 16)  # 120 ms each
    await world.run(16)
    assert await world.timeline(source) == [("degraded", 0, None)]


async def test_component_without_sources_becomes_unknown(
    world: World, services: AppServices
) -> None:
    source = await world.source()
    await world.feed(source, a="OO", b="OO")
    await world.run(2)
    async with services.db.system("test setup") as s:
        await s.execute(update(Source).where(Source.id == source.id).values(enabled=False))
    await world.run(5)
    assert await world.timeline(source) == [("operational", 0, 6), ("unknown", 6, None)]


async def test_status_rows_follow_tenancy(world: World, services: AppServices) -> None:
    org_a, org_b = await two_orgs(services)
    private = await world.source(org_a, "https://private.example.invalid/")
    shared = await world.source(None, "https://shared.example.invalid/")
    for source in (private, shared):
        await world.feed(source, a="OFFFF", b="OFFFF")
    await world.run(5)
    for org, expected in (
        (org_a, {private.component_id, shared.component_id}),
        (org_b, {shared.component_id}),
    ):
        scope = TenantScope(org_id=org, user_id=None, role=Role.VIEWER)
        async with services.db.tenant(scope) as s:
            seen = set((await s.scalars(select(ComponentState.component_id))).all())
            incidents = set((await s.scalars(select(Incident.component_id))).all())
            rounds = set((await s.scalars(select(SourceRound.source_id))).all())
        assert seen == expected
        assert incidents == expected
        assert len(rounds) == len(expected)
