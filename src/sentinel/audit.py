"""Audit trail helpers. Never pass secrets, tokens, passwords or full IP addresses."""

from __future__ import annotations

import ipaddress
import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from sentinel.models import AuditEvent


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
