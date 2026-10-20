"""Organization API tokens: ``es_<8 hex prefix>_<secret>``, shown once, stored as an HMAC."""

from __future__ import annotations

import hmac
import secrets
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select

from sentinel import audit
from sentinel.accounts.tokens import digest
from sentinel.config import Settings
from sentinel.db.base import utcnow
from sentinel.db.session import Database
from sentinel.models import ApiToken
from sentinel.tenancy.orgs import OrgError
from sentinel.tenancy.roles import Action, Role
from sentinel.tenancy.scope import TenantScope

PREFIX = "es_"
SCOPES = frozenset({"read"})
MAX_DAYS = 365
TOUCH_EVERY = timedelta(minutes=5)
_PURPOSE = "api-token"


@dataclass(frozen=True)
class TokenInfo:
    id: uuid.UUID
    name: str
    prefix: str
    scopes: tuple[str, ...]
    created_at: datetime
    expires_at: datetime
    last_used_at: datetime | None


def _split(raw: str) -> tuple[str, str] | None:
    if not raw.startswith(PREFIX) or len(raw) < len(PREFIX) + 10 or raw[len(PREFIX) + 8] != "_":
        return None
    prefix = raw[len(PREFIX) : len(PREFIX) + 8]
    if any(c not in "0123456789abcdef" for c in prefix):
        return None
    return prefix, raw


class ApiTokenService:
    def __init__(
        self, db: Database, settings: Settings, clock: Callable[[], datetime] = utcnow
    ) -> None:
        self._db = db
        self._key = settings.token_hash_key.get_secret_value()
        self._clock = clock

    def _digest(self, raw: str) -> bytes:
        return digest(self._key, raw, purpose=_PURPOSE)

    async def create(
        self, scope: TenantScope, name: str, days: int, ip: str
    ) -> tuple[str, TokenInfo]:
        """Return ``(raw_token, info)``. The raw token is never retrievable again."""
        scope.require(Action.TOKENS_MANAGE)
        name = " ".join(name.split())
        if not 1 <= len(name) <= 80:
            raise OrgError("tokens.invalid_name")
        if not 1 <= days <= MAX_DAYS:
            raise OrgError("tokens.invalid_days")
        prefix = secrets.token_hex(4)
        raw = f"{PREFIX}{prefix}_{secrets.token_urlsafe(32)}"
        now = self._clock()
        token = ApiToken(
            org_id=scope.org_id,
            name=name,
            prefix=prefix,
            token_hash=self._digest(raw),
            scopes=sorted(SCOPES),
            created_by_user_id=scope.user_id,
            expires_at=now + timedelta(days=days),
        )
        async with self._db.tenant(scope) as s:
            s.add(token)
            await s.flush()
            audit.record(
                s,
                "token.created",
                org_id=scope.org_id,
                actor_user_id=scope.user_id,
                target_type="api_token",
                target_id=token.id,
                details={"prefix": prefix},
                ip=ip,
            )
            info = _info(token)
        return raw, info

    async def list(self, scope: TenantScope) -> list[TokenInfo]:
        scope.require(Action.TOKENS_MANAGE)
        async with self._db.tenant(scope) as s:
            rows = await s.scalars(
                select(ApiToken)
                .where(ApiToken.revoked_at.is_(None))
                .order_by(ApiToken.created_at.desc())
            )
            return [_info(t) for t in rows.all()]

    async def revoke(self, scope: TenantScope, token_id: uuid.UUID, ip: str) -> None:
        scope.require(Action.TOKENS_MANAGE)
        async with self._db.tenant(scope) as s:
            token = await s.get(ApiToken, token_id, with_for_update=True)
            if token is None or token.revoked_at is not None:
                raise OrgError("not_found")
            token.revoked_at = self._clock()
            audit.record(
                s,
                "token.revoked",
                org_id=scope.org_id,
                actor_user_id=scope.user_id,
                target_type="api_token",
                target_id=token.id,
                ip=ip,
            )

    async def authenticate(self, raw: str) -> TenantScope | None:
        """Bearer token → read-only scope of its organization, or ``None``."""
        parts = _split(raw.strip())
        if parts is None:
            return None
        prefix, raw = parts
        async with self._db.system("authenticate an API token by its prefix") as s:
            token = await s.scalar(select(ApiToken).where(ApiToken.prefix == prefix))
            if token is None or not hmac.compare_digest(token.token_hash, self._digest(raw)):
                return None
            now = self._clock()
            if token.revoked_at is not None or token.expires_at <= now:
                return None
            if token.last_used_at is None or now - token.last_used_at > TOUCH_EVERY:
                token.last_used_at = now
            return TenantScope(org_id=token.org_id, user_id=None, role=Role.VIEWER)


def _info(token: ApiToken) -> TokenInfo:
    return TokenInfo(
        id=token.id,
        name=token.name,
        prefix=token.prefix,
        scopes=tuple(token.scopes),
        created_at=token.created_at,
        expires_at=token.expires_at,
        last_used_at=token.last_used_at,
    )
