"""Read models for pages and the API: current component status, as one organization sees it."""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from sqlalchemy import func, select

from sentinel.db.base import utcnow
from sentinel.db.session import Database
from sentinel.engine.consensus import SEVERITY, State
from sentinel.engine.uptime import WINDOWS, Span, Tally, day_bounds, tally
from sentinel.graph.service import GraphService, Node
from sentinel.models import (
    Component,
    ComponentDaily,
    ComponentState,
    Incident,
    IncidentKind,
    MaintenanceWindow,
    Product,
    SourceHourly,
)
from sentinel.tenancy.roles import Action
from sentinel.tenancy.scope import TenantScope

MAINTENANCE = "maintenance"


@dataclass(frozen=True)
class ComponentStatus:
    state: str  # a consensus state, or "maintenance" while the owner's window runs
    since: datetime | None
    cause: str | None
    incident: Incident | None
    maintenance: MaintenanceWindow | None


UNKNOWN = ComponentStatus("unknown", None, None, None, None)
BAR_DAYS = 90


@dataclass(frozen=True)
class DayBar:
    day: date
    uptime: float | None
    worst: str


@dataclass(frozen=True)
class OverviewRow:
    node: Node
    status: ComponentStatus
    uptime: dict[str, float | None]
    products: list[str] = field(default_factory=list)


def _rank(state: str) -> int:
    if state == "maintenance":
        return 1
    try:
        return SEVERITY[State(state)]
    except ValueError:  # pragma: no cover - only known states are stored
        return 0


