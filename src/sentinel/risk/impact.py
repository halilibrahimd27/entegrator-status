"""Impact: what upstream incidents, changes and risks mean for one product, and why.

Computed from the current dependency graph on every read, so a dependency added today
shows the risks that are already open on it. Severity is ``high`` / ``medium`` / ``low``
with a reason code:

    severity = event class * usage match * depth attenuation

- Usage only counts at depth 1, where the product's own ``usage.operations`` describes
  the dependency: a breaking contract change in an operation the product uses is
  ``high``; in operations it does not use, ``low``; without usage data, ``medium``
  ("possibly affected").
- Each hop beyond the first lowers severity by one level (never below ``low``).
- Risks that belong to one dependency (a pinned version) apply only to the product
  that owns that dependency.
"""

from __future__ import annotations

import uuid
from collections import deque
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select

from sentinel.db.base import utcnow
from sentinel.db.session import Database
from sentinel.graph.service import GraphService, Subgraph
from sentinel.models import Change, Incident, IncidentKind, Product, Risk
from sentinel.tenancy.roles import Action
from sentinel.tenancy.scope import TenantScope

LEVELS = ("low", "medium", "high")
CHANGE_WINDOW = timedelta(days=30)
DUE_SOON = timedelta(days=30)
ANNOUNCEMENT = {
    "security": (2, "security_notice"),
    "deprecation": (1, "deprecation_notice"),
    "maintenance": (1, "maintenance_notice"),
    "package_update": (1, "package_update"),
    "incident": (1, "vendor_reported_incident"),
    "other": (0, "announcement"),
}


@dataclass(frozen=True)
class Impact:
    kind: str  # incident | change | risk
    subject_id: uuid.UUID
    component_id: uuid.UUID
    depth: int
    path: tuple[str, ...]  # product, then "Service — Component" per hop
    severity: str
    reason: str
    title: str
    when: datetime | None
    link: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def level(self) -> int:
        return LEVELS.index(self.severity)


def attenuate(level: int, depth: int) -> int:
    return max(0, level - (depth - 1))


def change_rule(change: Change, depth: int, usage: list[str] | None) -> tuple[int, str] | None:
    detail = change.detail or {}
    if change.kind == "contract":
        if change.category == "cosmetic":
            return None
        if change.category != "breaking":
            return 0, "contract_non_breaking"
        if depth == 1 and usage:
            touched = set(detail.get("operations") or [])
            if touched & set(usage):
                return 2, "used_operation_breaking"
            return 0, "unused_operation"
        return 1, "possibly_affected"
    if change.kind == "release":
        if change.category == "prerelease":
            return None
        if change.category == "major" or detail.get("deprecated") or detail.get("breaking_notes"):
            return 1, "major_release"
        return 0, "release"
    if change.kind == "vendor_incident":
        return 1, "vendor_reported_incident"
    if change.kind == "vendor_maintenance":
        return 0, "vendor_maintenance"
    return ANNOUNCEMENT.get(change.category, (0, "announcement"))


def risk_rule(risk: Risk, now: datetime) -> tuple[int, str]:
    if risk.kind == "advisory":
        severe = str((risk.detail or {}).get("severity") or "") in ("HIGH", "CRITICAL")
        return (2 if severe else 1), "advisory"
    if risk.kind == "unadopted_major":
        return 1, "unadopted_major"
    if risk.due_at is None:
        return 1, "deprecated" if risk.kind == "deprecation" else "ended"
    return (2, "due_soon") if risk.due_at - now <= DUE_SOON else (1, "due_later")


def applies(risk: Risk, dependency_ids: set[str]) -> bool:
    """Source-level risks apply to every dependant; pinned-version risks to their owner."""
    subject = risk.subject or ""
    if not subject or subject.endswith("|source"):
        return True
    owner = subject.rsplit("|", 1)[-1]
    return owner in dependency_ids


