"""Edges and traversal.

An organization's view of the graph is every shared edge plus its own. Adding an edge
is refused when it would close a cycle in that view (checked over the full view, not
only the first three hops). Shared edges are acyclic by catalog validation; traversals
still keep a visited set and a depth limit, because a later catalog import could close
a cycle with some organization's private edges.
"""

from __future__ import annotations

import uuid
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from sentinel import audit
from sentinel.db.session import Database
from sentinel.models import CatalogTagRule, Component, Dependency, Product, Service
from sentinel.tenancy.roles import Action
from sentinel.tenancy.scope import TenantScope

MAX_DEPTH = 3


class GraphError(Exception):
    """``key`` is the i18n key shown to the user."""

    def __init__(self, key: str) -> None:
        super().__init__(key)
        self.key = key


@dataclass(frozen=True)
class Node:
    component_id: uuid.UUID
    component: str
    service: str
    service_id: uuid.UUID
    shared: bool
    depth: int


@dataclass(frozen=True)
class EdgeView:
    id: uuid.UUID
    origin: uuid.UUID  # product id for depth-1 edges, component id otherwise
    target: uuid.UUID
    shared: bool
    usage: dict[str, Any]


@dataclass
class Subgraph:
    product_id: uuid.UUID
    nodes: dict[uuid.UUID, Node] = field(default_factory=dict)
    edges: list[EdgeView] = field(default_factory=list)


@dataclass(frozen=True)
class Dependant:
    """Something affected by a component, and the path that connects them."""

    product_id: uuid.UUID
    path: tuple[
        uuid.UUID, ...
    ]  # component ids from the affected component up to the product's dependency


