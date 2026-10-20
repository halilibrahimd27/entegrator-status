"""Import an organization's dependencies into its graph: preview, then confirm.

The preview (package list only, never the uploaded file) is kept in Redis for 15
minutes under a random token bound to the organization and product.
"""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import asdict, dataclass

from redis.asyncio import Redis
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from sentinel import audit
from sentinel.accounts.tokens import new_token
from sentinel.catalog.parsers import (
    ADVISORY_ECOSYSTEM,
    RELEASE_ECOSYSTEM,
    ImportFormatError,
    Purl,
    parse_upload,
)
from sentinel.db.session import Database
from sentinel.models import Component, Dependency, Product, Service, ServiceKind, Source
from sentinel.sources.config import validate_source
from sentinel.tenancy.roles import Action
from sentinel.tenancy.scope import TenantScope

PREVIEW_TTL_S = 15 * 60
PACKAGE_COMPONENT = "paket"


@dataclass(frozen=True)
class PreviewItem:
    purl: str  # base, without version
    name: str
    type: str
    version: str | None
    match: str  # "shared" | "private" | "new"


@dataclass(frozen=True)
class Preview:
    token: str
    product_id: uuid.UUID
    items: list[PreviewItem]

    def count(self, match: str) -> int:
        return sum(1 for i in self.items if i.match == match)


@dataclass(frozen=True)
class ImportResult:
    linked: int
    created_services: int
    updated: int


def _slug_for(purl: Purl) -> str:
    text = re.sub(r"[^a-z0-9]+", "-", f"{purl.type}-{purl.display}".lower()).strip("-")
    return text[:63].rstrip("-")


class ImportService:
    def __init__(self, db: Database, redis: Redis) -> None:
        self._db = db
        self._redis = redis

    def _key(self, scope: TenantScope, token: str) -> str:
        return f"import:{scope.org_id}:{token}"

    async def preview(
        self, scope: TenantScope, product_id: uuid.UUID, filename: str, content: bytes
    ) -> Preview:
        scope.require(Action.GRAPH_EDIT)
        packages = parse_upload(filename, content)
        async with self._db.tenant(scope) as s:
            if await s.get(Product, product_id) is None:
                raise ImportFormatError("not_found")
            known = {
                row.purl: row.org_id
                for row in (
                    await s.execute(
                        select(Service.purl, Service.org_id).where(
                            Service.purl.in_([p.base for p in packages])
                        )
                    )
                ).all()
            }
        items = [
            PreviewItem(
                purl=p.base,
                name=p.display,
                type=p.type,
                version=p.version,
                match="new"
                if p.base not in known
                else ("shared" if known[p.base] is None else "private"),
            )
            for p in packages
        ]
        token = new_token(18)
        payload = {"product_id": str(product_id), "items": [asdict(i) for i in items]}
        await self._redis.set(self._key(scope, token), json.dumps(payload), ex=PREVIEW_TTL_S)
        return Preview(token=token, product_id=product_id, items=items)

    async def confirm(
        self, scope: TenantScope, product_id: uuid.UUID, token: str, ip: str
    ) -> ImportResult:
        scope.require(Action.GRAPH_EDIT)
        # The product is checked before the token is spent: a foreign or unknown product
        # answers "not found" and leaves the preview usable.
        async with self._db.tenant(scope) as s:
            if await s.get(Product, product_id) is None:
                raise ImportFormatError("not_found")
        raw = await self._redis.getdel(self._key(scope, token))
        if raw is None:
            raise ImportFormatError("imports.expired")
        payload = json.loads(raw)
        if payload["product_id"] != str(product_id):
            raise ImportFormatError("imports.expired")
        items = [PreviewItem(**i) for i in payload["items"]]
        linked = created = updated = 0
        async with self._db.tenant(scope) as s:
            if await s.get(Product, product_id) is None:
                raise ImportFormatError("not_found")
            for item in items:
                component, was_created = await self._package_component(s, scope, item)
                created += was_created
                usage = {"version": item.version} if item.version else {}
                edge = await s.scalar(
                    select(Dependency).where(
                        Dependency.org_id == scope.org_id,
                        Dependency.from_product_id == product_id,
                        Dependency.to_component_id == component.id,
                    )
                )
                if edge is None:
                    s.add(
                        Dependency(
                            org_id=scope.org_id,
                            from_product_id=product_id,
                            to_component_id=component.id,
                            usage=usage,
                        )
                    )
                    linked += 1
                elif edge.usage != usage:
                    edge.usage = usage
                    updated += 1
            audit.record(
                s,
                "graph.imported",
                org_id=scope.org_id,
                actor_user_id=scope.user_id,
                target_type="product",
                target_id=product_id,
                details={"linked": linked, "created_services": created, "updated": updated},
                ip=ip,
            )
        return ImportResult(linked=linked, created_services=created, updated=updated)

    async def _package_component(
        self, s: AsyncSession, scope: TenantScope, item: PreviewItem
    ) -> tuple[Component, bool]:
        """The component of the matching shared or own package service (created if new)."""
        service = await s.scalar(
            select(Service)
            .where(
                Service.purl == item.purl,
                or_(Service.org_id.is_(None), Service.org_id == scope.org_id),
            )
            .order_by(Service.org_id.is_(None).desc())  # prefer the shared catalog entry
        )
        created = False
        if service is None:
            kind = ServiceKind.CONTAINER_IMAGE if item.type == "docker" else ServiceKind.PACKAGE
            service = Service(
                org_id=scope.org_id,
                slug=await self._free_slug(s, _slug_for(Purl(item.type, None, item.name, None))),
                name=item.name,
                kind=kind.value,
                tags=[item.type],
                purl=item.purl,
            )
            s.add(service)
            await s.flush()
            created = True
        component = await s.scalar(
            select(Component)
            .where(Component.service_id == service.id)
            .order_by(Component.created_at)
        )
        if component is None:
            component = Component(
                org_id=service.org_id,
                service_id=service.id,
                slug=PACKAGE_COMPONENT,
                name=item.name,
                tags=[],
            )
            s.add(component)
            await s.flush()
            if service.org_id is not None:
                self._package_sources(s, component, item)
        return component, created

    @staticmethod
    def _package_sources(s: AsyncSession, component: Component, item: PreviewItem) -> None:
        """Release and advisory sources for the package (run from Phase 6 on)."""
        definitions = []
        if item.type in RELEASE_ECOSYSTEM:
            definitions.append(
                ("release", {"ecosystem": RELEASE_ECOSYSTEM[item.type], "name": item.name})
            )
        if item.type in ADVISORY_ECOSYSTEM:
            definitions.append(
                ("advisory", {"ecosystem": ADVISORY_ECOSYSTEM[item.type], "name": item.name})
            )
        for kind, config in definitions:
            valid = validate_source(kind, config)
            s.add(
                Source(
                    org_id=component.org_id,
                    component_id=component.id,
                    kind=valid.kind.value,
                    identity=valid.identity,
                    config=valid.config,
                    interval_s=valid.interval_s,
                    timeout_s=valid.timeout_s,
                    provenance="import",
                )
            )

    @staticmethod
    async def _free_slug(s: AsyncSession, base: str) -> str:
        taken = set(
            (await s.scalars(select(Service.slug).where(Service.slug.like(f"{base}%")))).all()
        )
        slug, n = base, 2
        while slug in taken:
            suffix = f"-{n}"
            slug, n = base[: 63 - len(suffix)] + suffix, n + 1
        return slug
