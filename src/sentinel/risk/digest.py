"""The weekly digest of one organization: what happened upstream, what is coming.

- changes of the past week that reach its products (with their impact severity),
- outages of the past week on components it depends on (resolved ones included),
- open risks due in the next 30 days, and the planned changes of the next 30 days.

Rendered as a page now; Phase 9 sends it by e-mail on a weekly schedule.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import select

from sentinel.db.base import utcnow
from sentinel.models import Incident, IncidentKind
from sentinel.risk.impact import Impact, ImpactViews
from sentinel.risk.register import CalendarItem, RegisterEntry, RiskRegister
from sentinel.tenancy.scope import TenantScope

PERIOD = timedelta(days=7)
AHEAD = timedelta(days=30)


@dataclass(frozen=True)
class Outage:
    component: str
    started_at: datetime
    resolved_at: datetime | None
    cause: str | None


@dataclass
class Digest:
    start: datetime
    end: datetime
    changes: list[Impact] = field(default_factory=list)
    outages: list[Outage] = field(default_factory=list)
    due: list[RegisterEntry] = field(default_factory=list)
    planned: list[CalendarItem] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not (self.changes or self.outages or self.due or self.planned)


class DigestViews:
    def __init__(
        self,
        impact: ImpactViews,
        register: RiskRegister,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self._impact = impact
        self._register = register
        self._clock = clock

    async def weekly(self, scope: TenantScope) -> Digest:
        now = self._clock()
        digest = Digest(start=now - PERIOD, end=now)
        by_product = await self._impact.for_org(scope)
        labels: dict[object, str] = {}
        seen: set[object] = set()
        for impacts in by_product.values():
            for impact in impacts:
                labels.setdefault(impact.component_id, impact.path[-1])
                recent = impact.when is not None and impact.when >= digest.start
                if impact.kind == "change" and recent and impact.subject_id not in seen:
                    seen.add(impact.subject_id)  # one line per change, however many products
                    digest.changes.append(impact)
        for product_id in by_product:
            graph = await self._impact.graph.downstream(scope, product_id)
            for node in graph.nodes.values():
                labels.setdefault(node.component_id, f"{node.service} — {node.component}")
        async with self._impact.db.tenant(scope) as s:
            incidents = (
                await s.scalars(
                    select(Incident)
                    .where(
                        Incident.kind == IncidentKind.AUTO,
                        Incident.component_id.in_(list(labels)),
                        Incident.started_at >= digest.start,
                    )
                    .order_by(Incident.started_at)
                )
            ).all()
        digest.outages = [
            Outage(labels[i.component_id], i.started_at, i.resolved_at, i.cause)
            for i in incidents
            if i.component_id is not None
        ]
        digest.due = [
            e
            for e in await self._register.entries(scope)
            if e.state == "open" and e.risk.due_at is not None and e.risk.due_at <= now + AHEAD
        ]
        digest.planned = [i for i in await self._register.calendar(scope) if i.when <= now + AHEAD]
        digest.changes.sort(key=lambda i: (-i.level, i.when or now))
        return digest
