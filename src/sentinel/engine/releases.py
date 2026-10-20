"""Release snapshots → one change per new version, classified against what came before.

Each new version is compared with the newest stable version already known that is
older than it: ``2.0.0`` after ``1.9.4`` is ``major``; a backport ``1.9.5`` published
after ``2.0.0`` is a ``patch`` of the 1.9 line, not "other". Release notes that mention
a breaking change are flagged in the change's detail whatever the version says.
The first snapshot is the baseline, as for every change source.
"""

from __future__ import annotations

import hashlib
from datetime import datetime
from functools import cmp_to_key
from typing import Any

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from sentinel.models import Change, ChangeKind, Snapshot, Source
from sentinel.sources.versions import Version, classify, compare, parse


def _when(raw: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(str(raw)) if raw else None
    except ValueError:
        return None


def baseline_for(version: Version, known: list[Version]) -> Version | None:
    older = [v for v in known if not v.is_prerelease and compare(v, version) < 0]
    return max(older, key=cmp_to_key(compare), default=None)


async def apply_releases(
    s: AsyncSession, source: Source, snapshot: Snapshot, previous: Snapshot | None
) -> int:
    if previous is None:
        return 0
    seen = {str(item.get("id")) for item in previous.items}
    known = [v for item in previous.items if (v := parse(str(item.get("version", ""))))]
    fresh = [
        (version, item)
        for item in snapshot.items
        if str(item.get("id")) not in seen and (version := parse(str(item.get("version", ""))))
    ]
    by_version = cmp_to_key(compare)
    fresh.sort(key=lambda pair: by_version(pair[0]))
    rows = []
    name = str(source.config.get("name", ""))
    for version, item in fresh:
        base = baseline_for(version, known)
        title = str(item.get("title") or version.raw)
        if title == version.raw:
            title = f"{name} {version.raw}"
        category = (
            classify(base, version)
            if base
            else ("prerelease" if version.is_prerelease else "other")
        )
        rows.append(
            {
                "org_id": snapshot.org_id,
                "source_id": source.id,
                "component_id": source.component_id,
                "kind": ChangeKind.RELEASE.value,
                "category": category,
                "title": title[:300],
                "link": item.get("link"),
                "published_at": _when(item.get("published")),
                "effective_at": None,
                "detected_at": snapshot.taken_at,
                "fingerprint": hashlib.sha256(str(item["id"]).encode()).hexdigest(),
                "detail": {
                    "version": version.raw,
                    "previous": base.raw if base else None,
                    "breaking_notes": bool(item.get("breaking")),
                    "deprecated": bool(item.get("deprecated")),
                },
            }
        )
        known.append(version)
    if not rows:
        return 0
    result = await s.execute(
        insert(Change).values(rows).on_conflict_do_nothing().returning(Change.id)
    )
    return len(result.all())
