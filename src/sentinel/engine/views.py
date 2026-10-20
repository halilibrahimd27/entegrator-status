"""Read models for pages and the API: current component status, as one organization sees it."""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select

from sentinel.db.base import utcnow
from sentinel.db.session import Database
from sentinel.models import Component, ComponentState, Incident, IncidentKind, MaintenanceWindow
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


class StatusViews:
    def __init__(self, db: Database, clock: Callable[[], datetime] = utcnow) -> None:
        self._db = db
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
