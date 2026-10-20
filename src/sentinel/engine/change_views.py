"""Read models for change sources: recent changes and how each watcher is doing."""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, or_, select
from sqlalchemy.dialects.postgresql import array

from sentinel.db.session import Database
from sentinel.models import Change, Source, SourceWatch, VendorStatus
from sentinel.tenancy.roles import Action
from sentinel.tenancy.scope import TenantScope

RECENT = 10


@dataclass(frozen=True)
class VendorClaim:
    """What the vendor's status page says about one of our components."""

    status: str | None  # the mapped vendor component's status, if mapped
    indicator: str  # the whole page
    open_incidents: int
    observed_at: datetime


class ChangeViews:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def recent(
        self, scope: TenantScope, component_ids: Iterable[uuid.UUID], limit: int = RECENT
    ) -> dict[uuid.UUID, list[Change]]:
        """The latest changes per component, newest first."""
        scope.require(Action.GRAPH_VIEW)
        ids = list(dict.fromkeys(component_ids))
        found: dict[uuid.UUID, list[Change]] = {c: [] for c in ids}
        if not ids:
            return found
        rank = (
            func.row_number()
            .over(partition_by=Change.component_id, order_by=Change.detected_at.desc())
            .label("rank")
        )
        async with self._db.tenant(scope) as s:
            ranked = select(Change.id, rank).where(Change.component_id.in_(ids)).subquery()
            rows = (
                await s.scalars(
                    select(Change)
                    .join(ranked, ranked.c.id == Change.id)
                    .where(ranked.c.rank <= limit)
                    .order_by(Change.detected_at.desc())
                )
            ).all()
        for change in rows:
            found[change.component_id].append(change)
        return found

    async def watch(
        self, scope: TenantScope, source_ids: Iterable[uuid.UUID]
    ) -> dict[uuid.UUID, SourceWatch]:
        scope.require(Action.GRAPH_VIEW)
        ids = list(dict.fromkeys(source_ids))
        if not ids:
            return {}
        async with self._db.tenant(scope) as s:
            rows = (
                await s.scalars(select(SourceWatch).where(SourceWatch.source_id.in_(ids)))
            ).all()
        return {row.source_id: row for row in rows}

    async def vendor(
        self, scope: TenantScope, component_ids: Iterable[uuid.UUID]
    ) -> dict[uuid.UUID, VendorClaim]:
        """Vendor declarations for components: mapped ones, and the status page's own."""
        scope.require(Action.GRAPH_VIEW)
        ids = list(dict.fromkeys(component_ids))
        if not ids:
            return {}
        keys = [str(c) for c in ids]
        async with self._db.tenant(scope) as s:
            rows = (
                await s.execute(
                    select(VendorStatus, Source.component_id)
                    .join(Source, Source.id == VendorStatus.source_id)
                    .where(
                        or_(
                            Source.component_id.in_(ids),
                            VendorStatus.components.has_any(array(keys)),
                        )
                    )
                    .order_by(VendorStatus.observed_at.desc())
                )
            ).all()
        found: dict[uuid.UUID, VendorClaim] = {}
        for vendor, own_component in rows:
            for component_id in ids:
                if component_id in found:
                    continue
                status = vendor.components.get(str(component_id))
                if status is not None or component_id == own_component:
                    found[component_id] = VendorClaim(
                        status, vendor.indicator, vendor.open_incidents, vendor.observed_at
                    )
        return found
