"""Organizations and memberships.

Organization rows are the tenants themselves; memberships are tenant-owned. Crossing
tenants happens in exactly three named places: creating an organization, listing the
organizations a user belongs to, and resolving a user's scope for a slug.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from sentinel import audit
from sentinel.db.base import utcnow
from sentinel.db.session import Database
from sentinel.models import Membership, Organization, User
from sentinel.tenancy.roles import Action, Role, can_grant
from sentinel.tenancy.scope import PermissionDeniedError, TenantScope
from sentinel.tenancy.slugs import slugify, with_suffix


class OrgError(Exception):
    """``key`` is the i18n key shown to the user."""

    def __init__(self, key: str) -> None:
        super().__init__(key)
        self.key = key


@dataclass(frozen=True)
class OrgSummary:
    id: uuid.UUID
    slug: str
    name: str
    role: Role


@dataclass(frozen=True)
class Member:
    membership_id: uuid.UUID
    user_id: uuid.UUID
    display_name: str
    email: str
    role: Role
    joined_at: datetime


def clean_name(raw: str) -> str:
    name = " ".join(raw.split())
    if not 2 <= len(name) <= 120:
        raise OrgError("orgs.invalid_name")
    return name


class OrgService:
    def __init__(self, db: Database, clock: Callable[[], datetime] = utcnow) -> None:
        self._db = db
        self._clock = clock

    async def create(self, user_id: uuid.UUID, name: str, ip: str) -> OrgSummary:
        name = clean_name(name)
        base = slugify(name)
        async with self._db.system("create an organization and its first owner") as s:
            taken = set(
                (
                    await s.scalars(
                        select(Organization.slug).where(Organization.slug.like(f"{base}%"))
                    )
                ).all()
            )
            slug, n = base, 2
            while slug in taken:
                slug, n = with_suffix(base, n), n + 1
            org = Organization(slug=slug, name=name)
            s.add(org)
            await s.flush()
            s.add(Membership(org_id=org.id, user_id=user_id, role=Role.OWNER.value))
            audit.record(s, "org.created", org_id=org.id, actor_user_id=user_id, ip=ip)
            return OrgSummary(id=org.id, slug=slug, name=name, role=Role.OWNER)

    async def of_user(self, user_id: uuid.UUID) -> list[OrgSummary]:
        async with self._db.system("list the organizations of the signed-in user") as s:
            rows = await s.execute(
                select(Organization, Membership.role)
                .join(Membership, Membership.org_id == Organization.id)
                .where(Membership.user_id == user_id, Organization.deleted_at.is_(None))
                .order_by(Organization.name)
            )
            return [OrgSummary(o.id, o.slug, o.name, Role(role)) for o, role in rows.all()]

    async def resolve(
        self, user_id: uuid.UUID, slug: str
    ) -> tuple[TenantScope, Organization] | None:
        """The user's scope in the organization ``slug``, or ``None`` (looks like 404)."""
        async with self._db.system("resolve the signed-in user's scope for a slug") as s:
            row = (
                await s.execute(
                    select(Organization, Membership.role)
                    .join(Membership, Membership.org_id == Organization.id)
                    .where(
                        Organization.slug == slug,
                        Organization.deleted_at.is_(None),
                        Membership.user_id == user_id,
                    )
                )
            ).one_or_none()
        if row is None:
            return None
        org, role = row
        return TenantScope(org_id=org.id, user_id=user_id, role=Role(role)), org

    async def rename(self, scope: TenantScope, name: str, ip: str) -> None:
        scope.require(Action.ORG_UPDATE)
        name = clean_name(name)
        async with self._db.tenant(scope) as s:
            org = await s.get(Organization, scope.org_id)
            if org is None:
                raise OrgError("not_found")
            old, org.name = org.name, name
            audit.record(
                s,
                "org.renamed",
                org_id=scope.org_id,
                actor_user_id=scope.user_id,
                details={"from": old, "to": name},
                ip=ip,
            )

    async def members(self, scope: TenantScope) -> list[Member]:
        scope.require(Action.MEMBERS_VIEW)
        async with self._db.tenant(scope) as s:
            rows = await s.execute(
                select(Membership, User)
                .join(User, User.id == Membership.user_id)
                .order_by(User.display_name)
            )
            members = [
                Member(m.id, u.id, u.display_name, u.email, Role(m.role), m.created_at)
                for m, u in rows.all()
            ]
        order = list(Role)
        return sorted(members, key=lambda m: (order.index(m.role), m.display_name.lower()))

    async def change_role(
        self, scope: TenantScope, membership_id: uuid.UUID, role: Role, ip: str
    ) -> None:
        scope.require(Action.MEMBERS_CHANGE_ROLE)
        async with self._db.tenant(scope) as s:
            membership = await s.get(Membership, membership_id, with_for_update=True)
            if membership is None:
                raise OrgError("not_found")
            current = Role(membership.role)
            # Both the old and the new role must be within the actor's power.
            if not (can_grant(scope.role, current) and can_grant(scope.role, role)):
                raise PermissionDeniedError(Action.MEMBERS_CHANGE_ROLE.value)
            if current is Role.OWNER and role is not Role.OWNER:
                await self._keep_an_owner(s, scope)
            membership.role = role.value
            audit.record(
                s,
                "member.role_changed",
                org_id=scope.org_id,
                actor_user_id=scope.user_id,
                target_type="membership",
                target_id=membership.id,
                details={"from": current.value, "to": role.value},
                ip=ip,
            )

    async def remove(self, scope: TenantScope, membership_id: uuid.UUID, ip: str) -> None:
        scope.require(Action.MEMBERS_REMOVE)
        async with self._db.tenant(scope) as s:
            membership = await s.get(Membership, membership_id, with_for_update=True)
            if membership is None:
                raise OrgError("not_found")
            if not can_grant(scope.role, Role(membership.role)):
                raise PermissionDeniedError(Action.MEMBERS_REMOVE.value)
            if membership.role == Role.OWNER.value:
                await self._keep_an_owner(s, scope)
            await s.delete(membership)
            audit.record(
                s,
                "member.removed",
                org_id=scope.org_id,
                actor_user_id=scope.user_id,
                target_type="user",
                target_id=membership.user_id,
                ip=ip,
            )

    async def leave(self, scope: TenantScope, ip: str) -> None:
        async with self._db.tenant(scope) as s:
            membership = await s.scalar(
                select(Membership).where(Membership.user_id == scope.user_id)
            )
            if membership is None:
                raise OrgError("not_found")
            if membership.role == Role.OWNER.value:
                await self._keep_an_owner(s, scope)
            await s.delete(membership)
            audit.record(s, "member.left", org_id=scope.org_id, actor_user_id=scope.user_id, ip=ip)

    @staticmethod
    async def _keep_an_owner(s: AsyncSession, scope: TenantScope) -> None:
        """Refuse a change that would leave the organization without an owner."""
        owners = await s.scalar(
            select(func.count()).select_from(Membership).where(Membership.role == Role.OWNER.value)
        )
        if (owners or 0) <= 1:
            raise OrgError("members.last_owner")
