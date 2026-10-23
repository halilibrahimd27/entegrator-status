"""Platform administration: organizations and quotas, the platform audit log, notices
on shared services.

Only platform admins (TOTP enforced) reach this; every change is recorded in the
platform audit log (``org_id`` NULL). Shared catalog entries themselves are changed only
through reviewed YAML packs and accepted proposals (docs/CATALOG.md): one source of
truth, so there is deliberately no catalog editor here.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from sentinel import audit
from sentinel.db.base import utcnow
from sentinel.db.session import Database
from sentinel.engine.notices import NoticeError, check_announcement, check_window
from sentinel.models import (
    AuditEvent,
    Change,
    ChangeKind,
    Component,
    MaintenanceWindow,
    Membership,
    Organization,
    Product,
    Service,
    Source,
    User,
)

MAX_QUOTA = 100_000
AUDIT_PAGE = 200
SERVICE_PAGE = 100


class PlatformError(Exception):
    def __init__(self, key: str) -> None:
        super().__init__(key)
        self.key = key


@dataclass(frozen=True)
class OrgRow:
    id: uuid.UUID
    slug: str
    name: str
    created_at: datetime
    deleted_at: datetime | None
    members: int
    products: int
    sources: int
    max_sources: int | None  # the org's own limit, None: the default
    limit: int


@dataclass(frozen=True)
class PlatformAuditRow:
    occurred_at: datetime
    action: str
    actor: str | None
    target_type: str | None
    target_id: str | None
    details: dict[str, Any]


@dataclass(frozen=True)
class SharedService:
    service: Service
    components: list[Component]
    windows: list[MaintenanceWindow]
    announcements: list[Change]


class PlatformService:
    def __init__(
        self,
        db: Database,
        *,
        default_max_sources: int,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self._db = db
        self._default = default_max_sources
        self._clock = clock

    # -- organizations -------------------------------------------------------------------

    async def orgs(self) -> list[OrgRow]:
        async with self._db.system("platform admin: organization list with usage") as s:
            members = dict(
                (
                    await s.execute(
                        select(Membership.org_id, func.count()).group_by(Membership.org_id)
                    )
                ).all()
            )
            products = dict(
                (
                    await s.execute(select(Product.org_id, func.count()).group_by(Product.org_id))
                ).all()
            )
            sources = dict(
                (
                    await s.execute(
                        select(Source.org_id, func.count())
                        .where(Source.org_id.is_not(None))
                        .group_by(Source.org_id)
                    )
                ).all()
            )
            orgs = (await s.scalars(select(Organization).order_by(Organization.slug))).all()
        return [
            OrgRow(
                o.id,
                o.slug,
                o.name,
                o.created_at,
                o.deleted_at,
                members.get(o.id, 0),
                products.get(o.id, 0),
                sources.get(o.id, 0),
                o.max_sources,
                o.max_sources or self._default,
            )
            for o in orgs
        ]

    async def set_quota(
        self, admin_id: uuid.UUID, org_id: uuid.UUID, raw: str, *, ip: str = ""
    ) -> None:
        """``raw`` empty: back to the default (ORG_MAX_SOURCES)."""
        value: int | None = None
        if raw.strip():
            if not raw.strip().isdigit() or not 1 <= int(raw) <= MAX_QUOTA:
                raise PlatformError("platform.invalid_quota")
            value = int(raw)
        async with self._db.system("platform admin: set an organization's quota") as s:
            org = await s.get(Organization, org_id)
            if org is None:
                raise PlatformError("not_found")
            org.max_sources = value
            audit.record(
                s,
                "platform.quota_set",
                actor_user_id=admin_id,
                target_type="organization",
                target_id=org_id,
                details={"slug": org.slug, "max_sources": value},
                ip=ip,
            )

    # -- the platform audit log ----------------------------------------------------------

    async def audit_log(self, limit: int = AUDIT_PAGE) -> list[PlatformAuditRow]:
        async with self._db.system("platform admin: platform audit log") as s:
            rows = (
                await s.execute(
                    select(AuditEvent, User.email)
                    .outerjoin(User, User.id == AuditEvent.actor_user_id)
                    .where(AuditEvent.org_id.is_(None))
                    .order_by(AuditEvent.occurred_at.desc())
                    .limit(limit)
                )
            ).all()
        return [
            PlatformAuditRow(
                e.occurred_at, e.action, email, e.target_type, e.target_id, dict(e.details or {})
            )
            for e, email in rows
        ]

    # -- notices on shared services ------------------------------------------------------

    async def shared_services(self, query: str = "") -> list[Service]:
        async with self._db.system("platform admin: shared services") as s:
            statement = select(Service).where(Service.org_id.is_(None))
            if query.strip():
                statement = statement.where(Service.name.ilike(f"%{query.strip()[:80]}%"))
            return list(
                (await s.scalars(statement.order_by(Service.name).limit(SERVICE_PAGE))).all()
            )

    async def shared_service(self, service_id: uuid.UUID) -> SharedService | None:
        now = self._clock()
        async with self._db.system("platform admin: one shared service") as s:
            service = await s.get(Service, service_id)
            if service is None or service.org_id is not None:
                return None
            components = list(
                (
                    await s.scalars(
                        select(Component)
                        .where(Component.service_id == service_id, Component.org_id.is_(None))
                        .order_by(Component.name)
                    )
                ).all()
            )
            ids = [c.id for c in components]
            windows = list(
                (
                    await s.scalars(
                        select(MaintenanceWindow)
                        .where(
                            MaintenanceWindow.component_id.in_(ids),
                            MaintenanceWindow.org_id.is_(None),
                            MaintenanceWindow.ends_at > now,
                        )
                        .order_by(MaintenanceWindow.starts_at)
                    )
                ).all()
            )
            announcements = list(
                (
                    await s.scalars(
                        select(Change)
                        .where(
                            Change.component_id.in_(ids),
                            Change.org_id.is_(None),
                            Change.origin == "manual",
                        )
                        .order_by(Change.detected_at.desc())
                        .limit(50)
                    )
                ).all()
            )
        return SharedService(service, components, windows, announcements)

    async def _shared_component(self, s: AsyncSession, component_id: uuid.UUID) -> Component:
        component = await s.get(Component, component_id)
        if component is None or component.org_id is not None:
            raise PlatformError("not_found")
        return component

    async def add_window(
        self,
        admin_id: uuid.UUID,
        component_id: uuid.UUID,
        *,
        title: str,
        starts_at: datetime,
        ends_at: datetime,
        ip: str = "",
    ) -> None:
        """Platform maintenance on a shared component: it counts for every organization."""
        try:
            title = check_window(title, starts_at, ends_at, self._clock())
        except NoticeError as exc:
            raise PlatformError(exc.key) from None
        async with self._db.system("platform admin: maintenance on a shared component") as s:
            await self._shared_component(s, component_id)
            window = MaintenanceWindow(
                org_id=None,
                component_id=component_id,
                title=title,
                starts_at=starts_at,
                ends_at=ends_at,
                created_by=admin_id,
            )
            s.add(window)
            await s.flush()
            audit.record(
                s,
                "platform.maintenance_added",
                actor_user_id=admin_id,
                target_type="maintenance_window",
                target_id=window.id,
                ip=ip,
            )

    async def end_window(self, admin_id: uuid.UUID, window_id: uuid.UUID, *, ip: str = "") -> None:
        now = self._clock()
        async with self._db.system("platform admin: end shared maintenance") as s:
            window = await s.get(MaintenanceWindow, window_id)
            if window is None or window.org_id is not None:
                raise PlatformError("not_found")
            if window.ends_at <= now:
                raise PlatformError("status.window_over")
            if window.starts_at >= now:
                await s.delete(window)
            else:
                window.ends_at = now
            audit.record(
                s,
                "platform.maintenance_ended",
                actor_user_id=admin_id,
                target_type="maintenance_window",
                target_id=window_id,
                ip=ip,
            )

    async def add_announcement(
        self,
        admin_id: uuid.UUID,
        service_id: uuid.UUID,
        *,
        title: str,
        category: str,
        component_ids: list[uuid.UUID],
        link: str = "",
        effective_at: datetime | None = None,
        ip: str = "",
    ) -> None:
        """A shared announcement: every organization depending on these components sees it."""
        try:
            title, kind, url = check_announcement(title, category, link, component_ids)
        except NoticeError as exc:
            raise PlatformError(exc.key) from None
        now = self._clock()
        async with self._db.system("platform admin: announcement on a shared service") as s:
            components = (
                await s.scalars(
                    select(Component).where(
                        Component.id.in_(component_ids),
                        Component.service_id == service_id,
                        Component.org_id.is_(None),
                    )
                )
            ).all()
            if len(components) != len(set(component_ids)):
                raise PlatformError("not_found")
            s.add_all(
                [
                    Change(
                        org_id=None,
                        component_id=component.id,
                        kind=ChangeKind.ANNOUNCEMENT,
                        category=kind,
                        title=title,
                        link=url,
                        effective_at=effective_at,
                        detected_at=now,
                        fingerprint=uuid.uuid4().hex * 2,
                        origin="manual",
                        created_by=admin_id,
                    )
                    for component in components
                ]
            )
            audit.record(
                s,
                "platform.announcement_added",
                actor_user_id=admin_id,
                target_type="service",
                target_id=service_id,
                details={"category": kind.value, "components": len(components)},
                ip=ip,
            )

    async def delete_announcement(
        self, admin_id: uuid.UUID, change_id: uuid.UUID, *, ip: str = ""
    ) -> uuid.UUID:
        """Remove a shared manual announcement; returns its service id."""
        async with self._db.system("platform admin: delete a shared announcement") as s:
            change = await s.get(Change, change_id)
            if change is None or change.org_id is not None or change.origin != "manual":
                raise PlatformError("not_found")
            component = await self._shared_component(s, change.component_id)
            await s.delete(change)
            audit.record(
                s,
                "platform.announcement_deleted",
                actor_user_id=admin_id,
                target_type="change",
                target_id=change_id,
                ip=ip,
            )
            return component.service_id
