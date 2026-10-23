"""The organization dashboard: "is anything upstream about to hurt us?" on one screen.

Each product gets a derived state, explained by what caused it:

- ``own_incident``: the organization opened an incident on the product itself;
- ``affected``: an upstream component the product uses directly is down;
- ``at_risk``: something two or three hops away is down, or a high-severity change or
  risk reaches the product;
- ``ok``: nothing of the above.

Below that: open upstream incidents, recent changes, risks due soon and planned changes,
each line once however many products it reaches.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import select

from sentinel.db.base import utcnow
from sentinel.models import Incident, IncidentKind, Product
from sentinel.risk.correlation import CorrelationViews, HintView
from sentinel.risk.impact import LEVELS, Impact, ImpactViews
from sentinel.risk.register import CalendarItem, RegisterEntry, RiskRegister
from sentinel.tenancy.scope import TenantScope

RECENT = timedelta(days=7)
AHEAD = timedelta(days=30)
STATES = ("own_incident", "affected", "at_risk", "ok")  # worst first


@dataclass(frozen=True)
class ProductLine:
    id: uuid.UUID
    name: str
    state: str
    reason: Impact | None  # what set the state (None for ok / own incident)
    high: int
    medium: int


@dataclass
class Dashboard:
    choices: list[tuple[uuid.UUID, str]] = field(default_factory=list)  # for the filter
    products: list[ProductLine] = field(default_factory=list)
    incidents: list[Impact] = field(default_factory=list)
    changes: list[Impact] = field(default_factory=list)
    due: list[RegisterEntry] = field(default_factory=list)
    planned: list[CalendarItem] = field(default_factory=list)
    hints: list[HintView] = field(default_factory=list)

    @property
    def calm(self) -> bool:
        return not (self.incidents or self.changes or self.due or self.planned) and all(
            p.state == "ok" for p in self.products
        )


def derive(impacts: list[Impact], own_incident: bool) -> tuple[str, Impact | None]:
    if own_incident:
        return "own_incident", None
    down = [i for i in impacts if i.kind == "incident"]
    direct = [i for i in down if i.depth == 1]
    if direct:
        return "affected", direct[0]
    if down:
        return "at_risk", down[0]
    serious = [i for i in impacts if i.severity == "high"]
    if serious:
        return "at_risk", serious[0]
    return "ok", None


class DashboardViews:
    def __init__(
        self,
        impact: ImpactViews,
        register: RiskRegister,
        correlation: CorrelationViews,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self._impact = impact
        self._register = register
        self._correlation = correlation
        self._clock = clock

    async def build(
        self,
        scope: TenantScope,
        *,
        product_id: uuid.UUID | None = None,
        min_severity: str = "low",
    ) -> Dashboard:
        now = self._clock()
        floor = LEVELS.index(min_severity) if min_severity in LEVELS else 0
        by_product = await self._impact.for_org(scope)
        async with self._impact.db.tenant(scope) as s:
            names = dict((await s.execute(select(Product.id, Product.name))).all())
            own = set(
                (
                    await s.scalars(
                        select(Incident.product_id).where(
                            Incident.kind == IncidentKind.MANUAL,
                            Incident.resolved_at.is_(None),
                            Incident.product_id.is_not(None),
                        )
                    )
                ).all()
            )
        board = Dashboard(choices=sorted(names.items(), key=lambda item: item[1]))
        chosen = [p for p in by_product if product_id is None or p == product_id]
        seen: set[tuple[str, uuid.UUID]] = set()
        for pid in chosen:
            impacts = by_product[pid]
            state, reason = derive(impacts, pid in own)
            board.products.append(
                ProductLine(
                    pid,
                    names.get(pid, ""),
                    state,
                    reason,
                    sum(1 for i in impacts if i.severity == "high"),
                    sum(1 for i in impacts if i.severity == "medium"),
                )
            )
            for impact in impacts:  # sorted worst first, so the first reach of a subject wins
                if impact.level < floor or (impact.kind, impact.subject_id) in seen:
                    continue
                if impact.kind == "incident":
                    board.incidents.append(impact)
                elif impact.kind == "change" and impact.when and impact.when >= now - RECENT:
                    board.changes.append(impact)
                else:
                    continue
                seen.add((impact.kind, impact.subject_id))
        board.products.sort(key=lambda p: (STATES.index(p.state), -p.high, p.name))
        board.incidents.sort(key=lambda i: (-i.level, i.when or now))
        board.changes.sort(key=lambda i: (-i.level, -(i.when or now).timestamp()))
        wanted = {names[p] for p in chosen if p in names}
        board.due = [
            e
            for e in await self._register.entries(scope)
            if e.state == "open"
            and e.risk.due_at is not None
            and e.risk.due_at <= now + AHEAD
            and LEVELS.index(e.severity) >= floor
            and (product_id is None or wanted & set(e.products))
        ]
        board.planned = [i for i in await self._register.calendar(scope) if i.when <= now + AHEAD]
        board.hints = await self._correlation.hints(scope)
        return board
