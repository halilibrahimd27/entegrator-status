"""Pinned vs. latest: how far each product's dependency lags its newest stable release.

The pinned version comes from the dependency's ``usage.version`` (imports set it from
lockfiles and SBOMs; people type it in). The latest comes from the newest snapshot of a
``release`` source on the target component: the registry's own "latest" when it says so
(npm dist-tags), otherwise the newest stable version. A dependency a major version
behind is an ``unadopted_major`` risk of the organization that owns the dependency, with
the dependency id as its subject; it resolves when the product catches up.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import distinct_on
from sqlalchemy.ext.asyncio import AsyncSession

from sentinel.db.base import utcnow
from sentinel.db.session import Database
from sentinel.models import Dependency, Risk, RiskKind, Snapshot, Source, SourceKind
from sentinel.sources.versions import ReleaseClass, Version, behind, newest, parse

_OPERATORS = re.compile(r"^[\s^~>=<!vV]+")


@dataclass(frozen=True)
class Latest:
    source_id: uuid.UUID
    version: Version
    link: str | None


@dataclass(frozen=True)
class Drift:
    pinned: str
    latest: str
    lag: ReleaseClass | None  # None: current
    link: str | None


def parse_pin(raw: Any) -> Version | None:
    """``^1.2.0``, ``~=2.1``, ``>=3,<4`` → the first concrete version."""
    if not isinstance(raw, str):
        return None
    first = re.split(r"[,\s|]+", _OPERATORS.sub("", raw.strip()), maxsplit=1)[0]
    return parse(first)


def latest_of(items: list[dict[str, Any]]) -> tuple[Version, str | None] | None:
    for item in items:
        version = parse(str(item.get("version", ""))) if item.get("latest") else None
        if version is not None and not version.is_prerelease:
            return version, item.get("link")
    parsed = [(v, i) for i in items if (v := parse(str(i.get("version", ""))))]
    best = newest([v for v, _ in parsed])
    if best is None:
        return None
    link = next((i.get("link") for v, i in parsed if v is best), None)
    return best, link


async def latest_releases(
    s: AsyncSession, component_ids: Iterable[uuid.UUID]
) -> dict[uuid.UUID, Latest]:
    ids = list(set(component_ids))
    if not ids:
        return {}
    rows = await s.execute(
        select(Source.component_id, Snapshot.source_id, Snapshot.items)
        .join(Source, Source.id == Snapshot.source_id)
        .ext(distinct_on(Snapshot.source_id))
        .where(
            Source.component_id.in_(ids),
            Source.kind == SourceKind.RELEASE.value,
            Source.enabled.is_(True),
        )
        .order_by(Snapshot.source_id, Snapshot.taken_at.desc())
    )
    found: dict[uuid.UUID, Latest] = {}
    for component_id, source_id, items in rows:
        latest = latest_of(items)
        if latest is not None and component_id not in found:
            found[component_id] = Latest(source_id, latest[0], latest[1])
    return found


def drift_of(dependency: Dependency, latest: Latest | None) -> Drift | None:
    pinned = parse_pin((dependency.usage or {}).get("version"))
    if pinned is None or latest is None:
        return None
    return Drift(pinned.raw, latest.version.raw, behind(pinned, latest.version), latest.link)


@dataclass
class DriftReport:
    opened: int = 0
    resolved: int = 0


async def run_drift(db: Database, clock: Callable[[], datetime] = utcnow) -> DriftReport:
    now = clock()
    report = DriftReport()
    async with db.system("drift: pinned versions of every organization's products") as s:
        dependencies = (
            await s.scalars(
                select(Dependency).where(
                    Dependency.from_product_id.is_not(None), Dependency.usage.has_key("version")
                )
            )
        ).all()
        latest = await latest_releases(s, (d.to_component_id for d in dependencies))
        open_risks = {
            risk.subject: risk
            for risk in (
                await s.scalars(
                    select(Risk).where(
                        Risk.kind == RiskKind.UNADOPTED_MAJOR, Risk.resolved_at.is_(None)
                    )
                )
            ).all()
        }
        behind_major = set()
        for dependency in dependencies:
            release = latest.get(dependency.to_component_id)
            drift = drift_of(dependency, release)
            if drift is None or drift.lag != "major" or release is None:
                continue
            subject = str(dependency.id)
            behind_major.add(subject)
            detail = {
                "pinned": drift.pinned,
                "latest": drift.latest,
                "product_id": str(dependency.from_product_id),
            }
            current = open_risks.get(subject)
            if current is not None:
                current.detail, current.link, current.last_seen_at = detail, drift.link, now
                continue
            s.add(
                Risk(
                    org_id=dependency.org_id,
                    source_id=release.source_id,
                    component_id=dependency.to_component_id,
                    kind=RiskKind.UNADOPTED_MAJOR,
                    subject=subject,
                    link=drift.link,
                    detail=detail,
                    first_seen_at=now,
                    last_seen_at=now,
                )
            )
            report.opened += 1
        for subject, risk in open_risks.items():
            if subject not in behind_major:
                risk.resolved_at = now  # caught up, unpinned, or the dependency is gone
                report.resolved += 1
    return report
