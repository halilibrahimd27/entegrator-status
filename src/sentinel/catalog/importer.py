"""Import validated packs into the shared catalog (idempotent upsert, never deletes).

A dry run performs the same writes inside a transaction and rolls it back, so the
printed plan is exactly what ``--apply`` would do.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any, Literal

from sqlalchemy import select

from sentinel import audit
from sentinel.catalog.pack import Pack, validate_packs
from sentinel.db.session import Database
from sentinel.graph.service import GraphService
from sentinel.models import CatalogTagRule, Component, Dependency, Service, Source
from sentinel.sources.config import validate_source


@dataclass(frozen=True)
class Change:
    op: Literal["+", "~"]
    what: str
    ref: str
    detail: str = ""

    def __str__(self) -> str:
        suffix = f" ({self.detail})" if self.detail else ""
        return f"{self.op} {self.what} {self.ref}{suffix}"


@dataclass
class ImportPlan:
    applied: bool
    changes: list[Change] = field(default_factory=list)

    def add(self, op: Literal["+", "~"], what: str, ref: str, detail: str = "") -> None:
        self.changes.append(Change(op, what, ref, detail))


class InvalidPacksError(ValueError):
    pass


class _DryRun(Exception):  # noqa: N818  (control flow, not an error)
    pass


def _update(row: Any, values: dict[str, Any]) -> list[str]:
    changed = [name for name, value in values.items() if getattr(row, name) != value]
    for name in changed:
        setattr(row, name, values[name])
    return changed


async def import_packs(db: Database, packs: list[Pack], *, apply: bool) -> ImportPlan:
    problems = validate_packs(packs)
    if problems:
        raise InvalidPacksError("; ".join(problems))
    plan = ImportPlan(applied=apply)
    try:
        async with db.system("catalog import of curated packs") as s:
            ids: dict[str, uuid.UUID] = {}
            for pack in packs:
                for svc in pack.services:
                    values: dict[str, Any] = {
                        "name": svc.name,
                        "kind": svc.kind.value,
                        "homepage": svc.homepage,
                        "description": svc.description,
                        "tags": list(svc.tags),
                        "purl": svc.purl,
                        "pack": pack.id,
                    }
                    service = await s.scalar(
                        select(Service).where(Service.org_id.is_(None), Service.slug == svc.slug)
                    )
                    if service is None:
                        service = Service(org_id=None, slug=svc.slug, **values)
                        s.add(service)
                        await s.flush()
                        plan.add("+", "service", svc.slug)
                    elif changed := _update(service, values):
                        plan.add("~", "service", svc.slug, ", ".join(changed))

                    for comp in svc.components:
                        ref = f"{svc.slug}/{comp.slug}"
                        cvalues: dict[str, Any] = {
                            "name": comp.name,
                            "description": comp.description,
                            "tags": list(comp.tags),
                        }
                        component = await s.scalar(
                            select(Component).where(
                                Component.service_id == service.id, Component.slug == comp.slug
                            )
                        )
                        if component is None:
                            component = Component(
                                org_id=None, service_id=service.id, slug=comp.slug, **cvalues
                            )
                            s.add(component)
                            await s.flush()
                            plan.add("+", "component", ref)
                        elif changed := _update(component, cvalues):
                            plan.add("~", "component", ref, ", ".join(changed))
                        ids[ref] = component.id

                        for src in comp.sources:
                            valid = validate_source(
                                src.kind.value,
                                src.config,
                                interval_s=src.interval_s,
                                timeout_s=src.timeout_s,
                                allow_http=src.allow_http,
                            )
                            svalues: dict[str, Any] = {
                                "kind": valid.kind.value,
                                "config": valid.config,
                                "interval_s": valid.interval_s,
                                "timeout_s": valid.timeout_s,
                                "allow_http": src.allow_http,
                                "availability_ignores_robots": src.availability_ignores_robots,
                                "provenance": src.source,
                            }
                            source = await s.scalar(
                                select(Source).where(
                                    Source.org_id.is_(None),
                                    Source.component_id == component.id,
                                    Source.identity == valid.identity,
                                )
                            )
                            if source is None:
                                s.add(
                                    Source(
                                        org_id=None,
                                        component_id=component.id,
                                        identity=valid.identity,
                                        **svalues,
                                    )
                                )
                                plan.add("+", "source", f"{ref} {valid.identity}")
                            elif changed := _update(source, svalues):
                                plan.add(
                                    "~", "source", f"{ref} {valid.identity}", ", ".join(changed)
                                )

            for pack in packs:
                for edge in pack.edges:
                    origin, target = ids[edge.from_], ids[edge.to]
                    existing = await s.scalar(
                        select(Dependency).where(
                            Dependency.org_id.is_(None),
                            Dependency.from_component_id == origin,
                            Dependency.to_component_id == target,
                        )
                    )
                    if existing is None:
                        s.add(
                            Dependency(
                                org_id=None,
                                from_component_id=origin,
                                to_component_id=target,
                                note=edge.note,
                            )
                        )
                        plan.add("+", "edge", f"{edge.from_} -> {edge.to}")
                    elif existing.note != edge.note:
                        existing.note = edge.note
                        plan.add("~", "edge", f"{edge.from_} -> {edge.to}", "note")
                for rule in pack.tag_rules:
                    target = ids[rule.to]
                    if (
                        await s.scalar(
                            select(CatalogTagRule.id).where(
                                CatalogTagRule.tag == rule.tag,
                                CatalogTagRule.to_component_id == target,
                            )
                        )
                        is None
                    ):
                        s.add(CatalogTagRule(tag=rule.tag, to_component_id=target, pack=pack.id))
                        plan.add("+", "tag rule", f"{rule.tag} -> {rule.to}")
            await s.flush()

            shared = await s.execute(
                select(Component, Service.slug)
                .join(Service, Service.id == Component.service_id)
                .where(Component.org_id.is_(None))
            )
            for component, service_slug in shared.all():
                if await GraphService.apply_tag_rules(s, component, None):
                    plan.add("+", "edge", f"{service_slug}/{component.slug} (tag rule)")

            if not apply:
                raise _DryRun
            audit.record(
                s,
                "catalog.imported",
                details={"packs": [p.id for p in packs], "changes": len(plan.changes)},
            )
    except _DryRun:
        pass
    return plan
