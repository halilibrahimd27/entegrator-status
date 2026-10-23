"""Correlated failures: many dependants of one upstream going down together.

When at least ``ratio`` (default 40 %) of the components that depend on one upstream
went down within the same ten minutes, the upstream gets a hint: "ortak bir kaynaktan
kaynaklanıyor olabilir". It is worded and shown as a hint, never as a finding, and it
is most useful exactly when the upstream itself is not observed.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select

from sentinel.db.base import utcnow
from sentinel.db.session import Database
from sentinel.models import Component, Dependency, Incident, IncidentKind, Service
from sentinel.tenancy.roles import Action
from sentinel.tenancy.scope import TenantScope

DEFAULT_RATIO = 0.4
WINDOW = timedelta(minutes=10)
MIN_DEPENDANTS = 2


@dataclass(frozen=True)
class Hint:
    upstream: uuid.UUID
    affected: tuple[uuid.UUID, ...]
    dependants: int
    first_down: datetime

    @property
    def ratio(self) -> float:
        return len(self.affected) / self.dependants


def find_hints(
    dependants: Mapping[uuid.UUID, set[uuid.UUID]],
    down_since: Mapping[uuid.UUID, datetime],
    *,
    ratio: float = DEFAULT_RATIO,
    window: timedelta = WINDOW,
) -> list[Hint]:
    """Pure: the largest group of dependants that went down within ``window`` of each other."""
    hints = []
    for upstream, children in dependants.items():
        if len(children) < MIN_DEPENDANTS:
            continue
        down = sorted((down_since[c], c) for c in children if c in down_since)
        best: list[tuple[datetime, uuid.UUID]] = []
        start = 0
        for end in range(len(down)):
            while down[end][0] - down[start][0] > window:
                start += 1
            if end - start + 1 > len(best):
                best = down[start : end + 1]
        if len(best) >= MIN_DEPENDANTS and len(best) / len(children) >= ratio - 1e-9:
            hints.append(Hint(upstream, tuple(c for _, c in best), len(children), best[0][0]))
    hints.sort(key=lambda h: (-h.ratio, h.first_down))
    return hints


@dataclass(frozen=True)
class HintView:
    hint: Hint
    upstream_name: str
    service_id: uuid.UUID
    affected_names: list[str]


class CorrelationViews:
    def __init__(
        self,
        db: Database,
        *,
        ratio: float = DEFAULT_RATIO,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self._db = db
        self._ratio = ratio
        self._clock = clock

    async def hints(self, scope: TenantScope) -> list[HintView]:
        """Hints over what this organization can see: shared edges and its own."""
        scope.require(Action.GRAPH_VIEW)
        async with self._db.tenant(scope) as s:
            edges = (
                await s.execute(
                    select(Dependency.to_component_id, Dependency.from_component_id).where(
                        Dependency.from_component_id.is_not(None)
                    )
                )
            ).all()
            dependants: dict[uuid.UUID, set[uuid.UUID]] = {}
            for upstream, child in edges:
                if child is not None:
                    dependants.setdefault(upstream, set()).add(child)
            open_incidents = (
                await s.execute(
                    select(Incident.component_id, Incident.started_at).where(
                        Incident.kind == IncidentKind.AUTO, Incident.resolved_at.is_(None)
                    )
                )
            ).all()
            down = {c: started for c, started in open_incidents if c is not None}
            found = find_hints(dependants, down, ratio=self._ratio)
            ids = {h.upstream for h in found} | {c for h in found for c in h.affected}
            names = {
                component.id: (f"{service.name} — {component.name}", service.id)
                for component, service in (
                    await s.execute(
                        select(Component, Service)
                        .join(Service, Service.id == Component.service_id)
                        .where(Component.id.in_(ids))
                    )
                ).all()
            }
        return [
            HintView(
                h,
                names[h.upstream][0],
                names[h.upstream][1],
                sorted(names[c][0] for c in h.affected),
            )
            for h in found
            if h.upstream in names
        ]
