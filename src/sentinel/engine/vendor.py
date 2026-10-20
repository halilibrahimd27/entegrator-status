"""Status page snapshots → vendor status, vendor incidents and announced maintenance.

- ``VendorStatus`` keeps what the vendor declares now (page indicator, mapped component
  statuses). It is shown next to our consensus and never feeds it.
- A new vendor incident or maintenance becomes a ``Change`` on each of our components
  it affects (through ``component_map``), or on the status page's own component.
- A vendor maintenance becomes a ``MaintenanceWindow`` only if it was announced before
  it started: first seen with a start time in the future. Maintenance declared after
  the fact stays an announcement, so a vendor cannot erase an outage from uptime.
  Rescheduling moves the window while it has not started.
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from sentinel.models import (
    Change,
    ChangeCategory,
    ChangeKind,
    Component,
    MaintenanceWindow,
    Snapshot,
    Source,
    VendorStatus,
)

CLOSED = frozenset({"resolved", "postmortem", "completed"})
INDICATORS = frozenset({"none", "minor", "major", "critical", "maintenance"})
COMPONENT_STATES = frozenset(
    {"operational", "degraded", "partial_outage", "major_outage", "maintenance", "unknown"}
)


def _when(raw: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(str(raw)) if raw else None
    except ValueError:
        return None


async def _mapped(s: AsyncSession, source: Source) -> dict[str, uuid.UUID]:
    """Vendor component name → our component id, within the status page's service."""
    mapping = {str(k): str(v) for k, v in dict(source.config.get("component_map") or {}).items()}
    if not mapping:
        return {}
    own = await s.get(Component, source.component_id)
    if own is None:  # pragma: no cover - cascades remove the source with it
        return {}
    rows = await s.execute(
        select(Component.slug, Component.id).where(
            Component.service_id == own.service_id, Component.slug.in_(set(mapping.values()))
        )
    )
    by_slug: dict[str, uuid.UUID] = dict(rows.all())
    return {name: by_slug[slug] for name, slug in mapping.items() if slug in by_slug}


async def apply_status_page(
    s: AsyncSession, source: Source, snapshot: Snapshot, previous: Snapshot | None
) -> int:
    mapped = await _mapped(s, source)
    items = snapshot.items
    page = next((i for i in items if i.get("type") == "page"), {})
    statuses = {
        str(mapped[i["name"]]): status
        if (status := str(i.get("status"))) in COMPONENT_STATES
        else "unknown"
        for i in items
        if i.get("type") == "component" and i.get("name") in mapped
    }
    indicator = str(page.get("indicator", "none"))
    open_incidents = sum(
        1 for i in items if i.get("type") == "incident" and i.get("status") not in CLOSED
    )
    values = {
        "org_id": snapshot.org_id,
        "indicator": indicator if indicator in INDICATORS else "minor",
        "components": statuses,
        "open_incidents": open_incidents,
        "observed_at": snapshot.taken_at,
    }
    await s.execute(
        insert(VendorStatus)
        .values(source_id=source.id, **values)
        .on_conflict_do_update(
            index_elements=["source_id"],
            set_=values,
            where=VendorStatus.observed_at <= snapshot.taken_at,
        )
    )
    for item in items:
        if item.get("type") == "maintenance":
            await _window(s, source, snapshot, item, mapped)
    if previous is None:
        return 0  # baseline: what was already declared is not news
    seen = {str(i.get("id")) for i in previous.items}
    rows = []
    for item in items:
        kind = item.get("type")
        if kind not in ("incident", "maintenance") or str(item.get("id")) in seen:
            continue
        maintenance = kind == "maintenance"
        for component_id in _targets(source, item, mapped):
            rows.append(
                {
                    "org_id": snapshot.org_id,
                    "source_id": source.id,
                    "component_id": component_id,
                    "kind": (
                        ChangeKind.VENDOR_MAINTENANCE if maintenance else ChangeKind.VENDOR_INCIDENT
                    ).value,
                    "category": (
                        ChangeCategory.MAINTENANCE if maintenance else ChangeCategory.INCIDENT
                    ).value,
                    "title": str(item.get("title") or "")[:300] or "—",
                    "link": item.get("link"),
                    "published_at": _when(item.get("started") or item.get("published")),
                    "effective_at": _when(item.get("starts")),
                    "detected_at": snapshot.taken_at,
                    "fingerprint": hashlib.sha256(
                        f"{item.get('id')}|{component_id}".encode()
                    ).hexdigest(),
                }
            )
    if not rows:
        return 0
    result = await s.execute(
        insert(Change).values(rows).on_conflict_do_nothing().returning(Change.id)
    )
    return len(result.all())


def _targets(source: Source, item: dict[str, Any], mapped: dict[str, uuid.UUID]) -> list[uuid.UUID]:
    found = [mapped[name] for name in item.get("components") or [] if name in mapped]
    return list(dict.fromkeys(found)) or [source.component_id]


async def _window(
    s: AsyncSession,
    source: Source,
    snapshot: Snapshot,
    item: dict[str, Any],
    mapped: dict[str, uuid.UUID],
) -> None:
    starts, ends = _when(item.get("starts")), _when(item.get("ends"))
    if starts is None or ends is None or ends <= starts or item.get("status") in CLOSED:
        return
    external = str(item.get("id"))[:120]
    for component_id in _targets(source, item, mapped):
        window = await s.scalar(
            select(MaintenanceWindow).where(
                MaintenanceWindow.component_id == component_id,
                MaintenanceWindow.external_id == external,
            )
        )
        if window is not None:
            not_started = window.starts_at > snapshot.taken_at
            if not_started and starts > snapshot.taken_at:
                window.starts_at, window.ends_at = starts, ends  # rescheduled in time
            continue
        if starts <= snapshot.taken_at:
            continue  # first seen after it started: an announcement, not a window
        s.add(
            MaintenanceWindow(
                org_id=snapshot.org_id,
                component_id=component_id,
                title=str(item.get("title") or "")[:200] or "—",
                starts_at=starts,
                ends_at=ends,
                origin="announcement",
                external_id=external,
            )
        )