class GraphService:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def add(
        self,
        scope: TenantScope,
        *,
        to_component_id: uuid.UUID,
        from_product_id: uuid.UUID | None = None,
        from_component_id: uuid.UUID | None = None,
        usage: dict[str, Any] | None = None,
        note: str | None = None,
        ip: str = "",
    ) -> uuid.UUID:
        scope.require(Action.GRAPH_EDIT)
        if (from_product_id is None) == (from_component_id is None):
            raise GraphError("graph.one_origin")
        async with self._db.tenant(scope) as s:
            if await s.get(Component, to_component_id) is None:
                raise GraphError("not_found")
            if from_product_id is not None and await s.get(Product, from_product_id) is None:
                raise GraphError("not_found")
            if from_component_id is not None:
                if await s.get(Component, from_component_id) is None:
                    raise GraphError("not_found")
                if from_component_id == to_component_id or await self._reaches(
                    s, to_component_id, from_component_id
                ):
                    raise GraphError("graph.cycle")
            duplicate = await s.scalar(
                select(Dependency.id).where(
                    Dependency.to_component_id == to_component_id,
                    Dependency.from_product_id.is_(None)
                    if from_product_id is None
                    else Dependency.from_product_id == from_product_id,
                    Dependency.from_component_id.is_(None)
                    if from_component_id is None
                    else Dependency.from_component_id == from_component_id,
                )
            )
            if duplicate is not None:
                raise GraphError("graph.exists")
            edge = Dependency(
                org_id=scope.org_id,
                from_product_id=from_product_id,
                from_component_id=from_component_id,
                to_component_id=to_component_id,
                usage=usage or {},
                note=note,
            )
            s.add(edge)
            await s.flush()
            audit.record(
                s,
                "graph.edge_added",
                org_id=scope.org_id,
                actor_user_id=scope.user_id,
                target_type="dependency",
                target_id=edge.id,
                ip=ip,
            )
            return edge.id

    async def remove(self, scope: TenantScope, dependency_id: uuid.UUID, ip: str = "") -> None:
        scope.require(Action.GRAPH_EDIT)
        async with self._db.tenant(scope) as s:
            edge = await s.get(Dependency, dependency_id)
            if edge is None:
                raise GraphError("not_found")
            if edge.org_id is None:
                raise GraphError("graph.shared_edge")
            await s.delete(edge)
            audit.record(
                s,
                "graph.edge_removed",
                org_id=scope.org_id,
                actor_user_id=scope.user_id,
                target_type="dependency",
                target_id=dependency_id,
                ip=ip,
            )

    async def downstream(
        self, scope: TenantScope, product_id: uuid.UUID, depth: int = MAX_DEPTH
    ) -> Subgraph:
        """What ``product_id`` depends on, up to ``depth`` hops."""
        scope.require(Action.GRAPH_VIEW)
        graph = Subgraph(product_id=product_id)
        async with self._db.tenant(scope) as s:
            if await s.get(Product, product_id) is None:
                raise GraphError("not_found")
            first = (
                await s.scalars(select(Dependency).where(Dependency.from_product_id == product_id))
            ).all()
            frontier: list[uuid.UUID] = []
            for edge in first:
                graph.edges.append(_edge_view(edge, product_id))
                frontier.append(edge.to_component_id)
            level = 1
            seen: set[uuid.UUID] = set()
            depth_of: dict[uuid.UUID, int] = {}
            while frontier and level <= depth:
                current = [c for c in dict.fromkeys(frontier) if c not in seen]
                seen.update(current)
                for c in current:
                    depth_of.setdefault(c, level)
                if level == depth:
                    break
                edges = (
                    await s.scalars(
                        select(Dependency).where(Dependency.from_component_id.in_(current))
                    )
                ).all()
                frontier = []
                for edge in edges:
                    if edge.from_component_id is None:
                        continue
                    graph.edges.append(_edge_view(edge, edge.from_component_id))
                    frontier.append(edge.to_component_id)
                level += 1
            await self._describe(s, graph, depth_of)
        # Edges pointing past the depth limit are dropped from the view.
        graph.edges = [e for e in graph.edges if e.target in graph.nodes]
        return graph

    async def dependants(
        self, scope: TenantScope, component_id: uuid.UUID, depth: int = MAX_DEPTH
    ) -> list[Dependant]:
        """Products affected by ``component_id`` within ``depth`` hops, with their paths."""
        scope.require(Action.GRAPH_VIEW)
        found: list[Dependant] = []
        async with self._db.tenant(scope) as s:
            queue: deque[tuple[uuid.UUID, tuple[uuid.UUID, ...]]] = deque(
                [(component_id, (component_id,))]
            )
            visited = {component_id}
            while queue:
                node, path = queue.popleft()
                edges = (
                    await s.scalars(select(Dependency).where(Dependency.to_component_id == node))
                ).all()
                for edge in edges:
                    if edge.from_product_id is not None:
                        found.append(Dependant(product_id=edge.from_product_id, path=path))
                    elif (
                        edge.from_component_id is not None
                        and edge.from_component_id not in visited
                        and len(path) < depth
                    ):
                        visited.add(edge.from_component_id)
                        queue.append((edge.from_component_id, (*path, edge.from_component_id)))
        return found

    @staticmethod
    async def apply_tag_rules(
        s: AsyncSession, component: Component, org_id: uuid.UUID | None
    ) -> int:
        """Add the edges platform rules require for the component's tags."""
        if not component.tags:
            return 0
        rules = (
            await s.scalars(select(CatalogTagRule).where(CatalogTagRule.tag.in_(component.tags)))
        ).all()
        added = 0
        for rule in rules:
            if rule.to_component_id == component.id:
                continue
            exists = await s.scalar(
                select(Dependency.id).where(
                    Dependency.from_component_id == component.id,
                    Dependency.to_component_id == rule.to_component_id,
                    or_(Dependency.org_id == org_id, Dependency.org_id.is_(None)),
                )
            )
            if exists is None:
                s.add(
                    Dependency(
                        org_id=org_id,
                        from_component_id=component.id,
                        to_component_id=rule.to_component_id,
                    )
                )
                added += 1
        return added

    @staticmethod
    async def _reaches(s: AsyncSession, start: uuid.UUID, goal: uuid.UUID) -> bool:
        frontier, visited = [start], {start}
        while frontier:
            targets = (
                await s.scalars(
                    select(Dependency.to_component_id).where(
                        Dependency.from_component_id.in_(frontier)
                    )
                )
            ).all()
            if goal in targets:
                return True
            frontier = [t for t in dict.fromkeys(targets) if t not in visited]
            visited.update(frontier)
        return False

    @staticmethod
    async def _describe(s: AsyncSession, graph: Subgraph, depth_of: dict[uuid.UUID, int]) -> None:
        if not depth_of:
            return
        rows = await s.execute(
            select(Component, Service)
            .join(Service, Service.id == Component.service_id)
            .where(Component.id.in_(list(depth_of)))
        )
        for component, service in rows.all():
            graph.nodes[component.id] = Node(
                component_id=component.id,
                component=component.name,
                service=service.name,
                service_id=service.id,
                shared=component.org_id is None,
                depth=depth_of[component.id],
            )


def _edge_view(edge: Dependency, origin: uuid.UUID) -> EdgeView:
    return EdgeView(
        id=edge.id,
        origin=origin,
        target=edge.to_component_id,
        shared=edge.org_id is None,
        usage=edge.usage,
    )