class StatusViews:
    def __init__(
        self,
        db: Database,
        graph: GraphService | None = None,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self._db = db
        self._graph = graph or GraphService(db)
        self._clock = clock

    async def current(
        self, scope: TenantScope, component_ids: Iterable[uuid.UUID]
    ) -> dict[uuid.UUID, ComponentStatus]:
        scope.require(Action.GRAPH_VIEW)
        ids = list(dict.fromkeys(component_ids))
        if not ids:
            return {}
        now = self._clock()
        async with self._db.tenant(scope) as s:
            owners = {
                c.id: c.org_id
                for c in (await s.scalars(select(Component).where(Component.id.in_(ids)))).all()
            }
            states = {
                row.component_id: row
                for row in (
                    await s.scalars(
                        select(ComponentState).where(
                            ComponentState.component_id.in_(ids), ComponentState.ended_at.is_(None)
                        )
                    )
                ).all()
            }
            incidents = {
                row.component_id: row
                for row in (
                    await s.scalars(
                        select(Incident).where(
                            Incident.kind == IncidentKind.AUTO,
                            Incident.component_id.in_(ids),
                            Incident.resolved_at.is_(None),
                        )
                    )
                ).all()
            }
            windows: dict[uuid.UUID, MaintenanceWindow] = {}
            for window in (
                await s.scalars(
                    select(MaintenanceWindow).where(
                        MaintenanceWindow.component_id.in_(ids),
                        MaintenanceWindow.starts_at <= now,
                        MaintenanceWindow.ends_at > now,
                    )
                )
            ).all():
                if window.org_id == owners.get(window.component_id, window.org_id):
                    windows.setdefault(window.component_id, window)
        found: dict[uuid.UUID, ComponentStatus] = {}
        for component_id in ids:
            if component_id not in owners:
                continue  # not visible to this organization
            row = states.get(component_id)
            active = windows.get(component_id)
            found[component_id] = ComponentStatus(
                state=MAINTENANCE if active else (row.state if row else "unknown"),
                since=active.starts_at if active else (row.started_at if row else None),
                cause=row.cause if row else None,
                incident=incidents.get(component_id),
                maintenance=active,
            )
        return found

    async def _timelines(
        self, scope: TenantScope, ids: list[uuid.UUID], since: datetime
    ) -> tuple[dict[uuid.UUID, list[Span]], dict[uuid.UUID, list[tuple[datetime, datetime]]]]:
        spans: dict[uuid.UUID, list[Span]] = {c: [] for c in ids}
        windows: dict[uuid.UUID, list[tuple[datetime, datetime]]] = {c: [] for c in ids}
        async with self._db.tenant(scope) as s:
            owners = {
                c.id: c.org_id
                for c in (await s.scalars(select(Component).where(Component.id.in_(ids)))).all()
            }
            for row in (
                await s.scalars(
                    select(ComponentState).where(
                        ComponentState.component_id.in_(ids),
                        (ComponentState.ended_at.is_(None)) | (ComponentState.ended_at > since),
                    )
                )
            ).all():
                spans[row.component_id].append(Span(row.state, row.started_at, row.ended_at))
            for window in (
                await s.scalars(
                    select(MaintenanceWindow).where(
                        MaintenanceWindow.component_id.in_(ids), MaintenanceWindow.ends_at > since
                    )
                )
            ).all():
                if window.org_id == owners.get(window.component_id, window.org_id):
                    windows[window.component_id].append((window.starts_at, window.ends_at))
        return spans, windows

    async def uptime(
        self, scope: TenantScope, component_ids: Iterable[uuid.UUID]
    ) -> dict[uuid.UUID, dict[str, float | None]]:
        """Uptime ratio per rolling window (24h, 7d, 30d, 90d), from the timeline only."""
        scope.require(Action.GRAPH_VIEW)
        ids = list(dict.fromkeys(component_ids))
        if not ids:
            return {}
        now = self._clock()
        spans, windows = await self._timelines(scope, ids, now - max(WINDOWS.values()))
        return {
            c: {
                key: tally(spans[c], windows[c], now - length, now, now).uptime
                for key, length in WINDOWS.items()
            }
            for c in ids
        }

    async def bars(
        self, scope: TenantScope, component_ids: Iterable[uuid.UUID], days: int = BAR_DAYS
    ) -> dict[uuid.UUID, list[DayBar]]:
        """One bar per UTC day, oldest first; today is computed live."""
        scope.require(Action.GRAPH_VIEW)
        ids = list(dict.fromkeys(component_ids))
        if not ids:
            return {}
        now = self._clock()
        today = now.date()
        first = today - timedelta(days=days - 1)
        stored: dict[tuple[uuid.UUID, date], Tally] = {}
        async with self._db.tenant(scope) as s:
            for row in (
                await s.scalars(
                    select(ComponentDaily).where(
                        ComponentDaily.component_id.in_(ids), ComponentDaily.day >= first
                    )
                )
            ).all():
                t = Tally()
                for state in t.seconds:
                    t.seconds[state] = float(getattr(row, f"{state}_s"))
                stored[(row.component_id, row.day)] = t
        start, _ = day_bounds(today)
        spans, windows = await self._timelines(scope, ids, start)
        result: dict[uuid.UUID, list[DayBar]] = {}
        for c in ids:
            bars = []
            for offset in range(days):
                day = first + timedelta(days=offset)
                if day == today:
                    lo, hi = day_bounds(day)
                    t = tally(spans[c], windows[c], lo, hi, now)
                else:
                    t = stored.get((c, day), Tally())
                bars.append(DayBar(day, t.uptime, t.worst))
            result[c] = bars
        return result

    async def latency(
        self, scope: TenantScope, source_ids: Iterable[uuid.UUID]
    ) -> dict[uuid.UUID, SourceHourly]:
        """The latest finished hour of each source, if any."""
        scope.require(Action.GRAPH_VIEW)
        ids = list(dict.fromkeys(source_ids))
        if not ids:
            return {}
        async with self._db.tenant(scope) as s:
            latest = (
                select(SourceHourly.source_id, func.max(SourceHourly.hour).label("hour"))
                .where(SourceHourly.source_id.in_(ids))
                .group_by(SourceHourly.source_id)
                .subquery()
            )
            rows = (
                await s.scalars(
                    select(SourceHourly).join(
                        latest,
                        (latest.c.source_id == SourceHourly.source_id)
                        & (latest.c.hour == SourceHourly.hour),
                    )
                )
            ).all()
            return {row.source_id: row for row in rows}

    async def overview(self, scope: TenantScope) -> list[OverviewRow]:
        """Every component the organization's products depend on (≤ 3 hops), worst first."""
        scope.require(Action.GRAPH_VIEW)
        async with self._db.tenant(scope) as s:
            products = (await s.execute(select(Product.id, Product.name))).all()
        nodes: dict[uuid.UUID, Node] = {}
        used_by: dict[uuid.UUID, set[str]] = {}
        for product_id, name in products:
            graph = await self._graph.downstream(scope, product_id)
            for node in graph.nodes.values():
                if node.component_id not in nodes or node.depth < nodes[node.component_id].depth:
                    nodes[node.component_id] = node
                used_by.setdefault(node.component_id, set()).add(name)
        statuses = await self.current(scope, nodes)
        uptimes = await self.uptime(scope, nodes)
        rows = [
            OverviewRow(
                node, statuses.get(cid, UNKNOWN), uptimes.get(cid, {}), sorted(used_by[cid])
            )
            for cid, node in nodes.items()
        ]
        rows.sort(
            key=lambda r: (-_rank(r.status.state), r.node.depth, r.node.service, r.node.component)
        )
        return rows
