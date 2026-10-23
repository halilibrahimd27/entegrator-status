"""Diff new snapshots of change sources into classified changes.

The first snapshot of a source is its baseline: what a feed already listed when we
started watching is not news. After that every item id not seen in the previous
snapshot becomes one change, classified from its title. A change is stored once per
source and item (unique fingerprint), so an item that drops out and comes back is
not reported twice.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from sentinel.db.base import utcnow
from sentinel.db.session import Database
from sentinel.engine.contracts import apply_contract
from sentinel.engine.releases import apply_releases
from sentinel.engine.vendor import apply_status_page
from sentinel.models import Change, ChangeKind, Snapshot, Source, SourceKind
from sentinel.sources.classify import classify, effective_date

BATCH = 200
ITEM_KINDS = frozenset({SourceKind.FEED, SourceKind.PAGE_WATCH})


@dataclass
class ChangeReport:
    snapshots: int = 0
    changes: int = 0


def fingerprint(item_id: str) -> str:
    return hashlib.sha256(item_id.encode()).hexdigest()


def _published(item: dict[str, Any]) -> datetime | None:
    raw = item.get("published")
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw))
    except ValueError:
        return None


async def run_changes(db: Database, clock: Callable[[], datetime] = utcnow) -> ChangeReport:
    report = ChangeReport()
    async with db.system("changes: diff new snapshots of every change source") as s:
        pending = (
            await s.scalars(
                select(Snapshot)
                .where(Snapshot.processed_at.is_(None))
                .order_by(Snapshot.taken_at)
                .limit(BATCH)
            )
        ).all()
        for snapshot in pending:
            report.changes += await _diff(s, snapshot)
            snapshot.processed_at = clock()
            report.snapshots += 1
    return report


async def _diff(s: AsyncSession, snapshot: Snapshot) -> int:
    source = await s.get(Source, snapshot.source_id)
    if source is None:  # pragma: no cover - cascades remove snapshots with their source
        return 0
    previous = await s.scalar(
        select(Snapshot)
        .where(Snapshot.source_id == snapshot.source_id, Snapshot.taken_at < snapshot.taken_at)
        .order_by(Snapshot.taken_at.desc())
        .limit(1)
    )
    if source.kind == SourceKind.STATUS_PAGE:
        return await apply_status_page(s, source, snapshot, previous)
    if source.kind == SourceKind.RELEASE:
        return await apply_releases(s, source, snapshot, previous)
    if source.kind == SourceKind.API_CONTRACT:
        return await apply_contract(s, source, snapshot, previous)
    if source.kind not in ITEM_KINDS or previous is None:
        return 0  # previous None: the baseline
    seen = {str(item.get("id")) for item in previous.items}
    rows = []
    for item in snapshot.items:
        item_id = str(item.get("id", ""))
        title = str(item.get("title") or "")
        if not item_id or not title or item_id in seen:
            continue
        rows.append(
            {
                "org_id": snapshot.org_id,
                "source_id": source.id,
                "component_id": source.component_id,
                "kind": ChangeKind.ANNOUNCEMENT.value,
                "category": classify(title).value,
                "title": title[:300],
                "link": item.get("link"),
                "published_at": _published(item),
                "effective_at": effective_date(title),
                "detected_at": snapshot.taken_at,
                "fingerprint": fingerprint(item_id),
            }
        )
    if not rows:
        return 0
    result = await s.execute(
        insert(Change).values(rows).on_conflict_do_nothing().returning(Change.id)
    )
    return len(result.all())
