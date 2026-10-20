"""KVKK rights: export of one's own data, account deletion, closing an organization."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, select

from sentinel import audit
from sentinel.accounts import totp
from sentinel.accounts.passwords import Passwords
from sentinel.accounts.service import AccountError
from sentinel.accounts.sessions import SessionManager
from sentinel.db.base import utcnow
from sentinel.db.session import Database
from sentinel.models import AuditEvent, Membership, Organization, User, UserSession
from sentinel.tenancy.roles import Action, Role
from sentinel.tenancy.scope import TenantScope

ORG_PURGE_AFTER = timedelta(days=30)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


class PrivacyService:
    def __init__(
        self,
        *,
        db: Database,
        passwords: Passwords,
        totp_box: totp.TotpBox,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self._db = db
        self._passwords = passwords
        self._box = totp_box
        self._clock = clock

    async def export(self, user_id: uuid.UUID) -> dict[str, Any]:
        """Everything we hold about the user, without secrets or hashes."""
        async with self._db.system("export the signed-in user's own data") as s:
            user = await s.get(User, user_id)
            if user is None:
                raise AccountError("not_found")
            memberships = await s.execute(
                select(Organization.name, Organization.slug, Membership.role, Membership.created_at)
                .join(Organization, Organization.id == Membership.org_id)
                .where(Membership.user_id == user_id)
            )
            sessions = await s.scalars(select(UserSession).where(UserSession.user_id == user_id))
            events = await s.execute(
                select(AuditEvent, Organization.slug)
                .outerjoin(Organization, Organization.id == AuditEvent.org_id)
                .where(AuditEvent.actor_user_id == user_id)
                .order_by(AuditEvent.occurred_at)
            )
            return {
                "exported_at": self._clock().isoformat(),
                "account": {
                    "email": user.email,
                    "display_name": user.display_name,
                    "created_at": _iso(user.created_at),
                    "email_verified_at": _iso(user.email_verified_at),
                    "two_factor_enabled": user.totp_enabled_at is not None,
                    "last_login_at": _iso(user.last_login_at),
                    "privacy_notice_version": user.privacy_notice_version,
                    "privacy_notice_seen_at": _iso(user.privacy_notice_seen_at),
                },
                "memberships": [
                    {"organization": name, "slug": slug, "role": role, "joined_at": _iso(joined)}
                    for name, slug, role, joined in memberships.all()
                ],
                "sessions": [
                    {
                        "created_at": _iso(x.created_at),
                        "last_seen_at": _iso(x.last_seen_at),
                        "expires_at": _iso(x.expires_at),
                    }
                    for x in sessions.all()
                ],
                "audit_events": [
                    {
                        "occurred_at": _iso(e.occurred_at),
                        "action": e.action,
                        "organization": slug,
                        "ip_prefix": e.ip_prefix,
                    }
                    for e, slug in events.all()
                ],
            }

    def _check_identity(self, user: User, password: str, code: str) -> None:
        ok, _ = self._passwords.verify(user.password_hash, password)
        if not ok:
            raise AccountError("privacy.wrong_password")
        if user.totp_enabled_at is not None and user.totp_secret is not None:
            secret = self._box.open(user.totp_secret, user.id)
            if totp.accept(secret, code, last_step=user.totp_last_step, now=self._clock()) is None:
                raise AccountError("privacy.wrong_code")

    async def delete_account(self, user_id: uuid.UUID, *, password: str, code: str, ip: str) -> int:
        """Delete the account. Organizations where the user is alone are closed with it.

        Refused while the user is the only owner of an organization that has other
        members (ownership must be handed over first). Returns the number of closed
        organizations.
        """
        async with self._db.system(
            "delete an account and the organizations only it belongs to"
        ) as s:
            user = await s.get(User, user_id, with_for_update=True)
            if user is None:
                raise AccountError("not_found")
            self._check_identity(user, password, code)
            mine = (
                await s.execute(
                    select(Organization, Membership.role)
                    .join(Membership, Membership.org_id == Organization.id)
                    .where(Membership.user_id == user_id, Organization.deleted_at.is_(None))
                )
            ).all()
            to_close: list[Organization] = []
            for org, role in mine:
                members = await s.scalar(
                    select(func.count()).select_from(Membership).where(Membership.org_id == org.id)
                )
                owners = await s.scalar(
                    select(func.count())
                    .select_from(Membership)
                    .where(Membership.org_id == org.id, Membership.role == Role.OWNER.value)
                )
                if (members or 0) <= 1:
                    to_close.append(org)
                elif role == Role.OWNER.value and (owners or 0) <= 1:
                    raise AccountError("privacy.sole_owner")
            now = self._clock()
            for org in to_close:
                org.deleted_at = now
                org.purge_after = now + ORG_PURGE_AFTER
                audit.record(s, "org.deleted", org_id=org.id, details={"reason": "account_deleted"})
            await SessionManager.destroy_all(s, user_id)
            await s.delete(user)  # cascades memberships, sessions and one-time tokens
            audit.record(s, "account.deleted", details={"closed_orgs": len(to_close)}, ip=ip)
            return len(to_close)

    async def close_org(
        self,
        scope: TenantScope,
        user_id: uuid.UUID,
        *,
        password: str,
        code: str,
        confirm: str,
        ip: str,
    ) -> None:
        """Soft-delete now (invisible, tokens stop working); purged after 30 days."""
        scope.require(Action.ORG_DELETE)
        async with self._db.tenant(scope) as s:
            org = await s.get(Organization, scope.org_id, with_for_update=True)
            user = await s.get(User, user_id)
            if org is None or user is None:
                raise AccountError("not_found")
            if confirm.strip() != org.slug:
                raise AccountError("privacy.confirm_mismatch")
            self._check_identity(user, password, code)
            now = self._clock()
            org.deleted_at = now
            org.purge_after = now + ORG_PURGE_AFTER
            audit.record(s, "org.deleted", org_id=org.id, actor_user_id=user_id, ip=ip)
