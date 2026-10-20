"""What people write: manual incidents on products, maintenance windows, announcements.

Manual incidents belong to an organization's own product. Maintenance windows belong
to the organization's own components; windows on shared components are platform-level
(Phase 5 creates them from vendor announcements). A window must be declared before it
starts (5 minutes of slack) and cannot be moved into the past afterwards, because
maintenance is left out of uptime.

Manual announcements cover vendors that publish news where automation is not allowed
(robots.txt) or not possible; they are private to the organization that enters them.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import case, select

from sentinel import audit
from sentinel.db.base import utcnow
from sentinel.db.session import Database
from sentinel.models import (
    Change,
    ChangeCategory,
    ChangeKind,
    Component,
    Incident,
    IncidentKind,
    IncidentStatus,
    IncidentUpdate,
    MaintenanceWindow,
    Product,
)
from sentinel.models.changes import ANNOUNCEMENT_CATEGORIES
from sentinel.models.status import MANUAL_STATUSES
from sentinel.sources.feeds import safe_link
from sentinel.tenancy.roles import Action
from sentinel.tenancy.scope import TenantScope

MAX_WINDOW = timedelta(days=14)
WINDOW_SLACK = timedelta(minutes=5)
MAX_TITLE = 200
MAX_BODY = 4000
RECENT_INCIDENTS = 20


class NoticeError(Exception):
    def __init__(self, key: str) -> None:
        super().__init__(key)
        self.key = key


@dataclass(frozen=True)
class IncidentView:
    incident: Incident
    updates: list[IncidentUpdate]


def _text(raw: str, limit: int, key: str) -> str:
    value = " ".join(raw.split()) if limit <= MAX_TITLE else raw.strip()
    if not value or len(value) > limit:
        raise NoticeError(key)
    return value


def _manual_status(raw: str) -> IncidentStatus:
    try:
        status = IncidentStatus(raw)
    except ValueError:
        raise NoticeError("status.invalid_status") from None
    if status not in MANUAL_STATUSES:
        raise NoticeError("status.invalid_status")
    return status


class NoticeService:
    def __init__(self, db: Database, clock: Callable[[], datetime] = utcnow) -> None:
        self._db = db
        self._clock = clock

    # --- manual incidents -------------------------------------------------------------

    async def product_incidents(
        self, scope: TenantScope, product_id: uuid.UUID
    ) -> list[IncidentView]:
        scope.require(Action.GRAPH_VIEW)
        async with self._db.tenant(scope) as s:
            open_first = case((Incident.resolved_at.is_(None), 0), else_=1)
            incidents = (
                await s.scalars(
                    select(Incident)
                    .where(Incident.product_id == product_id)
                    .order_by(open_first, Incident.started_at.desc())
                    .limit(RECENT_INCIDENTS)
                )
            ).all()
            updates: dict[uuid.UUID, list[IncidentUpdate]] = {i.id: [] for i in incidents}
            for update in (
                await s.scalars(
                    select(IncidentUpdate)
                    .where(IncidentUpdate.incident_id.in_(list(updates)))
                    .order_by(IncidentUpdate.created_at.desc())
                )
            ).all():
                updates[update.incident_id].append(update)
            return [IncidentView(i, updates[i.id]) for i in incidents]

    async def open_incident(
        self,
        scope: TenantScope,
        product_id: uuid.UUID,
        *,
        title: str,
        body: str,
        status: str,
        ip: str = "",
    ) -> Incident:
        scope.require(Action.STATUS_MANAGE)
        title = _text(title, MAX_TITLE, "status.invalid_title")
        body = _text(body, MAX_BODY, "status.invalid_body")
        state = _manual_status(status)
        now = self._clock()
        async with self._db.tenant(scope) as s:
            if await s.get(Product, product_id) is None:
                raise NoticeError("not_found")
            incident = Incident(
                org_id=scope.org_id,
                kind=IncidentKind.MANUAL,
                status=state,
                product_id=product_id,
                title=title,
                started_at=now,
                resolved_at=now if state is IncidentStatus.RESOLVED else None,
                created_by=scope.user_id,
            )
            s.add(incident)
            await s.flush()
            s.add(
                IncidentUpdate(
                    org_id=scope.org_id,
                    incident_id=incident.id,
                    status=state,
                    body=body,
                    author_id=scope.user_id,
                )
            )
            audit.record(
                s,
                "incident.opened",
                org_id=scope.org_id,
                actor_user_id=scope.user_id,
                target_type="incident",
                target_id=incident.id,
                ip=ip,
            )
            return incident

    async def update_incident(
        self, scope: TenantScope, incident_id: uuid.UUID, *, status: str, body: str, ip: str = ""
    ) -> Incident:
        scope.require(Action.STATUS_MANAGE)
        body = _text(body, MAX_BODY, "status.invalid_body")
        state = _manual_status(status)
        async with self._db.tenant(scope) as s:
            incident = await s.get(Incident, incident_id)
            # shared (engine) incidents are readable here but never editable
            if incident is None or incident.kind != IncidentKind.MANUAL:
                raise NoticeError("not_found")
            incident.status = state
            if state is IncidentStatus.RESOLVED:
                incident.resolved_at = incident.resolved_at or self._clock()
            else:
                incident.resolved_at = None
            s.add(
                IncidentUpdate(
                    org_id=scope.org_id,
                    incident_id=incident.id,
                    status=state,
                    body=body,
                    author_id=scope.user_id,
                )
            )
            audit.record(
                s,
                "incident.updated",
                org_id=scope.org_id,
                actor_user_id=scope.user_id,
                target_type="incident",
                target_id=incident.id,
                details={"status": state.value},
                ip=ip,
            )
            return incident

    # --- maintenance windows ----------------------------------------------------------

    async def add_window(
        self,
        scope: TenantScope,
        component_id: uuid.UUID,
        *,
        title: str,
        starts_at: datetime,
        ends_at: datetime,
        ip: str = "",
    ) -> MaintenanceWindow:
        scope.require(Action.STATUS_MANAGE)
        title = _text(title, MAX_TITLE, "status.invalid_title")
        if ends_at <= starts_at:
            raise NoticeError("status.window_order")
        if ends_at - starts_at > MAX_WINDOW:
            raise NoticeError("status.window_too_long")
        if starts_at < self._clock() - WINDOW_SLACK:
            raise NoticeError("status.window_in_past")
        async with self._db.tenant(scope) as s:
            component = await s.get(Component, component_id)
            if component is None:
                raise NoticeError("not_found")
            if component.org_id is None:
                raise NoticeError("status.shared_component")
            window = MaintenanceWindow(
                org_id=scope.org_id,
                component_id=component_id,
                title=title,
                starts_at=starts_at,
                ends_at=ends_at,
                created_by=scope.user_id,
            )
            s.add(window)
            await s.flush()
            audit.record(
                s,
                "maintenance.added",
                org_id=scope.org_id,
                actor_user_id=scope.user_id,
                target_type="maintenance_window",
                target_id=window.id,
                ip=ip,
            )
            return window

    async def end_window(self, scope: TenantScope, window_id: uuid.UUID, *, ip: str = "") -> None:
        """Cancel a planned window, or end a running one now. Past windows are history."""
        scope.require(Action.STATUS_MANAGE)
        now = self._clock()
        async with self._db.tenant(scope) as s:
            window = await s.get(MaintenanceWindow, window_id)
            if window is None:
                raise NoticeError("not_found")
            if window.org_id is None:
                raise NoticeError("status.shared_component")
            if window.ends_at <= now:
                raise NoticeError("status.window_over")
            if window.starts_at >= now:
                await s.delete(window)
                action = "maintenance.cancelled"
            else:
                window.ends_at = now
                action = "maintenance.ended"
            audit.record(
                s,
                action,
                org_id=scope.org_id,
                actor_user_id=scope.user_id,
                target_type="maintenance_window",
                target_id=window_id,
                ip=ip,
            )

    async def component_windows(
        self, scope: TenantScope, component_ids: list[uuid.UUID]
    ) -> dict[uuid.UUID, list[MaintenanceWindow]]:
        """Upcoming and running windows that apply for this organization."""
        scope.require(Action.GRAPH_VIEW)
        found: dict[uuid.UUID, list[MaintenanceWindow]] = {c: [] for c in component_ids}
        async with self._db.tenant(scope) as s:
            components = {
                c.id: c
                for c in (
                    await s.scalars(select(Component).where(Component.id.in_(component_ids)))
                ).all()
            }
            for window in (
                await s.scalars(
                    select(MaintenanceWindow)
                    .where(
                        MaintenanceWindow.component_id.in_(component_ids),
                        MaintenanceWindow.ends_at > self._clock(),
                    )
                    .order_by(MaintenanceWindow.starts_at)
                )
            ).all():
                component = components.get(window.component_id)
                # only the owner's windows count: platform ones for shared components
                if component is not None and window.org_id == component.org_id:
                    found[window.component_id].append(window)
        return found

    # --- manual announcements ---------------------------------------------------------

    async def add_announcement(
        self,
        scope: TenantScope,
        service_id: uuid.UUID,
        *,
        title: str,
        category: str,
        component_ids: list[uuid.UUID],
        link: str = "",
        published_at: datetime | None = None,
        effective_at: datetime | None = None,
        ip: str = "",
    ) -> list[Change]:
        scope.require(Action.STATUS_MANAGE)
        title = _text(title, MAX_TITLE + 100, "status.invalid_title")
        try:
            kind = ChangeCategory(category)
        except ValueError:
            raise NoticeError("changes.invalid_category") from None
        if kind not in ANNOUNCEMENT_CATEGORIES:
            raise NoticeError("changes.invalid_category")  # release classes come from versions
        url = link.strip() or None
        if url is not None and safe_link(url, url) != url:
            raise NoticeError("changes.invalid_link")  # absolute http(s) only
        if not component_ids:
            raise NoticeError("changes.no_components")
        now = self._clock()
        async with self._db.tenant(scope) as s:
            components = (
                await s.scalars(
                    select(Component).where(
                        Component.id.in_(component_ids), Component.service_id == service_id
                    )
                )
            ).all()
            if len(components) != len(set(component_ids)):
                raise NoticeError("not_found")
            changes = [
                Change(
                    org_id=scope.org_id,
                    component_id=component.id,
                    kind=ChangeKind.ANNOUNCEMENT,
                    category=kind,
                    title=title,
                    link=url,
                    published_at=published_at,
                    effective_at=effective_at,
                    detected_at=now,
                    fingerprint=uuid.uuid4().hex * 2,
                    origin="manual",
                    created_by=scope.user_id,
                )
                for component in components
            ]
            s.add_all(changes)
            await s.flush()
            audit.record(
                s,
                "announcement.added",
                org_id=scope.org_id,
                actor_user_id=scope.user_id,
                target_type="service",
                target_id=service_id,
                details={"components": len(changes), "category": kind.value},
                ip=ip,
            )
            return changes

    async def delete_announcement(
        self, scope: TenantScope, change_id: uuid.UUID, *, ip: str = ""
    ) -> uuid.UUID:
        """Remove a manual announcement; returns its component id."""
        scope.require(Action.STATUS_MANAGE)
        async with self._db.tenant(scope) as s:
            change = await s.get(Change, change_id)
            if change is None or change.origin != "manual" or change.org_id is None:
                raise NoticeError("not_found")
            component_id = change.component_id
            await s.delete(change)
            audit.record(
                s,
                "announcement.deleted",
                org_id=scope.org_id,
                actor_user_id=scope.user_id,
                target_type="change",
                target_id=change_id,
                ip=ip,
            )
            return component_id
