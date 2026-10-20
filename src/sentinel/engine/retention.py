"""Retention jobs (KVKK data minimisation). Idempotent; safe to run at any interval."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, cast

from sqlalchemy import CursorResult, Delete, and_, delete, or_

from sentinel.db.base import utcnow
from sentinel.db.session import Database
from sentinel.models import (
    AuditEvent,
    IngestBatch,
    Invitation,
    Observation,
    OneTimeToken,
    Organization,
    SourceHourly,
    SourceRound,
    User,
    UserSession,
)

UNVERIFIED_ACCOUNTS = timedelta(hours=48)
EXPIRED_HOUSEKEEPING = timedelta(days=1)
STALE_INVITATIONS = timedelta(days=30)
PLATFORM_AUDIT = timedelta(days=365)
RAW_OBSERVATIONS = timedelta(days=30)
INGEST_BATCHES = timedelta(days=8)  # replay protection must outlive the 7-day ingest window
ROUNDS = timedelta(days=120)
HOURLY = timedelta(days=400)


@dataclass(frozen=True)
class RetentionReport:
    unverified_users: int
    purged_orgs: int
    sessions: int
    one_time_tokens: int
    invitations: int
    platform_audit_events: int
    observations: int = 0
    ingest_batches: int = 0
    rounds: int = 0
    hourly: int = 0


async def run_retention(db: Database, clock: Callable[[], datetime] = utcnow) -> RetentionReport:
    now = clock()
    async with db.system("retention: purge expired personal data") as s:

        async def count(statement: Delete) -> int:
            result = cast(CursorResult[Any], await s.execute(statement))
            return int(result.rowcount or 0)

        unverified = await count(
            delete(User).where(
                User.email_verified_at.is_(None), User.created_at < now - UNVERIFIED_ACCOUNTS
            )
        )
        # Cascades remove memberships, invitations, API tokens and the org's audit events.
        orgs = await count(
            delete(Organization).where(
                Organization.deleted_at.is_not(None), Organization.purge_after <= now
            )
        )
        sessions = await count(
            delete(UserSession).where(UserSession.expires_at < now - EXPIRED_HOUSEKEEPING)
        )
        tokens = await count(
            delete(OneTimeToken).where(
                or_(
                    OneTimeToken.expires_at < now - EXPIRED_HOUSEKEEPING,
                    OneTimeToken.used_at < now - EXPIRED_HOUSEKEEPING,
                )
            )
        )
        invitations = await count(
            delete(Invitation).where(
                or_(
                    Invitation.expires_at < now - STALE_INVITATIONS,
                    Invitation.accepted_at < now - STALE_INVITATIONS,
                    Invitation.revoked_at < now - STALE_INVITATIONS,
                )
            )
        )
        audit = await count(
            delete(AuditEvent).where(
                and_(AuditEvent.org_id.is_(None), AuditEvent.occurred_at < now - PLATFORM_AUDIT)
            )
        )
        observations = await count(
            delete(Observation).where(Observation.observed_at < now - RAW_OBSERVATIONS)
        )
        batches = await count(
            delete(IngestBatch).where(IngestBatch.received_at < now - INGEST_BATCHES)
        )
        rounds = await count(delete(SourceRound).where(SourceRound.start < now - ROUNDS))
        hourly = await count(delete(SourceHourly).where(SourceHourly.hour < now - HOURLY))
    return RetentionReport(
        unverified,
        orgs,
        sessions,
        tokens,
        invitations,
        audit,
        observations,
        batches,
        rounds,
        hourly,
    )
