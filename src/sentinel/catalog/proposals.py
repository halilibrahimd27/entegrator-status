"""Organizations propose private services for the shared catalog; platform admins review.

Accepting copies the service and its components only. Sources are not copied: their
URLs may be internal, and shared sources are added through reviewed packs after a
human checked that they are public (docs/CATALOG.md). Usage data and results stay
with the organization.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sentinel import audit
from sentinel.catalog.service import CatalogError
from sentinel.db.base import utcnow
from sentinel.db.session import Database
from sentinel.models import CatalogProposal, Component, Organization, ProposalStatus, Service
from sentinel.tenancy.roles import Action
from sentinel.tenancy.scope import TenantScope
from sentinel.tenancy.slugs import with_suffix


@dataclass(frozen=True)
class ProposalRow:
    id: uuid.UUID
    service_id: uuid.UUID
    service: str
    organization: str
    status: str
    note: str | None
    review_note: str | None
    created_at: datetime


class ProposalService:
    def __init__(self, db: Database, clock: Callable[[], datetime] = utcnow) -> None:
        self._db = db
        self._clock = clock

    async def propose(self, scope: TenantScope, service_id: uuid.UUID, note: str, ip: str) -> None:
        scope.require(Action.GRAPH_EDIT)
        async with self._db.tenant(scope) as s:
            service = await s.get(Service, service_id)
            if service is None:
                raise CatalogError("not_found")
            if service.org_id is None:
                raise CatalogError("catalog.already_shared")
            pending = await s.scalar(
                select(CatalogProposal.id).where(
                    CatalogProposal.service_id == service_id,
                    CatalogProposal.status == ProposalStatus.PENDING.value,
                )
            )
            if pending is not None:
                raise CatalogError("catalog.already_proposed")
            proposal = CatalogProposal(
                org_id=scope.org_id,
                service_id=service_id,
                note=note.strip()[:2000] or None,
                proposed_by_user_id=scope.user_id,
            )
            s.add(proposal)
            await s.flush()
            audit.record(
                s,
                "catalog.proposed",
                org_id=scope.org_id,
                actor_user_id=scope.user_id,
                target_type="service",
                target_id=service_id,
                ip=ip,
            )

    async def of_org(self, scope: TenantScope) -> list[ProposalRow]:
        scope.require(Action.GRAPH_VIEW)
        async with self._db.tenant(scope) as s:
            rows = await s.execute(
                select(CatalogProposal, Service.name)
                .join(Service, Service.id == CatalogProposal.service_id)
                .order_by(CatalogProposal.created_at.desc())
            )
            return [_row(p, name, "") for p, name in rows.all()]

    async def pending(self) -> list[ProposalRow]:
        """For platform admins (the caller checks the role)."""
        async with self._db.system("platform admin: list pending catalog proposals") as s:
            rows = await s.execute(
                select(CatalogProposal, Service.name, Organization.name)
                .join(Service, Service.id == CatalogProposal.service_id)
                .join(Organization, Organization.id == CatalogProposal.org_id)
                .where(CatalogProposal.status == ProposalStatus.PENDING.value)
                .order_by(CatalogProposal.created_at)
            )
            return [_row(p, service, org) for p, service, org in rows.all()]

    async def accept(
        self, admin_user_id: uuid.UUID, proposal_id: uuid.UUID, note: str, ip: str
    ) -> uuid.UUID:
        async with self._db.system("platform admin: accept a catalog proposal") as s:
            proposal = await self._open(s, proposal_id)
            private = await s.get(Service, proposal.service_id)
            if private is None:
                raise CatalogError("not_found")
            taken = set(
                (
                    await s.scalars(
                        select(Service.slug).where(
                            Service.org_id.is_(None), Service.slug.like(f"{private.slug}%")
                        )
                    )
                ).all()
            )
            slug, n = private.slug, 2
            while slug in taken:
                slug, n = with_suffix(private.slug, n), n + 1
            shared = Service(
                org_id=None,
                slug=slug,
                name=private.name,
                kind=private.kind,
                homepage=private.homepage,
                description=private.description,
                tags=list(private.tags),
                purl=private.purl,
                pack="proposal",
            )
            s.add(shared)
            await s.flush()
            for component in (
                await s.scalars(select(Component).where(Component.service_id == private.id))
            ).all():
                s.add(
                    Component(
                        org_id=None,
                        service_id=shared.id,
                        slug=component.slug,
                        name=component.name,
                        description=component.description,
                        tags=list(component.tags),
                    )
                )
            self._close(proposal, ProposalStatus.ACCEPTED, admin_user_id, note)
            proposal.shared_service_id = shared.id
            audit.record(
                s,
                "catalog.proposal_accepted",
                actor_user_id=admin_user_id,
                target_type="service",
                target_id=shared.id,
                ip=ip,
            )
            audit.record(
                s,
                "catalog.proposal_accepted",
                org_id=proposal.org_id,
                target_type="service",
                target_id=private.id,
            )
            return shared.id

    async def reject(
        self, admin_user_id: uuid.UUID, proposal_id: uuid.UUID, note: str, ip: str
    ) -> None:
        async with self._db.system("platform admin: reject a catalog proposal") as s:
            proposal = await self._open(s, proposal_id)
            self._close(proposal, ProposalStatus.REJECTED, admin_user_id, note)
            audit.record(
                s,
                "catalog.proposal_rejected",
                actor_user_id=admin_user_id,
                target_type="proposal",
                target_id=proposal.id,
                ip=ip,
            )
            audit.record(
                s,
                "catalog.proposal_rejected",
                org_id=proposal.org_id,
                target_type="service",
                target_id=proposal.service_id,
            )

    @staticmethod
    async def _open(s: AsyncSession, proposal_id: uuid.UUID) -> CatalogProposal:
        proposal = await s.get(CatalogProposal, proposal_id, with_for_update=True)
        if proposal is None or proposal.status != ProposalStatus.PENDING.value:
            raise CatalogError("not_found")
        return proposal

    def _close(
        self, proposal: CatalogProposal, status: ProposalStatus, admin: uuid.UUID, note: str
    ) -> None:
        proposal.status = status.value
        proposal.reviewed_by_user_id = admin
        proposal.reviewed_at = self._clock()
        proposal.review_note = note.strip()[:2000] or None


def _row(p: CatalogProposal, service: str, organization: str) -> ProposalRow:
    return ProposalRow(
        p.id, p.service_id, service, organization, p.status, p.note, p.review_note, p.created_at
    )
