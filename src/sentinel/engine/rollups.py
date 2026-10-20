"""Rollups: hourly latency per source, seconds per state per UTC day per component.

Both are idempotent. Hourly rows are written once per finished hour (a late result
does not change them). Daily rows are rewritten for the last two days, because the
open interval of a timeline may still change state at its start (ADR 0008).
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from sqlalchemy import ColumnElement, Integer, cast, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from sentinel.db.base import utcnow
from sentinel.db.session import Database
from sentinel.engine.uptime import Span, day_bounds, tally
from sentinel.models import (
    Component,
    ComponentDaily,
    ComponentState,
    MaintenanceWindow,
    Observation,
    Source,
    SourceHourly,
)

HOURLY_LOOKBACK = timedelta(hours=48)
DAILY_BACKFILL_DAYS = 90
DAILY_REWRITE_DAYS = 2


@dataclass(frozen=True)
class RollupReport:
    hourly: int
    daily: int


def _hour(at: datetime) -> datetime:
    return at.replace(minute=0, second=0, microsecond=0)


async def run_rollups(db: Database, clock: Callable[[], datetime] = utcnow) -> RollupReport:
    now = clock()
    return RollupReport(await _hourly(db, now), await _daily(db, now))


async def _hourly(db: Database, now: datetime) -> int:
    end = _hour(now)
    start = end - HOURLY_LOOKBACK
    hour = func.date_trunc("hour", Observation.observed_at, "UTC")
    latency = Observation.latency_ms

    def pct(p: float) -> ColumnElement[int]:
        return cast(func.round(func.percentile_cont(p).within_group(latency)), Integer)

    rows = (
        select(Source.org_id, Observation.source_id, hour, func.count(), pct(0.5), pct(0.95))
        .join(Source, Source.id == Observation.source_id)
        .where(
            Observation.ok.is_(True),
            Observation.node_healthy.is_(True),
            latency.is_not(None),
            Observation.observed_at >= start,
            Observation.observed_at < end,
        )
        .group_by(Source.org_id, Observation.source_id, hour)
    )
    async with db.system("rollups: hourly latency of every source") as s:
        result = await s.execute(
            insert(SourceHourly)
            .from_select(["org_id", "source_id", "hour", "samples", "p50_ms", "p95_ms"], rows)
            .on_conflict_do_nothing()
            .returning(SourceHourly.source_id)
        )
        return len(result.all())


async def _daily(db: Database, now: datetime) -> int:
    today = now.date()
    written = 0
    async with db.system("rollups: list components with a timeline") as s:
        component_ids = (await s.scalars(select(ComponentState.component_id).distinct())).all()
    for component_id in component_ids:
        async with db.system("rollups: daily seconds of one component") as s:
            written += await _component_days(s, component_id, today, now)
    return written


async def _component_days(
    s: AsyncSession, component_id: uuid.UUID, today: date, now: datetime
) -> int:
    component = await s.get(Component, component_id)
    if component is None:  # pragma: no cover - deleted while running
        return 0
    last = await s.scalar(
        select(func.max(ComponentDaily.day)).where(ComponentDaily.component_id == component_id)
    )
    first_interval = await s.scalar(
        select(func.min(ComponentState.started_at)).where(
            ComponentState.component_id == component_id
        )
    )
    if first_interval is None:  # pragma: no cover - listed above
        return 0
    first = max(first_interval.date(), today - timedelta(days=DAILY_BACKFILL_DAYS))
    if last is not None:
        first = max(first, last - timedelta(days=DAILY_REWRITE_DAYS - 1))
    if first >= today:
        return 0
    start, _ = day_bounds(first)
    end, _ = day_bounds(today)
    spans = [
        Span(row.state, row.started_at, row.ended_at)
        for row in (
            await s.scalars(
                select(ComponentState).where(
                    ComponentState.component_id == component_id,
                    ComponentState.started_at < end,
                    (ComponentState.ended_at.is_(None)) | (ComponentState.ended_at > start),
                )
            )
        ).all()
    ]
    windows = [
        (w.starts_at, w.ends_at)
        for w in (
            await s.scalars(
                select(MaintenanceWindow).where(
                    MaintenanceWindow.component_id == component_id,
                    MaintenanceWindow.org_id.is_not_distinct_from(component.org_id),
                    MaintenanceWindow.starts_at < end,
                    MaintenanceWindow.ends_at > start,
                )
            )
        ).all()
    ]
    rows = []
    day = first
    while day < today:
        lo, hi = day_bounds(day)
        seconds = tally(spans, windows, lo, hi, now).seconds
        rows.append(
            {
                "org_id": component.org_id,
                "component_id": component_id,
                "day": day,
                **{f"{state}_s": round(value) for state, value in seconds.items()},
            }
        )
        day += timedelta(days=1)
    statement = insert(ComponentDaily).values(rows)
    await s.execute(
        statement.on_conflict_do_update(
            index_elements=["component_id", "day"],
            set_={
                name: statement.excluded[name]
                for name in (
                    "operational_s",
                    "degraded_s",
                    "unverified_s",
                    "down_s",
                    "unknown_s",
                    "maintenance_s",
                )
            },
        )
    )
    return len(rows)
