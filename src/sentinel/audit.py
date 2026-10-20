"""Audit trail helpers. Never pass secrets, tokens, passwords or full IP addresses."""

from __future__ import annotations

import ipaddress
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sentinel.db.session import Database
from sentinel.models import AuditEvent, User
from sentinel.tenancy.roles import Action
from sentinel.tenancy.scope import TenantScope


def ip_prefix(ip: str | None) -> str | None:
    """Truncate to /24 (IPv4) or /48 (IPv6): enough to spot abuse, not to identify."""
    if not ip:
        return None
    try:
        address = ipaddress.ip_address(ip)
    except ValueError:
        return None
    prefix = 24 if address.version == 4 else 48
    return str(ipaddress.ip_network(f"{address}/{prefix}", strict=False))


def record(
    session: AsyncSession,
    action: str,
    *,
    org_id: uuid.UUID | None = None,
    actor_user_id: uuid.UUID | None = None,
    target_type: str | None = None,
    target_id: object | None = None,
    details: dict[str, Any] | None = None,
    ip: str | None = None,
) -> None:
    session.add(
        AuditEvent(
            org_id=org_id,
            actor_user_id=actor_user_id,
            action=action,
            target_type=target_type,
            target_id=None if target_id is None else str(target_id),
            details=details or {},
            ip_prefix=ip_prefix(ip),
        )
    )


@dataclass(frozen=True)
class AuditEntry:
    occurred_at: datetime
    action: str
    actor: str | None
    details: dict[str, Any]


async def recent(db: Database, scope: TenantScope, limit: int = 100) -> list[AuditEntry]:
    """The organization's latest audit events (tenant-scoped; platform rows excluded)."""
    scope.require(Action.AUDIT_VIEW)
    async with db.tenant(scope) as s:
        rows = await s.execute(
            select(AuditEvent, User.display_name)
            .outerjoin(User, User.id == AuditEvent.actor_user_id)
            .order_by(AuditEvent.occurred_at.desc())
            .limit(limit)
        )
        return [AuditEntry(e.occurred_at, e.action, name, e.details) for e, name in rows.all()]
