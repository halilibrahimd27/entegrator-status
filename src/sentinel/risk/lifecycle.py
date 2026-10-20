"""Advisory and end-of-life risks for the versions organizations actually use.

Targets of a source: the version or cycle set on the source itself (a risk of the
source's owner) and every product dependency pinned on its component (a risk of the
dependency's owner). Subjects keep them apart: ``<advisory or cycle>|<source or
dependency id>``.

- **Advisories** (OSV): a pinned version is affected when it is listed explicitly or
  falls in a range (``introduced`` ≤ v < ``fixed``, or ≤ ``last_affected``).
- **End of life** (endoflife.date): the pinned version's cycle is the longest cycle
  name that prefixes it (``3.12.4`` → ``3.12``); a risk opens ``WARN_DAYS`` before its
  end of life, or at once if it has ended.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import distinct_on
from sqlalchemy.ext.asyncio import AsyncSession

from sentinel.db.base import utcnow
from sentinel.db.session import Database
from sentinel.models import Dependency, Risk, RiskKind, Snapshot, Source, SourceKind
from sentinel.risk.drift import parse_pin
from sentinel.sources.versions import Version, compare, parse

WARN_DAYS = 180
_ZERO = Version("0", "semver", (0,))


def affected(item: dict[str, Any], version: Version) -> bool:
    for listed in item.get("versions") or []:
        known = parse(str(listed))
        if known is not None and compare(known, version) == 0:
            return True
    start: Version | None = None
    for event in item.get("events") or []:
        ((key, value),) = event.items()
        if key == "introduced":
            start = _ZERO if value == "0" else parse(value)
            continue
        end = parse(value)
        if start is not None and end is not None and compare(version, start) >= 0:
            inside = compare(version, end) < 0 if key == "fixed" else compare(version, end) <= 0
            if inside:
                return True
        start = None
    return start is not None and compare(version, start) >= 0


def cycle_of(version: Version, cycles: list[dict[str, Any]]) -> dict[str, Any] | None:
    best: tuple[int, dict[str, Any]] | None = None
    for cycle in cycles:
        parts = str(cycle.get("cycle", "")).split(".")
        if not all(p.isdigit() for p in parts):
            continue
        numbers = tuple(int(p) for p in parts)
        if version.release[: len(numbers)] == numbers and (best is None or len(numbers) > best[0]):
            best = (len(numbers), cycle)
    return best[1] if best else None


@dataclass(frozen=True)
class Target:
    org_id: uuid.UUID | None
    key: str  # "source" or a dependency id
    value: str  # a version or a cycle name


@dataclass
class LifecycleReport:
    opened: int = 0
    resolved: int = 0


@dataclass
class _Wanted:
    org_id: uuid.UUID | None
    component_id: uuid.UUID
    due_at: datetime | None
    link: str | None
    detail: dict[str, Any] = field(default_factory=dict)


async def _latest(s: AsyncSession, kind: SourceKind) -> list[tuple[Source, list[dict[str, Any]]]]:
    rows = await s.execute(
        select(Source, Snapshot.items)
        .join(Snapshot, Snapshot.source_id == Source.id)
        .ext(distinct_on(Snapshot.source_id))
        .where(Source.kind == kind.value, Source.enabled.is_(True))
        .order_by(Snapshot.source_id, Snapshot.taken_at.desc())
    )
    return [(source, items) for source, items in rows]


async def _pins(
    s: AsyncSession, component_ids: Iterable[uuid.UUID]
) -> dict[uuid.UUID, list[Target]]:
    ids = list(set(component_ids))
    found: dict[uuid.UUID, list[Target]] = {c: [] for c in ids}
    if not ids:
        return found
    for dependency in (
        await s.scalars(
            select(Dependency).where(
                Dependency.to_component_id.in_(ids),
                Dependency.from_product_id.is_not(None),
                Dependency.usage.has_key("version"),
            )
        )
    ).all():
        raw = str((dependency.usage or {}).get("version", ""))
        found[dependency.to_component_id].append(Target(dependency.org_id, str(dependency.id), raw))
    return found


def _midnight(value: str) -> datetime:
    return datetime.combine(date.fromisoformat(value), time(), tzinfo=UTC)


async def run_lifecycle(db: Database, clock: Callable[[], datetime] = utcnow) -> LifecycleReport:
    now = clock()
    report = LifecycleReport()
    async with db.system("lifecycle: advisories and end of life for pinned versions") as s:
        wanted: dict[tuple[uuid.UUID, str, str], _Wanted] = {}
        advisories = await _latest(s, SourceKind.ADVISORY)
        eols = await _latest(s, SourceKind.EOL)
        pins = await _pins(s, (src.component_id for src, _ in [*advisories, *eols]))
        for source, items in advisories:
            targets = list(pins[source.component_id])
            if source.config.get("version"):
                targets.append(Target(source.org_id, "source", str(source.config["version"])))
            for target in targets:
                version = parse_pin(target.value)
                if version is None:
                    continue
                for item in items:
                    if item.get("type") == "none" or not affected(item, version):
                        continue
                    wanted[(source.id, RiskKind.ADVISORY.value, f"{item['id']}|{target.key}")] = (
                        _Wanted(
                            target.org_id,
                            source.component_id,
                            None,
                            item.get("link"),
                            {
                                "id": item["id"],
                                "title": item.get("title"),
                                "severity": item.get("severity"),
                                "aliases": item.get("aliases", []),
                                "version": version.raw,
                            },
                        )
                    )
        for source, items in eols:
            targets = [
                Target(t.org_id, t.key, str(c["cycle"]))
                for t in pins[source.component_id]
                if (v := parse_pin(t.value)) is not None and (c := cycle_of(v, items))
            ]
            if source.config.get("cycle"):
                targets.append(Target(source.org_id, "source", str(source.config["cycle"])))
            by_cycle = {str(c.get("cycle")): c for c in items}
            for target in targets:
                cycle = by_cycle.get(target.value)
                if cycle is None or cycle.get("eol") in (None, False):
                    continue
                ended = cycle["eol"] is True
                due = None if ended else _midnight(str(cycle["eol"]))
                if due is not None and due - now > timedelta(days=WARN_DAYS):
                    continue
                wanted[(source.id, RiskKind.EOL.value, f"{target.value}|{target.key}")] = _Wanted(
                    target.org_id,
                    source.component_id,
                    due,
                    cycle.get("link"),
                    {"cycle": target.value, "latest": cycle.get("latest"), "ended": ended},
                )
        sources = [src.id for src, _ in [*advisories, *eols]]
        open_risks = {
            (risk.source_id, risk.kind, risk.subject): risk
            for risk in (
                await s.scalars(
                    select(Risk).where(
                        Risk.source_id.in_(sources),
                        Risk.kind.in_([RiskKind.ADVISORY.value, RiskKind.EOL.value]),
                        Risk.resolved_at.is_(None),
                    )
                )
            ).all()
        }
        for (source_id, kind, subject), want in wanted.items():
            current = open_risks.get((source_id, kind, subject))
            if current is not None:
                current.detail, current.due_at, current.link = want.detail, want.due_at, want.link
                current.last_seen_at = now
                continue
            s.add(
                Risk(
                    org_id=want.org_id,
                    source_id=source_id,
                    component_id=want.component_id,
                    kind=kind,
                    subject=subject,
                    due_at=want.due_at,
                    link=want.link,
                    detail=want.detail,
                    first_seen_at=now,
                    last_seen_at=now,
                )
            )
            report.opened += 1
        for key, risk in open_risks.items():
            if key not in wanted:
                risk.resolved_at = now  # fixed, upgraded, unpinned, or withdrawn
                report.resolved += 1
    return report
