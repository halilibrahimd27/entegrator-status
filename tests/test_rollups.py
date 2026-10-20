"""Rollups and the read models built on them, on a fixed clock with hand-computed seconds."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from probe_support import add_source
from sentinel.engine.rollups import run_rollups
from sentinel.engine.views import StatusViews
from sentinel.models import (
    ComponentDaily,
    ComponentState,
    MaintenanceWindow,
    Observation,
    Organization,
    Source,
    SourceHourly,
)
from sentinel.tenancy.roles import Role
from sentinel.tenancy.scope import TenantScope
from sentinel.web.services import AppServices

NOW = datetime(2026, 10, 20, 6, 0, tzinfo=UTC)
D0 = datetime(2026, 10, 20, tzinfo=UTC)  # "today", UTC


def day(n: int, hour: float = 0) -> datetime:
    return D0 + timedelta(days=n, hours=hour)


async def shared_source(services: AppServices) -> Source:
    source_id = await add_source(services, None, "https://portal.example.invalid/")
    async with services.db.system("test lookup") as s:
        source = await s.get(Source, source_id)
        assert source is not None
        return source


async def test_hourly_latency(services: AppServices) -> None:
    source = await shared_source(services)
    node = (await services.nodes.register("n1", "test")).node_id
    hour = NOW.replace(minute=0)

    def obs(at: datetime, latency: int, ok: bool = True, healthy: bool = True) -> Observation:
        return Observation(
            org_id=None,
            source_id=source.id,
            node_id=node,
            observed_at=at,
            ok=ok,
            error_class=None if ok else "http_5xx",
            latency_ms=latency,
            node_healthy=healthy,
        )

    two_ago = hour - timedelta(hours=2)
    rows = [
        obs(two_ago + timedelta(minutes=m), ms)
        for m, ms in ((1, 100), (2, 200), (3, 300), (4, 400))
    ]
    rows += [
        obs(two_ago + timedelta(minutes=5), 5000, ok=False),  # failures have no latency to speak of
        obs(two_ago + timedelta(minutes=6), 9000, healthy=False),  # nor do unhealthy nodes
        obs(hour - timedelta(minutes=30), 50),
        obs(hour + timedelta(minutes=5), 70),  # the current hour is not finished
    ]
    async with services.db.system("test setup") as s:
        s.add_all(rows)

    report = await run_rollups(services.db, clock=lambda: NOW)
    assert report.hourly == 2
    async with services.db.system("test check") as s:
        stored = {
            r.hour: (r.samples, r.p50_ms, r.p95_ms)
            for r in (await s.scalars(select(SourceHourly))).all()
        }
    assert stored == {two_ago: (4, 250, 385), hour - timedelta(hours=1): (1, 50, 50)}
    assert (await run_rollups(services.db, clock=lambda: NOW)).hourly == 0


@pytest.fixture
async def timeline(services: AppServices) -> Source:
    """Up from three days ago; down for an hour two days ago; maintenance yesterday."""
    source = await shared_source(services)
    component = source.component_id
    async with services.db.system("test setup") as s:
        s.add_all(
            [
                ComponentState(
                    component_id=component,
                    state="operational",
                    started_at=day(-3),
                    ended_at=day(-2, 12),
                ),
                ComponentState(
                    component_id=component,
                    state="down",
                    started_at=day(-2, 12),
                    ended_at=day(-2, 13),
                ),
                ComponentState(component_id=component, state="operational", started_at=day(-2, 13)),
                MaintenanceWindow(
                    component_id=component,
                    title="Bakım",
                    starts_at=day(-1, 10),
                    ends_at=day(-1, 11),
                ),
            ]
        )
    return source


async def test_daily_rollup(services: AppServices, timeline: Source) -> None:
    report = await run_rollups(services.db, clock=lambda: NOW)
    assert report.daily == 3  # three finished days; today is computed live
    async with services.db.system("test check") as s:
        rows = {
            r.day: (r.operational_s, r.down_s, r.maintenance_s, r.unknown_s)
            for r in (await s.scalars(select(ComponentDaily))).all()
        }
    hour = 3600
    assert rows == {
        day(-3).date(): (24 * hour, 0, 0, 0),
        day(-2).date(): (23 * hour, hour, 0, 0),
        day(-1).date(): (23 * hour, 0, hour, 0),
    }
    assert (await run_rollups(services.db, clock=lambda: NOW)).daily == 2  # last two rewritten


async def test_uptime_and_bars(services: AppServices, timeline: Source) -> None:
    await run_rollups(services.db, clock=lambda: NOW)
    async with services.db.system("test setup") as s:
        org = Organization(slug="a", name="A")
        s.add(org)
        await s.flush()
    scope = TenantScope(org_id=org.id, user_id=None, role=Role.VIEWER)
    views = StatusViews(services.db, clock=lambda: NOW)
    component = timeline.component_id

    uptime = (await views.uptime(scope, [component]))[component]
    assert uptime["24h"] == 1.0  # the maintenance hour does not count
    assert uptime["7d"] == pytest.approx(76 / 77)  # 78 h observed, 1 h maintenance, 1 h down
    assert uptime["90d"] == uptime["7d"]  # nothing measured before

    bars = (await views.bars(scope, [component], days=5))[component]
    assert [(b.day, b.worst) for b in bars] == [
        (day(-4).date(), "unknown"),
        (day(-3).date(), "operational"),
        (day(-2).date(), "down"),
        (day(-1).date(), "operational"),
        (day(0).date(), "operational"),  # live
    ]
    assert bars[2].uptime == pytest.approx(23 / 24)
    assert await views.uptime(scope, []) == {}
    assert await views.bars(scope, []) == {}
    assert await views.latency(scope, []) == {}
    assert await views.current(scope, [uuid.uuid4()]) == {}  # unknown ids are skipped
