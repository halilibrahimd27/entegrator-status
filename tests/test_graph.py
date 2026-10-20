"""Edges, cycles, depth limits and visibility of the dependency graph."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from itertools import pairwise

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from sentinel.db.session import Database
from sentinel.graph.service import GraphError, GraphService
from sentinel.models import CatalogTagRule, Component, Dependency, Organization, Product, Service
from sentinel.tenancy.roles import Role
from sentinel.tenancy.scope import PermissionDeniedError, TenantScope


@dataclass
class World:
    a: TenantScope
    b: TenantScope
    gib: uuid.UUID  # shared: GİB central e-Fatura
    integrator: uuid.UUID  # shared: an integrator's e-Fatura API (depends on GİB)
    product: uuid.UUID  # A's product
    private: uuid.UUID  # A's private component


async def component(
    s: AsyncSession, service: Service, slug: str, tags: list[str] | None = None
) -> Component:
    c = Component(
        org_id=service.org_id, service_id=service.id, slug=slug, name=slug.upper(), tags=tags or []
    )
    s.add(c)
    await s.flush()
    return c


@pytest.fixture
async def world(db: Database) -> World:
    async with db.system("test fixture") as s:
        org_a, org_b = Organization(slug="a", name="A"), Organization(slug="b", name="B")
        s.add_all([org_a, org_b])
        await s.flush()
        gib_svc = Service(org_id=None, slug="gib", name="GİB", kind="authority", tags=[])
        int_svc = Service(
            org_id=None, slug="entegrator-x", name="Entegratör X", kind="integrator", tags=[]
        )
        own_svc = Service(
            org_id=org_a.id, slug="ic-sistem", name="İç sistem", kind="internal", tags=[]
        )
        s.add_all([gib_svc, int_svc, own_svc])
        await s.flush()
        gib = await component(s, gib_svc, "efatura-merkez")
        integrator = await component(s, int_svc, "efatura")
        private = await component(s, own_svc, "api")
        s.add(Dependency(org_id=None, from_component_id=integrator.id, to_component_id=gib.id))
        product = Product(org_id=org_a.id, slug="fatura", name="Fatura modülü")
        s.add(product)
        await s.flush()
        ids = (org_a.id, org_b.id, gib.id, integrator.id, product.id, private.id)
    a = TenantScope(org_id=ids[0], user_id=None, role=Role.MEMBER)
    b = TenantScope(org_id=ids[1], user_id=None, role=Role.MEMBER)
    return World(a=a, b=b, gib=ids[2], integrator=ids[3], product=ids[4], private=ids[5])


async def test_third_degree_view(db: Database, world: World) -> None:
    graph = GraphService(db)
    await graph.add(
        world.a,
        from_product_id=world.product,
        to_component_id=world.integrator,
        usage={"version": "v2"},
    )
    view = await graph.downstream(world.a, world.product)
    assert {n.component: n.depth for n in view.nodes.values()} == {
        "EFATURA": 1,
        "EFATURA-MERKEZ": 2,
    }
    assert all(n.shared for n in view.nodes.values())
    assert any(e.shared for e in view.edges)  # the curated upstream edge
    assert any(not e.shared and e.usage == {"version": "v2"} for e in view.edges)

    affected = await graph.dependants(world.a, world.gib)
    assert [(d.product_id, d.path) for d in affected] == [
        (world.product, (world.gib, world.integrator))
    ]


async def test_cycles_are_refused(db: Database, world: World) -> None:
    graph = GraphService(db)
    with pytest.raises(GraphError, match=r"graph\.cycle"):
        await graph.add(world.a, from_component_id=world.gib, to_component_id=world.integrator)
    with pytest.raises(GraphError, match=r"graph\.cycle"):
        await graph.add(world.a, from_component_id=world.gib, to_component_id=world.gib)
    # A longer loop through a private component is caught too.
    await graph.add(world.a, from_component_id=world.gib, to_component_id=world.private)
    with pytest.raises(GraphError, match=r"graph\.cycle"):
        await graph.add(world.a, from_component_id=world.private, to_component_id=world.integrator)


async def test_duplicates_and_origins(db: Database, world: World) -> None:
    graph = GraphService(db)
    await graph.add(world.a, from_product_id=world.product, to_component_id=world.gib)
    with pytest.raises(GraphError, match=r"graph\.exists"):
        await graph.add(world.a, from_product_id=world.product, to_component_id=world.gib)
    with pytest.raises(GraphError, match=r"graph\.one_origin"):
        await graph.add(world.a, to_component_id=world.gib)
    with pytest.raises(GraphError, match=r"graph\.one_origin"):
        await graph.add(
            world.a,
            from_product_id=world.product,
            from_component_id=world.private,
            to_component_id=world.gib,
        )


async def test_shared_edges_cannot_be_removed_by_tenants(db: Database, world: World) -> None:
    graph = GraphService(db)
    view_edge = await graph.add(
        world.a, from_product_id=world.product, to_component_id=world.integrator
    )
    shared = next(e for e in (await graph.downstream(world.a, world.product)).edges if e.shared)
    with pytest.raises(GraphError, match=r"graph\.shared_edge"):
        await graph.remove(world.a, shared.id)
    await graph.remove(world.a, view_edge)
    assert (await graph.downstream(world.a, world.product)).nodes == {}


async def test_other_tenants_see_none_of_it(db: Database, world: World) -> None:
    graph = GraphService(db)
    await graph.add(world.a, from_product_id=world.product, to_component_id=world.integrator)
    with pytest.raises(GraphError, match="not_found"):
        await graph.add(world.b, from_product_id=world.product, to_component_id=world.gib)
    with pytest.raises(GraphError, match="not_found"):
        await graph.add(world.b, from_component_id=world.private, to_component_id=world.gib)
    with pytest.raises(GraphError, match="not_found"):
        await graph.downstream(world.b, world.product)
    assert await graph.dependants(world.b, world.gib) == []  # A's product is invisible to B


async def test_depth_limit(db: Database, world: World) -> None:
    graph = GraphService(db)
    async with db.system("test chain") as s:
        svc = Service(org_id=None, slug="chain", name="Chain", kind="other", tags=[])
        s.add(svc)
        await s.flush()
        chain = [await component(s, svc, f"c{i}") for i in range(5)]
        for upper, lower in pairwise(chain):
            s.add(Dependency(org_id=None, from_component_id=upper.id, to_component_id=lower.id))
        first, last = chain[0].id, chain[-1].id
    await graph.add(world.a, from_product_id=world.product, to_component_id=first)
    view = await graph.downstream(world.a, world.product)
    assert sorted(n.depth for n in view.nodes.values()) == [1, 2, 3]
    assert all(e.target in view.nodes for e in view.edges)
    assert await graph.dependants(world.a, last) == []  # five hops away from the product


async def test_viewers_cannot_edit(db: Database, world: World) -> None:
    viewer = TenantScope(org_id=world.a.org_id, user_id=None, role=Role.VIEWER)
    with pytest.raises(PermissionDeniedError):
        await GraphService(db).add(viewer, from_product_id=world.product, to_component_id=world.gib)


async def test_tag_rules_add_upstream_edges(db: Database, world: World) -> None:
    async with db.system("test rule") as s:
        s.add(CatalogTagRule(tag="efatura_service", to_component_id=world.gib))
        svc = Service(
            org_id=None, slug="entegrator-y", name="Entegratör Y", kind="integrator", tags=[]
        )
        s.add(svc)
        await s.flush()
        tagged = await component(s, svc, "efatura", tags=["efatura_service"])
        assert await GraphService.apply_tag_rules(s, tagged, None) == 1
        assert await GraphService.apply_tag_rules(s, tagged, None) == 0  # idempotent
        tagged_id = tagged.id
    affected_component = await GraphService(db).dependants(world.a, world.gib)
    assert affected_component == []  # no product depends on Y yet
    await GraphService(db).add(world.a, from_product_id=world.product, to_component_id=tagged_id)
    assert [d.path for d in await GraphService(db).dependants(world.a, world.gib)] == [
        (world.gib, tagged_id)
    ]
