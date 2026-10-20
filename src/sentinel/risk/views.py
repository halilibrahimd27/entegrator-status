"""Read model: open risks per component, soonest first (undated last)."""

from __future__ import annotations

import uuid
from collections.abc import Iterable

from sqlalchemy import select

from sentinel.db.session import Database
from sentinel.models import Dependency, Risk
from sentinel.risk.drift import Drift, drift_of, latest_releases
from sentinel.tenancy.roles import Action
from sentinel.tenancy.scope import TenantScope


class RiskViews:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def open(
        self, scope: TenantScope, component_ids: Iterable[uuid.UUID]
    ) -> dict[uuid.UUID, list[Risk]]:
        scope.require(Action.GRAPH_VIEW)
        ids = list(dict.fromkeys(component_ids))
        found: dict[uuid.UUID, list[Risk]] = {c: [] for c in ids}
        if not ids:
            return found
        async with self._db.tenant(scope) as s:
            rows = (
                await s.scalars(
                    select(Risk)
                    .where(Risk.component_id.in_(ids), Risk.resolved_at.is_(None))
                    .order_by(Risk.due_at.asc().nulls_last(), Risk.first_seen_at)
                )
            ).all()
        for risk in rows:
            found[risk.component_id].append(risk)
        return found

    async def drift(
        self, scope: TenantScope, dependencies: Iterable[Dependency]
    ) -> dict[uuid.UUID, Drift]:
        """Pinned vs. latest per dependency that has both."""
        scope.require(Action.GRAPH_VIEW)
        deps = list(dependencies)
        async with self._db.tenant(scope) as s:
            latest = await latest_releases(s, (d.to_component_id for d in deps))
        found = {}
        for dependency in deps:
            drift = drift_of(dependency, latest.get(dependency.to_component_id))
            if drift is not None:
                found[dependency.id] = drift
        return found