def paths(graph: Subgraph) -> dict[uuid.UUID, tuple[uuid.UUID, ...]]:
    """Shortest component path from the product to every node of its subgraph."""
    children: dict[uuid.UUID, list[uuid.UUID]] = {}
    for edge in graph.edges:
        children.setdefault(edge.origin, []).append(edge.target)
    found: dict[uuid.UUID, tuple[uuid.UUID, ...]] = {}
    queue: deque[tuple[uuid.UUID, tuple[uuid.UUID, ...]]] = deque([(graph.product_id, ())])
    while queue:
        node, path = queue.popleft()
        for child in children.get(node, []):
            if child not in found:
                found[child] = (*path, child)
                queue.append((child, found[child]))
    return found


class ImpactViews:
    def __init__(
        self, db: Database, graph: GraphService, clock: Callable[[], datetime] = utcnow
    ) -> None:
        self._db = db
        self._graph = graph
        self._clock = clock

    async def for_product(self, scope: TenantScope, product_id: uuid.UUID) -> list[Impact]:
        scope.require(Action.GRAPH_VIEW)
        graph = await self._graph.downstream(scope, product_id)
        routes = paths(graph)
        ids = list(routes)
        if not ids:
            return []
        now = self._clock()
        usage = {
            e.target: list((e.usage or {}).get("operations") or [])
            for e in graph.edges
            if e.origin == product_id
        }
        own_dependencies = {str(e.id) for e in graph.edges if e.origin == product_id}
        async with self._db.tenant(scope) as s:
            product = await s.get(Product, product_id)
            incidents = (
                await s.scalars(
                    select(Incident).where(
                        Incident.kind == IncidentKind.AUTO,
                        Incident.component_id.in_(ids),
                        Incident.resolved_at.is_(None),
                    )
                )
            ).all()
            changes = (
                await s.scalars(
                    select(Change)
                    .where(Change.component_id.in_(ids), Change.detected_at > now - CHANGE_WINDOW)
                    .order_by(Change.detected_at.desc())
                    .limit(300)
                )
            ).all()
            risks = (
                await s.scalars(
                    select(Risk).where(Risk.component_id.in_(ids), Risk.resolved_at.is_(None))
                )
            ).all()
        name = product.name if product else ""

        def path_of(component_id: uuid.UUID) -> tuple[str, ...]:
            hops = routes[component_id]
            return (name, *(f"{graph.nodes[c].service} — {graph.nodes[c].component}" for c in hops))

        found: list[Impact] = []
        for incident in incidents:
            if incident.component_id is None:  # pragma: no cover - auto incidents always have one
                continue
            depth = len(routes[incident.component_id])
            found.append(
                Impact(
                    "incident",
                    incident.id,
                    incident.component_id,
                    depth,
                    path_of(incident.component_id),
                    LEVELS[attenuate(2, depth)],
                    "upstream_down",
                    incident.cause or "",
                    incident.started_at,
                )
            )
        for change in changes:
            depth = len(routes[change.component_id])
            rule = change_rule(change, depth, usage.get(change.component_id))
            if rule is None:
                continue
            level, reason = rule
            found.append(
                Impact(
                    "change",
                    change.id,
                    change.component_id,
                    depth,
                    path_of(change.component_id),
                    LEVELS[attenuate(level, depth)],
                    reason,
                    change.title,
                    change.detected_at,
                    change.link,
                    {"category": change.category, "kind": change.kind},
                )
            )
        for risk in risks:
            if not applies(risk, own_dependencies):
                continue
            depth = len(routes[risk.component_id])
            level, reason = risk_rule(risk, now)
            found.append(
                Impact(
                    "risk",
                    risk.id,
                    risk.component_id,
                    depth,
                    path_of(risk.component_id),
                    LEVELS[attenuate(level, depth)],
                    reason,
                    risk.kind,
                    risk.due_at,
                    risk.link,
                    dict(risk.detail or {}),
                )
            )
        found.sort(key=lambda i: (-i.level, i.depth, -(i.when.timestamp() if i.when else 0)))
        return found

    async def for_org(self, scope: TenantScope) -> dict[uuid.UUID, list[Impact]]:
        async with self._db.tenant(scope) as s:
            products: Iterable[uuid.UUID] = (await s.scalars(select(Product.id))).all()
        return {p: await self.for_product(scope, p) for p in products}
