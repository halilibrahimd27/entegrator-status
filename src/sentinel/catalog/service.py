"""What an organization does with the catalog: products, private services, components,
sources, and browsing the shared catalog. Shared rows are read-only here.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from sentinel import audit
from sentinel.db.session import Database
from sentinel.graph.service import GraphService
from sentinel.models import Component, Dependency, Product, Service, ServiceKind, Source
from sentinel.sources.config import SourceConfigError, validate_source
from sentinel.tenancy.roles import Action
from sentinel.tenancy.scope import TenantScope
from sentinel.tenancy.slugs import slugify, with_suffix

PAGE_SIZE = 50
_TAG = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")


class CatalogError(Exception):
    """``key`` is the i18n key; ``detail`` an optional technical explanation."""

    def __init__(self, key: str, detail: str = "") -> None:
        super().__init__(key)
        self.key = key
        self.detail = detail


@dataclass(frozen=True)
class ServiceRow:
    id: uuid.UUID
    slug: str
    name: str
    kind: str
    shared: bool
    components: int


@dataclass(frozen=True)
class ComponentRow:
    id: uuid.UUID
    service_id: uuid.UUID
    service: str
    name: str
    shared: bool


@dataclass(frozen=True)
class ServiceDetail:
    service: Service
    components: list[Component]
    sources: dict[uuid.UUID, list[Source]]  # by component
    upstream: dict[uuid.UUID, list[tuple[Dependency, Component, Service]]]  # by component


def _name(raw: str, *, key: str = "catalog.invalid_name") -> str:
    name = " ".join(raw.split())
    if not 1 <= len(name) <= 120:
        raise CatalogError(key)
    return name


def _tags(raw: str) -> list[str]:
    tags = sorted({t.strip().lower() for t in raw.replace(",", " ").split() if t.strip()})
    if any(not _TAG.match(t) for t in tags) or len(tags) > 20:
        raise CatalogError("catalog.invalid_tags")
    return tags


async def _free_slug(s: AsyncSession, model: type[Product] | type[Service], base: str) -> str:
    taken = set((await s.scalars(select(model.slug).where(model.slug.like(f"{base}%")))).all())
    slug, n = base, 2
    while slug in taken:
        slug, n = with_suffix(base, n), n + 1
    return slug


class CatalogService:
    def __init__(self, db: Database) -> None:
        self._db = db

    # --- products -------------------------------------------------------------

    async def products(self, scope: TenantScope) -> list[tuple[Product, int]]:
        scope.require(Action.GRAPH_VIEW)
        async with self._db.tenant(scope) as s:
            rows = await s.execute(
                select(Product, func.count(Dependency.id))
                .outerjoin(Dependency, Dependency.from_product_id == Product.id)
                .group_by(Product.id)
                .order_by(Product.name)
            )
            return [(p, int(n)) for p, n in rows.all()]

    async def product(self, scope: TenantScope, product_id: uuid.UUID) -> Product:
        scope.require(Action.GRAPH_VIEW)
        async with self._db.tenant(scope) as s:
            product = await s.get(Product, product_id)
            if product is None:
                raise CatalogError("not_found")
            return product

    async def create_product(
        self, scope: TenantScope, name: str, description: str, ip: str
    ) -> Product:
        scope.require(Action.GRAPH_EDIT)
        name = _name(name)
        async with self._db.tenant(scope) as s:
            product = Product(
                org_id=scope.org_id,
                slug=await _free_slug(s, Product, slugify(name)),
                name=name,
                description=description.strip()[:2000] or None,
            )
            s.add(product)
            await s.flush()
            audit.record(
                s,
                "product.created",
                org_id=scope.org_id,
                actor_user_id=scope.user_id,
                target_type="product",
                target_id=product.id,
                ip=ip,
            )
            return product

    async def update_product(
        self, scope: TenantScope, product_id: uuid.UUID, name: str, description: str, ip: str
    ) -> None:
        scope.require(Action.GRAPH_EDIT)
        async with self._db.tenant(scope) as s:
            product = await s.get(Product, product_id)
            if product is None:
                raise CatalogError("not_found")
            product.name = _name(name)
            product.description = description.strip()[:2000] or None
            audit.record(
                s,
                "product.updated",
                org_id=scope.org_id,
                actor_user_id=scope.user_id,
                target_type="product",
                target_id=product.id,
                ip=ip,
            )

    async def delete_product(self, scope: TenantScope, product_id: uuid.UUID, ip: str) -> None:
        scope.require(Action.GRAPH_EDIT)
        async with self._db.tenant(scope) as s:
            product = await s.get(Product, product_id)
            if product is None:
                raise CatalogError("not_found")
            await s.delete(product)
            audit.record(
                s,
                "product.deleted",
                org_id=scope.org_id,
                actor_user_id=scope.user_id,
                target_type="product",
                target_id=product_id,
                ip=ip,
            )

    async def dependencies(
        self, scope: TenantScope, product_id: uuid.UUID
    ) -> list[tuple[Dependency, Component, Service]]:
        scope.require(Action.GRAPH_VIEW)
        async with self._db.tenant(scope) as s:
            rows = await s.execute(
                select(Dependency, Component, Service)
                .join(Component, Component.id == Dependency.to_component_id)
                .join(Service, Service.id == Component.service_id)
                .where(Dependency.from_product_id == product_id)
                .order_by(Service.name, Component.name)
            )
            return [(d, c, sv) for d, c, sv in rows.all()]

    # --- browsing -----------------------------------------------------------------

    async def services(
        self, scope: TenantScope, query: str = "", *, shared: bool | None = None
    ) -> list[ServiceRow]:
        scope.require(Action.GRAPH_VIEW)
        async with self._db.tenant(scope) as s:
            statement = (
                select(Service, func.count(Component.id))
                .outerjoin(Component, Component.service_id == Service.id)
                .group_by(Service.id)
                .order_by(Service.org_id.is_(None), Service.name)
                .limit(PAGE_SIZE)
            )
            if query.strip():
                like = f"%{query.strip()[:80]}%"
                statement = statement.where(
                    or_(
                        Service.name.ilike(like), Service.slug.ilike(like), Service.purl.ilike(like)
                    )
                )
            if shared is True:
                statement = statement.where(Service.org_id.is_(None))
            elif shared is False:
                statement = statement.where(Service.org_id == scope.org_id)
            rows = await s.execute(statement)
            return [
                ServiceRow(sv.id, sv.slug, sv.name, sv.kind, sv.org_id is None, int(n))
                for sv, n in rows.all()
            ]

    async def find_components(self, scope: TenantScope, query: str) -> list[ComponentRow]:
        scope.require(Action.GRAPH_VIEW)
        if not query.strip():
            return []
        like = f"%{query.strip()[:80]}%"
        async with self._db.tenant(scope) as s:
            rows = await s.execute(
                select(Component, Service)
                .join(Service, Service.id == Component.service_id)
                .where(
                    or_(
                        Service.name.ilike(like),
                        Component.name.ilike(like),
                        Service.slug.ilike(like),
                    )
                )
                .order_by(Service.name, Component.name)
                .limit(PAGE_SIZE)
            )
            return [
                ComponentRow(c.id, sv.id, sv.name, c.name, c.org_id is None) for c, sv in rows.all()
            ]

    async def service_detail(self, scope: TenantScope, service_id: uuid.UUID) -> ServiceDetail:
        scope.require(Action.GRAPH_VIEW)
        async with self._db.tenant(scope) as s:
            service = await s.get(Service, service_id)
            if service is None:
                raise CatalogError("not_found")
            components = list(
                (
                    await s.scalars(
                        select(Component)
                        .where(Component.service_id == service_id)
                        .order_by(Component.name)
                    )
                ).all()
            )
            ids = [c.id for c in components]
            sources: dict[uuid.UUID, list[Source]] = {c: [] for c in ids}
            for source in (
                await s.scalars(
                    select(Source).where(Source.component_id.in_(ids)).order_by(Source.kind)
                )
            ).all():
                sources[source.component_id].append(source)
            upstream: dict[uuid.UUID, list[tuple[Dependency, Component, Service]]] = {
                c: [] for c in ids
            }
            rows = await s.execute(
                select(Dependency, Component, Service)
                .join(Component, Component.id == Dependency.to_component_id)
                .join(Service, Service.id == Component.service_id)
                .where(Dependency.from_component_id.in_(ids))
            )
            for dep, target, target_service in rows.all():
                if dep.from_component_id is not None:
                    upstream[dep.from_component_id].append((dep, target, target_service))
            return ServiceDetail(service, components, sources, upstream)

    # --- private services, components, sources ----------------------------------------

    async def create_service(
        self,
        scope: TenantScope,
        *,
        name: str,
        kind: str,
        homepage: str = "",
        description: str = "",
        component: str = "",
        tags: str = "",
        ip: str = "",
    ) -> Service:
        scope.require(Action.GRAPH_EDIT)
        name = _name(name)
        try:
            service_kind = ServiceKind(kind)
        except ValueError:
            raise CatalogError("catalog.invalid_kind") from None
        homepage = homepage.strip()
        if homepage:
            try:
                validate_source("http", {"url": homepage})
            except SourceConfigError as exc:
                raise CatalogError("catalog.invalid_url", str(exc)) from None
        async with self._db.tenant(scope) as s:
            service = Service(
                org_id=scope.org_id,
                slug=await _free_slug(s, Service, slugify(name)),
                name=name,
                kind=service_kind.value,
                homepage=homepage or None,
                description=description.strip()[:2000] or None,
                tags=[],
            )
            s.add(service)
            await s.flush()
            first = Component(
                org_id=scope.org_id,
                service_id=service.id,
                slug=slugify(component or "genel"),
                name=_name(component or "Genel"),
                tags=_tags(tags),
            )
            s.add(first)
            await s.flush()
            await GraphService.apply_tag_rules(s, first, scope.org_id)
            audit.record(
                s,
                "service.created",
                org_id=scope.org_id,
                actor_user_id=scope.user_id,
                target_type="service",
                target_id=service.id,
                ip=ip,
            )
            return service

    async def add_component(
        self, scope: TenantScope, service_id: uuid.UUID, name: str, tags: str, ip: str
    ) -> Component:
        scope.require(Action.GRAPH_EDIT)
        async with self._db.tenant(scope) as s:
            service = await s.get(Service, service_id)
            if service is None:
                raise CatalogError("not_found")
            if service.org_id is None:
                raise CatalogError("catalog.shared_readonly")
            name = _name(name)
            slug = slugify(name)
            if await s.scalar(
                select(Component.id).where(
                    Component.service_id == service_id, Component.slug == slug
                )
            ):
                raise CatalogError("catalog.duplicate_component")
            component = Component(
                org_id=scope.org_id, service_id=service_id, slug=slug, name=name, tags=_tags(tags)
            )
            s.add(component)
            await s.flush()
            await GraphService.apply_tag_rules(s, component, scope.org_id)
            audit.record(
                s,
                "component.created",
                org_id=scope.org_id,
                actor_user_id=scope.user_id,
                target_type="component",
                target_id=component.id,
                ip=ip,
            )
            return component

    async def add_source(
        self,
        scope: TenantScope,
        component_id: uuid.UUID,
        kind: str,
        config: dict[str, Any],
        *,
        interval_s: int | None = None,
        allow_http: bool = False,
        ip: str = "",
    ) -> Source:
        """A private source; may hang under a shared component (the org's own check)."""
        scope.require(Action.GRAPH_EDIT)
        try:
            valid = validate_source(kind, config, interval_s=interval_s, allow_http=allow_http)
        except SourceConfigError as exc:
            raise CatalogError("catalog.invalid_source", str(exc)) from None
        async with self._db.tenant(scope) as s:
            if await s.get(Component, component_id) is None:
                raise CatalogError("not_found")
            duplicate = await s.scalar(
                select(Source.id).where(
                    Source.component_id == component_id,
                    Source.identity == valid.identity,
                    Source.org_id == scope.org_id,
                )
            )
            if duplicate is not None:
                raise CatalogError("catalog.duplicate_source")
            source = Source(
                org_id=scope.org_id,
                component_id=component_id,
                kind=valid.kind.value,
                identity=valid.identity,
                config=valid.config,
                interval_s=valid.interval_s,
                timeout_s=valid.timeout_s,
                allow_http=allow_http,
                provenance="org",
            )
            s.add(source)
            await s.flush()
            audit.record(
                s,
                "source.created",
                org_id=scope.org_id,
                actor_user_id=scope.user_id,
                target_type="source",
                target_id=source.id,
                details={"kind": valid.kind.value},
                ip=ip,
            )
            return source

    async def delete_private(
        self,
        scope: TenantScope,
        model: type[Service] | type[Component] | type[Source],
        row_id: uuid.UUID,
        ip: str,
    ) -> None:
        scope.require(Action.GRAPH_EDIT)
        async with self._db.tenant(scope) as s:
            row: Service | Component | Source | None = await s.get(model, row_id)
            if row is None:
                raise CatalogError("not_found")
            if row.org_id is None:
                raise CatalogError("catalog.shared_readonly")
            await s.delete(row)
            audit.record(
                s,
                f"{model.__tablename__.rstrip('s')}.deleted",
                org_id=scope.org_id,
                actor_user_id=scope.user_id,
                target_type=model.__tablename__,
                target_id=row_id,
                ip=ip,
            )

    async def create_api_from_openapi(
        self, scope: TenantScope, name: str, url: str, ip: str
    ) -> Service:
        """An API service whose contract is watched from its OpenAPI document (Phase 7)."""
        try:
            validate_source("api_contract", {"url": url, "format": "openapi"})
        except SourceConfigError as exc:
            raise CatalogError("catalog.invalid_source", str(exc)) from None
        service = await self.create_service(
            scope, name=name, kind=ServiceKind.SAAS_API.value, component="API", ip=ip
        )
        detail = await self.service_detail(scope, service.id)
        await self.add_source(
            scope, detail.components[0].id, "api_contract", {"url": url, "format": "openapi"}, ip=ip
        )
        return service
