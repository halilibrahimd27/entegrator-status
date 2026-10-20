"""One status-engine pass: finished rounds → source machines → component timeline → incidents.

Each component is handled in its own transaction, so one bad row cannot stall the rest.
A round is final once ``GRACE`` has passed after it ended; results that arrive later are
stored but do not change history. After downtime the engine catches up at most
``MAX_CATCH_UP``; older rounds are skipped (the machines keep their state).

Timeline changes never rewrite history: a change dated before the current interval
started is clamped to that start. With one availability source per component (the
usual case) back-dated ``down`` and recovery land exactly; with several, a back-dated
change may land up to one round late.
"""

from __future__ import annotations

import bisect
import logging
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from sentinel.db.base import utcnow
from sentinel.db.session import Database
from sentinel.engine.consensus import (
    AVAILABILITY_KINDS,
    Machine,
    Round,
    Rules,
    Sample,
    State,
    Verdict,
    combine,
    judge,
    lookback_start,
    round_start,
    step,
)
from sentinel.models import (
    Component,
    ComponentState,
    Incident,
    IncidentKind,
    IncidentStatus,
    MaintenanceWindow,
    Observation,
    Source,
    SourceRound,
    SourceState,
)

log = logging.getLogger("sentinel.engine")

GRACE = timedelta(seconds=90)  # nodes ship every 10 s; allow for a slow batch
MAX_CATCH_UP = timedelta(hours=24)
FLAP_MERGE = timedelta(minutes=10)


@dataclass
class StatusReport:
    components: int = 0
    rounds: int = 0
    changes: int = 0
    opened: int = 0
    reopened: int = 0
    resolved: int = 0


def threshold_of(source: Source) -> int | None:
    value = source.config.get("latency_degraded_ms") if source.kind == "http" else None
    return int(value) if value else None


class StatusEngine:
    def __init__(
        self,
        db: Database,
        *,
        clock: Callable[[], datetime] = utcnow,
        rules: Rules | None = None,
        grace: timedelta = GRACE,
    ) -> None:
        self._db = db
        self._clock = clock
        self._rules = rules or Rules()
        self._grace = grace

    async def run(self) -> StatusReport:
        now = self._clock()
        report = StatusReport()
        async with self._db.system("status engine: list availability sources") as s:
            sources = (
                await s.scalars(
                    select(Source)
                    .where(Source.enabled.is_(True), Source.kind.in_(AVAILABILITY_KINDS))
                    .order_by(Source.created_at)
                )
            ).all()
            tracked = set(
                (
                    await s.scalars(
                        select(ComponentState.component_id).where(
                            ComponentState.ended_at.is_(None),
                            ComponentState.state != State.UNKNOWN.value,
                        )
                    )
                ).all()
            )
        groups: dict[uuid.UUID, list[Source]] = {}
        for source in sources:
            groups.setdefault(source.component_id, []).append(source)
        for component_id, group in groups.items():
            try:
                await self._component(component_id, group, now, report)
            except Exception:
                log.exception("status engine: component %s failed", component_id)
        for component_id in tracked - groups.keys():  # no enabled availability source left
            await self._forget(component_id, now, report)
        return report

    async def _component(
        self, component_id: uuid.UUID, group: list[Source], now: datetime, report: StatusReport
    ) -> None:
        async with self._db.system("status engine: consensus for one component") as s:
            component = await s.get(Component, component_id)
            if component is None:  # pragma: no cover - deleted while running
                return
            stored = {
                st.source_id: st
                for st in (
                    await s.scalars(
                        select(SourceState).where(SourceState.source_id.in_([g.id for g in group]))
                    )
                ).all()
            }
            machines = {
                src.id: Machine.from_json(stored[src.id].machine) if src.id in stored else Machine()
                for src in group
            }
            events: list[tuple[datetime, str, Source, Round]] = []
            until: dict[uuid.UUID, datetime] = {}
            for src in group:
                state = stored.get(src.id)
                first = (
                    state.processed_until if state else round_start(src.created_at, src.interval_s)
                )
                first = max(first, round_start(now - MAX_CATCH_UP, src.interval_s))
                last = round_start(now - self._grace, src.interval_s)  # rounds ending by then
                until[src.id] = max(last, state.processed_until) if state else last
                if first >= last:
                    continue
                samples = await _samples(s, src.id, lookback_start(first, src.interval_s), last)
                times = [sample.observed_at for sample in samples]
                start = first
                while start < last:
                    lo = bisect.bisect_left(times, lookback_start(start, src.interval_s))
                    hi = bisect.bisect_left(times, start + timedelta(seconds=src.interval_s))
                    events.append(
                        (start, str(src.id), src, judge(samples[lo:hi], start, src.interval_s))
                    )
                    start += timedelta(seconds=src.interval_s)
            if not events and all(src.id in stored for src in group):
                return
            report.components += 1
            events.sort(key=lambda e: (e[0], e[1]))
            rows: list[dict[str, Any]] = []
            for _, _, src, rnd in events:
                machine, change = step(
                    machines[src.id], rnd, threshold_ms=threshold_of(src), rules=self._rules
                )
                machines[src.id] = machine
                report.rounds += 1
                if rnd.verdict is not Verdict.UNKNOWN:
                    rows.append(_round_row(src, rnd, machine.state))
                if change is not None:
                    combined = combine(machines[g.id].state for g in group)
                    await self._record(s, component, combined, change.at, change.cause, report)
            if rows:
                await s.execute(insert(SourceRound).values(rows).on_conflict_do_nothing())
            for src in group:
                values = {
                    "org_id": src.org_id,
                    "state": machines[src.id].state.value,
                    "machine": machines[src.id].to_json(),
                    "processed_until": until[src.id],
                    "updated_at": now,
                }
                await s.execute(
                    insert(SourceState)
                    .values(source_id=src.id, **values)
                    .on_conflict_do_update(index_elements=["source_id"], set_=values)
                )

    async def _forget(self, component_id: uuid.UUID, now: datetime, report: StatusReport) -> None:
        async with self._db.system("status engine: component without sources") as s:
            component = await s.get(Component, component_id)
            if component is not None:
                await self._record(s, component, State.UNKNOWN, now, None, report)

    async def _record(
        self,
        s: AsyncSession,
        component: Component,
        state: State,
        at: datetime,
        cause: str | None,
        report: StatusReport,
    ) -> None:
        """Move the component's timeline to ``state`` at ``at`` (clamped, see module doc)."""
        current = await s.scalar(
            select(ComponentState).where(
                ComponentState.component_id == component.id, ComponentState.ended_at.is_(None)
            )
        )
        old = State(current.state) if current else State.UNKNOWN
        if old is state:
            return
        report.changes += 1
        if current is None:
            s.add(_interval(component, state, at, cause))
        else:
            at = max(at, current.started_at)
            if at > current.started_at:
                current.ended_at = at
                await s.flush()  # the open interval is unique per component
                s.add(_interval(component, state, at, cause))
            else:
                await self._replace(s, current, state, cause)
        await s.flush()
        await self._incidents(s, component, old, state, at, cause, report)

    async def _replace(
        self, s: AsyncSession, current: ComponentState, state: State, cause: str | None
    ) -> None:
        """The open interval changes state at its very start: rewrite it or merge it back."""
        previous = await s.scalar(
            select(ComponentState)
            .where(
                ComponentState.component_id == current.component_id,
                ComponentState.ended_at == current.started_at,
            )
            .order_by(ComponentState.started_at.desc())
            .limit(1)
        )
        if previous is not None and previous.state == state:
            await s.delete(current)
            await s.flush()
            previous.ended_at = None
        else:
            current.state = state.value
            current.cause = cause

    async def _incidents(
        self,
        s: AsyncSession,
        component: Component,
        old: State,
        new: State,
        at: datetime,
        cause: str | None,
        report: StatusReport,
    ) -> None:
        if new is State.DOWN and old is not State.DOWN:
            if await _in_maintenance(s, component, at):
                return
            last = await s.scalar(
                select(Incident)
                .where(Incident.kind == IncidentKind.AUTO, Incident.component_id == component.id)
                .order_by(Incident.started_at.desc())
                .limit(1)
            )
            if last is not None and last.resolved_at is None:
                return
            if last is not None and last.resolved_at and at - last.resolved_at <= FLAP_MERGE:
                last.resolved_at = None
                last.status = IncidentStatus.OPEN
                report.reopened += 1
                return
            s.add(
                Incident(
                    org_id=component.org_id,
                    kind=IncidentKind.AUTO,
                    status=IncidentStatus.OPEN,
                    component_id=component.id,
                    cause=cause,
                    started_at=at,
                )
            )
            report.opened += 1
        elif old is State.DOWN and new is not State.DOWN:
            incident = await s.scalar(
                select(Incident).where(
                    Incident.kind == IncidentKind.AUTO,
                    Incident.component_id == component.id,
                    Incident.resolved_at.is_(None),
                )
            )
            if incident is not None:
                incident.resolved_at = max(at, incident.started_at)
                incident.status = IncidentStatus.RESOLVED
                report.resolved += 1


