"""An organization's credentials: create, replace, delete, attach. Never read back.

The only way out of the vault is ``secret_for_job`` (system sessions, building jobs for
trusted nodes). Error messages and audit events never contain secret values.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sentinel import audit
from sentinel.config import Settings
from sentinel.db.base import utcnow
from sentinel.db.session import Database
from sentinel.models import Credential, CredentialKind, OrgDataKey, Source
from sentinel.models.credentials import SECRET_FIELDS
from sentinel.security.addresses import parse_target
from sentinel.security.errors import OutboundError
from sentinel.security.vault import Vault, VaultError
from sentinel.tenancy.roles import Action
from sentinel.tenancy.scope import TenantScope

MAX_SECRET = 4096
_HEADER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,63}$")
# headers a credential may never set: transport, identity of our bot, proxies
FORBIDDEN_HEADERS = frozenset(
    {
        "host",
        "cookie",
        "set-cookie",
        "content-length",
        "content-type",
        "transfer-encoding",
        "connection",
        "upgrade",
        "te",
        "trailer",
        "user-agent",
        "accept-encoding",
        "proxy-authorization",
        "forwarded",
        "x-forwarded-for",
        "x-forwarded-host",
    }
)


class CredentialError(Exception):
    def __init__(self, key: str) -> None:
        super().__init__(key)
        self.key = key


@dataclass(frozen=True)
class CredentialInfo:
    id: uuid.UUID
    name: str
    kind: str
    host: str
    header_name: str | None
    token_url: str | None
    created_at: datetime
    secret_set_at: datetime
    last_used_at: datetime | None
    sources: int


_LOCAL_SUFFIXES = (".localhost", ".local", ".internal", ".lan", ".home.arpa")


def public_host(raw: str) -> str:
    """A public host name: dotted, not local-only (resolution is guarded again later)."""
    host = raw.strip().lower().rstrip(".")
    if "." not in host or host == "localhost" or host.endswith(_LOCAL_SUFFIXES):
        raise CredentialError("credentials.invalid_host")
    try:
        target = parse_target(f"https://{host}/")
    except OutboundError:
        raise CredentialError("credentials.invalid_host") from None
    if target.host != host:
        raise CredentialError("credentials.invalid_host")
    return host


def _secret(kind: CredentialKind, values: dict[str, str]) -> dict[str, str]:
    secret = {}
    for name in SECRET_FIELDS[kind]:
        value = values.get(name, "")
        if not value or len(value) > MAX_SECRET or any(c in value for c in "\r\n\x00"):
            raise CredentialError("credentials.invalid_secret")
        secret[name] = value
    return secret


class CredentialService:
    def __init__(
        self, db: Database, settings: Settings, clock: Callable[[], datetime] = utcnow
    ) -> None:
        self._db = db
        self._vault = Vault(settings.credentials_master_key.get_secret_value())
        self._clock = clock

    async def _data_key(self, s: AsyncSession, org_id: uuid.UUID) -> bytes:
        row = await s.get(OrgDataKey, org_id)
        if row is None:
            key = self._vault.new_data_key()
            s.add(OrgDataKey(org_id=org_id, wrapped=self._vault.wrap(key, org_id)))
            await s.flush()
            return key
        return self._vault.unwrap(row.wrapped, org_id)

    async def list(self, scope: TenantScope) -> list[CredentialInfo]:
        scope.require(Action.CREDENTIALS_MANAGE)
        async with self._db.tenant(scope) as s:
            rows = (await s.scalars(select(Credential).order_by(Credential.name))).all()
            used: dict[uuid.UUID, int] = {}
            for credential_id in (
                await s.scalars(
                    select(Source.credential_id).where(Source.credential_id.is_not(None))
                )
            ).all():
                if credential_id is not None:
                    used[credential_id] = used.get(credential_id, 0) + 1
        return [
            CredentialInfo(
                c.id,
                c.name,
                c.kind,
                c.host,
                c.header_name,
                c.token_url,
                c.created_at,
                c.secret_set_at,
                c.last_used_at,
                used.get(c.id, 0),
            )
            for c in rows
        ]

    async def create(
        self,
        scope: TenantScope,
        *,
        name: str,
        kind: str,
        host: str,
        secret: dict[str, str],
        entitled: bool,
        header_name: str | None = None,
        token_url: str | None = None,
        oauth_scope: str | None = None,
        ip: str = "",
    ) -> uuid.UUID:
        scope.require(Action.CREDENTIALS_MANAGE)
        if not entitled:
            raise CredentialError("credentials.confirm_entitled")
        name = " ".join(name.split())
        if not 1 <= len(name) <= 80:
            raise CredentialError("credentials.invalid_name")
        try:
            credential_kind = CredentialKind(kind)
        except ValueError:
            raise CredentialError("credentials.invalid_kind") from None
        bound = public_host(host)
        if credential_kind is CredentialKind.API_KEY_HEADER:
            header = (header_name or "").strip()
            if not _HEADER.match(header) or header.lower() in FORBIDDEN_HEADERS:
                raise CredentialError("credentials.invalid_header")
            header_name = header
        else:
            header_name = None
        if credential_kind is CredentialKind.OAUTH2_CLIENT:
            try:
                token_url = str(parse_target((token_url or "").strip()).url)
            except OutboundError:
                raise CredentialError("credentials.invalid_token_url") from None
        else:
            token_url, oauth_scope = None, None
        values = _secret(credential_kind, secret)
        credential_id = uuid.uuid4()
        async with self._db.tenant(scope) as s:
            if await s.scalar(select(Credential.id).where(Credential.name == name)):
                raise CredentialError("credentials.name_taken")
            key = await self._data_key(s, scope.org_id)
            s.add(
                Credential(
                    id=credential_id,
                    org_id=scope.org_id,
                    name=name,
                    kind=credential_kind,
                    host=bound,
                    header_name=header_name,
                    token_url=token_url,
                    scope=(oauth_scope or "").strip()[:500] or None,
                    sealed=self._vault.seal_secret(key, values, scope.org_id, credential_id),
                    created_by=scope.user_id,
                )
            )
            audit.record(
                s,
                "credential.created",
                org_id=scope.org_id,
                actor_user_id=scope.user_id,
                target_type="credential",
                target_id=credential_id,
                details={"kind": credential_kind.value, "host": bound},
                ip=ip,
            )
        return credential_id

    async def replace_secret(
        self, scope: TenantScope, credential_id: uuid.UUID, secret: dict[str, str], *, ip: str = ""
    ) -> None:
        scope.require(Action.CREDENTIALS_MANAGE)
        async with self._db.tenant(scope) as s:
            credential = await s.get(Credential, credential_id)
            if credential is None:
                raise CredentialError("not_found")
            values = _secret(CredentialKind(credential.kind), secret)
            key = await self._data_key(s, scope.org_id)
            credential.sealed = self._vault.seal_secret(key, values, scope.org_id, credential_id)
            credential.secret_set_at = self._clock()
            audit.record(
                s,
                "credential.rotated",
                org_id=scope.org_id,
                actor_user_id=scope.user_id,
                target_type="credential",
                target_id=credential_id,
                ip=ip,
            )

    async def delete(self, scope: TenantScope, credential_id: uuid.UUID, *, ip: str = "") -> None:
        scope.require(Action.CREDENTIALS_MANAGE)
        async with self._db.tenant(scope) as s:
            credential = await s.get(Credential, credential_id)
            if credential is None:
                raise CredentialError("not_found")
            await s.delete(credential)  # sources keep working, unauthenticated (SET NULL)
            audit.record(
                s,
                "credential.deleted",
                org_id=scope.org_id,
                actor_user_id=scope.user_id,
                target_type="credential",
                target_id=credential_id,
                ip=ip,
            )

    async def attach(
        self,
        scope: TenantScope,
        source_id: uuid.UUID,
        credential_id: uuid.UUID | None,
        *,
        ip: str = "",
    ) -> None:
        """Use ``credential_id`` for ``source_id`` (None detaches)."""
        scope.require(Action.CREDENTIALS_MANAGE)
        async with self._db.tenant(scope) as s:
            source = await s.get(Source, source_id)
            if source is None:
                raise CredentialError("not_found")
            if source.org_id is None:
                raise CredentialError("credentials.shared_source")
            if credential_id is not None:
                credential = await s.get(Credential, credential_id)
                if credential is None:
                    raise CredentialError("not_found")
                url = str(source.config.get("url") or "")
                try:
                    host = parse_target(url, allow_http=source.allow_http).host if url else ""
                except OutboundError:
                    host = ""
                if host != credential.host:
                    raise CredentialError("credentials.host_mismatch")
            source.credential_id = credential_id
            audit.record(
                s,
                "credential.attached" if credential_id else "credential.detached",
                org_id=scope.org_id,
                actor_user_id=scope.user_id,
                target_type="source",
                target_id=source_id,
                details={"credential": str(credential_id) if credential_id else None},
                ip=ip,
            )

    async def secret_for_job(self, s: AsyncSession, credential: Credential) -> dict[str, str]:
        """Open a secret to seal it for a trusted node. System sessions only."""
        row = await s.get(OrgDataKey, credential.org_id)
        if row is None:
            raise VaultError("no data key")
        key = self._vault.unwrap(row.wrapped, credential.org_id)
        return self._vault.open_secret(key, credential.sealed, credential.org_id, credential.id)

    async def seal_payload(
        self, s: AsyncSession, org_id: uuid.UUID, row_id: uuid.UUID, payload: dict[str, str]
    ) -> bytes:
        """Seal another secret of the organization (alert channel URLs, signing secrets)."""
        key = await self._data_key(s, org_id)
        return self._vault.seal_secret(key, payload, org_id, row_id)

    async def open_payload(
        self, s: AsyncSession, org_id: uuid.UUID, row_id: uuid.UUID, blob: bytes
    ) -> dict[str, str]:
        row = await s.get(OrgDataKey, org_id)
        if row is None:
            raise VaultError("no data key")
        key = self._vault.unwrap(row.wrapped, org_id)
        return self._vault.open_secret(key, blob, org_id, row_id)
