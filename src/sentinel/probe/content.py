"""Core side of change sources: who fetches them, and what their results leave behind.

Availability is measured by every node (consensus needs several). A change source is
fetched by exactly one node, chosen by rendezvous hashing over the nodes seen in the
last ten minutes, so a provider sees one request per interval however many nodes run.

On ingest a result updates the source's watch state (``SourceWatch``) and, when its
snapshot differs from the last one stored, adds a ``Snapshot`` for the engine to diff.
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Iterable
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import distinct_on
from sqlalchemy.ext.asyncio import AsyncSession

from sentinel.models import Attention, Snapshot, SourceKind, SourceWatch

if TYPE_CHECKING:
    from sentinel.probe.nodes import IngestResult, SnapshotIn

CONTENT_KINDS = frozenset(
    {
        SourceKind.FEED,
        SourceKind.STATUS_PAGE,
        SourceKind.PAGE_WATCH,
        SourceKind.RELEASE,
        SourceKind.ADVISORY,
        SourceKind.EOL,
    }
)
NODE_FRESH = timedelta(minutes=10)
FAILING_AFTER = 5
_NEEDS_ATTENTION = frozenset({"no_items", "parse_error"})


def owner_of(job_key: str, nodes: Iterable[uuid.UUID]) -> uuid.UUID | None:
    """The node that fetches ``job_key``: stable while the node set is stable."""
    return max(
        nodes,
        key=lambda node: hashlib.sha256(f"{job_key}:{node}".encode()).digest(),
        default=None,
    )


async def latest_validators(
    s: AsyncSession, source_ids: list[uuid.UUID]
) -> dict[uuid.UUID, dict[str, str]]:
    if not source_ids:
        return {}
    rows = await s.execute(
        select(Snapshot.source_id, Snapshot.validators)
        .where(Snapshot.source_id.in_(source_ids))
        .ext(distinct_on(Snapshot.source_id))
        .order_by(Snapshot.source_id, Snapshot.taken_at.desc())
    )
    return {source_id: dict(validators or {}) for source_id, validators in rows}


async def record(
    s: AsyncSession,
    results: list[IngestResult],
    sources: dict[uuid.UUID, tuple[uuid.UUID | None, str]],
    now: datetime,
) -> None:
    for result in sorted(results, key=lambda r: r.observed_at):
        for source_id in result.source_ids:
            org_id, kind = sources.get(source_id, (None, ""))
            if kind not in CONTENT_KINDS:
                continue
            watch = await s.get(SourceWatch, source_id)
            if watch is None:
                watch = SourceWatch(
                    org_id=org_id,
                    source_id=source_id,
                    last_checked_at=result.observed_at,
                    failures=0,
                )
                s.add(watch)
            elif result.observed_at < watch.last_checked_at:
                continue  # a late, older result does not overwrite a newer state
            watch.last_checked_at = result.observed_at
            _attention(watch, result)
            if result.snapshot is not None:
                await _snapshot(s, watch, org_id, result.observed_at, result.snapshot)


def _attention(watch: SourceWatch, result: IngestResult) -> None:
    if result.ok:
        watch.attention, watch.reason, watch.failures = None, None, 0
    elif result.error_class == "blocked_by_robots":
        watch.attention = Attention.BLOCKED_BY_ROBOTS
        watch.reason = str(result.details.get("robots", "disallowed"))[:200]
    elif result.error_class in _NEEDS_ATTENTION:
        watch.attention, watch.reason = Attention.NEEDS_ATTENTION, result.error_class
    else:
        watch.failures += 1
        if watch.failures >= FAILING_AFTER:
            watch.attention, watch.reason = Attention.FAILING, result.error_class


async def _snapshot(
    s: AsyncSession,
    watch: SourceWatch,
    org_id: uuid.UUID | None,
    observed_at: datetime,
    snapshot: SnapshotIn,
) -> None:
    latest = await s.scalar(
        select(Snapshot)
        .where(Snapshot.source_id == watch.source_id)
        .order_by(Snapshot.taken_at.desc())
        .limit(1)
    )
    if latest is not None and latest.hash == snapshot.hash:
        latest.validators = snapshot.validators  # same content, fresher validators
        return
    if latest is not None and latest.taken_at >= observed_at:
        return
    s.add(
        Snapshot(
            org_id=org_id,
            source_id=watch.source_id,
            taken_at=observed_at,
            hash=snapshot.hash,
            items=snapshot.items,
            validators=snapshot.validators,
        )
    )
    watch.last_changed_at = observed_at