async def _samples(
    s: AsyncSession, source_id: uuid.UUID, start: datetime, end: datetime
) -> Sequence[Sample]:
    rows = await s.execute(
        select(
            Observation.node_id,
            Observation.observed_at,
            Observation.ok,
            Observation.error_class,
            Observation.latency_ms,
            Observation.node_healthy,
        )
        .where(
            Observation.source_id == source_id,
            Observation.observed_at >= start,
            Observation.observed_at < end,
        )
        .order_by(Observation.observed_at)
    )
    return [
        Sample(str(node), observed_at, ok, error_class, latency, healthy)
        for node, observed_at, ok, error_class, latency, healthy in rows
    ]


async def _in_maintenance(s: AsyncSession, component: Component, at: datetime) -> bool:
    """Windows of the component's owner: platform windows for shared components."""
    found = await s.scalar(
        select(MaintenanceWindow.id)
        .where(
            MaintenanceWindow.component_id == component.id,
            MaintenanceWindow.org_id.is_not_distinct_from(component.org_id),
            MaintenanceWindow.starts_at <= at,
            MaintenanceWindow.ends_at > at,
        )
        .limit(1)
    )
    return found is not None


def _interval(
    component: Component, state: State, at: datetime, cause: str | None
) -> ComponentState:
    return ComponentState(
        org_id=component.org_id,
        component_id=component.id,
        state=state.value,
        started_at=at,
        cause=cause,
    )


def _round_row(source: Source, rnd: Round, state: State) -> dict[str, Any]:
    return {
        "org_id": source.org_id,
        "source_id": source.id,
        "start": rnd.start,
        "interval_s": rnd.interval_s,
        "verdict": rnd.verdict.value,
        "state": state.value,
        "ok_nodes": rnd.ok_nodes,
        "fail_nodes": rnd.fail_nodes,
        "p50_ms": rnd.p50_ms,
        "p95_ms": rnd.p95_ms,
        "error_class": rnd.error_class,
    }
