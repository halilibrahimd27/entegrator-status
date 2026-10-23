"""Contract snapshots → one classified change per transition, with a readable diff.

The change's category is its worst difference (breaking > non-breaking > cosmetic). The
detail lists each difference (fact, reason, before/after, operation) and the operations
touched, so impact can later be matched against each product's ``usage.operations``.
The first snapshot is the baseline.
"""

from __future__ import annotations

import hashlib

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from sentinel.contracts.diff import diff, overall, summary
from sentinel.models import Change, ChangeKind, Snapshot, Source


async def apply_contract(
    s: AsyncSession, source: Source, snapshot: Snapshot, previous: Snapshot | None
) -> int:
    if previous is None:
        return 0
    changes = diff(previous.items, snapshot.items)
    category = overall(changes)
    if category is None:
        return 0
    detail = summary(changes)
    title = f"{source.config.get('format', 'api')}: {source.config.get('url', '')}"
    result = await s.execute(
        insert(Change)
        .values(
            org_id=snapshot.org_id,
            source_id=source.id,
            component_id=source.component_id,
            kind=ChangeKind.CONTRACT.value,
            category=category,
            title=title[:300],
            link=None,
            published_at=None,
            effective_at=None,
            detected_at=snapshot.taken_at,
            fingerprint=hashlib.sha256(f"{previous.hash}>{snapshot.hash}".encode()).hexdigest(),
            detail=detail,
        )
        .on_conflict_do_nothing()
        .returning(Change.id)
    )
    return len(result.all())
