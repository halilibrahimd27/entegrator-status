"""Invitations: an e-mail-bound, single-use token that turns into a membership."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sentinel import audit
from sentinel.accounts.mail import Mail, Mailer
from sentinel.accounts.service import normalize_email
from sentinel.accounts.throttle import Limit, Throttle
from sentinel.accounts.tokens import digest, new_token
from sentinel.config import Settings
from sentinel.db.base import utcnow
from sentinel.db.session import Database
from sentinel.models import Invitation, Membership, Organization, User
from sentinel.tenancy.orgs import OrgError
from sentinel.tenancy.roles import Action, Role, can_grant
from sentinel.tenancy.scope import PermissionDeniedError, TenantScope
from sentinel.web.i18n import t

INVITE_TTL = timedelta(days=7)
INVITES_PER_ORG = Limit("invite-org", 50, 24 * 3600)
_PURPOSE = "invitation"


@dataclass(frozen=True)
class PendingInvitation:
    id: uuid.UUID
    email: str
    role: Role
    expires_at: datetime


@dataclass(frozen=True)
class InvitationPreview:
    org_name: str
    role: Role
    email: str


class InvitationService:
    def __init__(
        self,
        *,
        db: Database,
        settings: Settings,
        mailer: Mailer,
        throttle: Throttle,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self._db = db
        self._settings = settings
        self._mailer = mailer
        self._throttle = throttle
        self._clock = clock
        self._key = settings.token_hash_key.get_secret_value()

    def _digest(self, token: str) -> bytes:
        return digest(self._key, token, purpose=_PURPOSE)

    async def invite(
        self, scope: TenantScope, email: str, role: Role, inviter: str, ip: str
    ) -> None:
        scope.require(Action.MEMBERS_INVITE)
        if not can_grant(scope.role, role):
            raise PermissionDeniedError(Action.MEMBERS_INVITE.value)
        email = normalize_email(email)
        if not await self._throttle.allow(INVITES_PER_ORG, str(scope.org_id)):
            raise OrgError("throttled")
        token = new_token()
        async with self._db.tenant(scope) as s:
            already = await s.scalar(
                select(Membership.id)
                .join(User, User.id == Membership.user_id)
                .where(User.email == email)
            )
            if already is not None:
                raise OrgError("members.already_member")
            org = await s.get(Organization, scope.org_id)
            if org is None:
                raise OrgError("not_found")
            org_name = org.name
            s.add(
                Invitation(
                    org_id=scope.org_id,
                    email=email,
                    role=role.value,
                    token_hash=self._digest(token),
                    invited_by_user_id=scope.user_id,
                    expires_at=self._clock() + INVITE_TTL,
                )
            )
            audit.record(
                s,
                "member.invited",
                org_id=scope.org_id,
                actor_user_id=scope.user_id,
                details={"role": role.value},
                ip=ip,
            )
        link = f"{self._settings.public_base}/davet/{token}"
        body = t("mail.invite.body").format(org=org_name, inviter=inviter, link=link)
        await self._mailer.send(
            Mail(to=email, subject=t("mail.invite.subject").format(org=org_name), text=body)
        )

    async def pending(self, scope: TenantScope) -> list[PendingInvitation]:
        scope.require(Action.MEMBERS_VIEW)
        now = self._clock()
        async with self._db.tenant(scope) as s:
            rows = await s.scalars(
                select(Invitation)
                .where(
                    Invitation.accepted_at.is_(None),
                    Invitation.revoked_at.is_(None),
                    Invitation.expires_at > now,
                )
                .order_by(Invitation.created_at.desc())
            )
            return [
                PendingInvitation(i.id, i.email, Role(i.role), i.expires_at) for i in rows.all()
            ]

    async def revoke(self, scope: TenantScope, invitation_id: uuid.UUID, ip: str) -> None:
        scope.require(Action.MEMBERS_INVITE)
        async with self._db.tenant(scope) as s:
            invitation = await s.get(Invitation, invitation_id, with_for_update=True)
            if invitation is None or invitation.accepted_at is not None:
                raise OrgError("not_found")
            invitation.revoked_at = self._clock()
            audit.record(
                s,
                "member.invite_revoked",
                org_id=scope.org_id,
                actor_user_id=scope.user_id,
                target_type="invitation",
                target_id=invitation.id,
                ip=ip,
            )

    async def preview(self, token: str) -> InvitationPreview | None:
        async with self._db.system("look up an invitation by its token") as s:
            row = await self._open(s, token)
            if row is None:
                return None
            invitation, org = row
            return InvitationPreview(
                org_name=org.name, role=Role(invitation.role), email=invitation.email
            )

    async def accept(self, token: str, user_id: uuid.UUID, ip: str) -> str:
        """Turn the invitation into a membership. Returns the organization slug."""
        async with self._db.system("accept an invitation by its token") as s:
            row = await self._open(s, token, lock=True)
            if row is None:
                raise OrgError("invite.invalid")
            invitation, org = row
            user = await s.get(User, user_id)
            if user is None or user.email != invitation.email:
                raise OrgError("invite.wrong_account")
            existing = await s.scalar(
                select(Membership.id).where(
                    Membership.org_id == org.id, Membership.user_id == user_id
                )
            )
            invitation.accepted_at = self._clock()
            if existing is None:
                s.add(Membership(org_id=org.id, user_id=user_id, role=invitation.role))
            audit.record(
                s,
                "member.joined",
                org_id=org.id,
                actor_user_id=user_id,
                details={"role": invitation.role},
                ip=ip,
            )
            slug = org.slug
        return slug

    async def _open(
        self, s: AsyncSession, token: str, *, lock: bool = False
    ) -> tuple[Invitation, Organization] | None:
        """A usable invitation (not accepted, revoked or expired) and its organization."""
        query = (
            select(Invitation, Organization)
            .join(Organization, Organization.id == Invitation.org_id)
            .where(Invitation.token_hash == self._digest(token), Organization.deleted_at.is_(None))
        )
        if lock:
            query = query.with_for_update(of=Invitation)
        row = (await s.execute(query)).one_or_none()
        if row is None:
            return None
        invitation, org = row
        if (
            invitation.accepted_at is not None
            or invitation.revoked_at is not None
            or invitation.expires_at <= self._clock()
        ):
            return None
        return invitation, org
