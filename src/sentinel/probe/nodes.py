"""Core side of the probe protocol: node registration, job lists and result ingest."""

from __future__ import annotations

import hashlib
import hmac
import json
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Annotated, Any, Literal, cast, get_args

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import CursorResult, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from sentinel import audit
from sentinel.accounts.tokens import digest, new_token
from sentinel.config import Settings
from sentinel.db.base import utcnow
from sentinel.db.session import Database
from sentinel.models import Credential, IngestBatch, Observation, ProbeNode, Source, SourceKind
from sentinel.probe import content
from sentinel.probe.signing import node_signing_key
from sentinel.security.vault import seal_for_node
from sentinel.sources.config import probe_config
from sentinel.sources.snapshots import (
    MAX_ITEMS,
    MAX_SNAPSHOT_BYTES,
    canonical,
    safe_validator,
    snapshot_hash,
)
from sentinel.tenancy.credentials import CredentialService

# Kinds the prober runs today; grows phase by phase as adapters arrive.
PROBED_KINDS = (
    frozenset({SourceKind.HTTP, SourceKind.TLS_CERT, SourceKind.DNS, SourceKind.DOMAIN_EXPIRY})
    | content.CONTENT_KINDS
)
ErrorClass = Literal[
    "dns",
    "connect",
    "connect_timeout",
    "read_timeout",
    "tls",
    "http_4xx",
    "http_5xx",
    "assertion",
    "blocked_by_robots",
    "rate_limited_by_us",
    "blocked_url",
    "blocked_address",
    "too_many_redirects",
    "response_too_large",
    "parse_error",
    "no_items",
    "auth_failed",
    "unsupported",
    "error",
]
ERROR_CLASSES: tuple[str, ...] = get_args(ErrorClass)
MAX_RESULTS = 2000
MAX_DETAILS_BYTES = 4096
MAX_AGE = timedelta(days=7)
MAX_AHEAD = timedelta(minutes=5)
_PURPOSE = "probe-node"


class ReplayError(Exception):
    """The batch id was seen before."""


class SnapshotIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    hash: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    items: list[dict[str, Any]] = Field(max_length=MAX_ITEMS)
    validators: dict[str, str] = {}

    @model_validator(mode="after")
    def _consistent(self) -> SnapshotIn:
        if len(canonical(self.items)) > MAX_SNAPSHOT_BYTES:
            raise ValueError(f"snapshot larger than {MAX_SNAPSHOT_BYTES} bytes")
        if snapshot_hash(self.items) != self.hash:
            raise ValueError("snapshot hash does not match its items")
        if set(self.validators) - {"etag", "last_modified"} or any(
            safe_validator(v) != v for v in self.validators.values()
        ):
            raise ValueError("invalid validators")
        return self


class IngestResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID
    source_ids: list[uuid.UUID] = Field(min_length=1, max_length=1000)
    observed_at: datetime
    ok: bool
    error_class: ErrorClass | None = None
    status_code: Annotated[int, Field(ge=100, le=599)] | None = None
    latency_ms: Annotated[int, Field(ge=0, le=600_000)] | None = None
    node_healthy: bool = True
    details: dict[str, Any] = {}
    snapshot: SnapshotIn | None = None

    @field_validator("observed_at")
    @classmethod
    def _aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("must include a time zone")
        return value

    @field_validator("details")
    @classmethod
    def _small(cls, value: dict[str, Any]) -> dict[str, Any]:
        if len(json.dumps(value, default=str)) > MAX_DETAILS_BYTES:
            raise ValueError(f"larger than {MAX_DETAILS_BYTES} bytes")
        return value


class IngestPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    results: list[IngestResult] = Field(max_length=MAX_RESULTS)


@dataclass(frozen=True)
class NodeCredentials:
    node_id: uuid.UUID
    token: str
    signing_key: str


@dataclass(frozen=True)
class Job:
    """One probe for one or more identical sources (fanned out on ingest)."""

    key: str
    kind: str
    config: dict[str, Any]
    interval_s: int
    timeout_s: int
    allow_http: bool
    ignores_robots: bool
    source_ids: tuple[str, ...]
    validators: dict[str, str]
    auth: str | None = field(default=None, repr=False)  # sealed for the requesting node


def job_key(source: Source) -> str:
    identity = {
        "kind": source.kind,
        "config": probe_config(source.config),
        "allow_http": source.allow_http,
        "ignores_robots": source.availability_ignores_robots,
        "timeout_s": source.timeout_s,
        # an organization's credential makes the job its own (never grouped across orgs)
        "credential": str(source.credential_id) if source.credential_id else None,
    }
    return hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()[:32]


class NodeService:
    def __init__(
        self, db: Database, settings: Settings, clock: Callable[[], datetime] = utcnow
    ) -> None:
        self._db = db
        self._token_key = settings.token_hash_key.get_secret_value()
        self._ingest_key = settings.ingest_hmac_key.get_secret_value()
        self._vault = CredentialService(db, settings, clock)
        self._clock = clock

    def signing_key(self, node_id: uuid.UUID) -> str:
        return node_signing_key(self._ingest_key, node_id)

    async def register(
        self, name: str, location: str, *, credentials: bool = False
    ) -> NodeCredentials:
        token = new_token()
        async with self._db.system("register a probe node") as s:
            node = ProbeNode(
                name=name.strip()[:63],
                location=location.strip()[:120],
                token_hash=digest(self._token_key, token, purpose=_PURPOSE),
                handles_credentials=credentials,
            )
            s.add(node)
            try:
                await s.flush()
            except IntegrityError:
                raise ValueError(f"a node named {name!r} already exists") from None
            audit.record(s, "probe.node_registered", target_type="probe_node", target_id=node.id)
            return NodeCredentials(node.id, token, self.signing_key(node.id))

    async def authenticate(self, node_id: uuid.UUID, token: str) -> bool:
        async with self._db.system("authenticate a probe node") as s:
            node = await s.get(ProbeNode, node_id)
            expected = digest(self._token_key, token, purpose=_PURPOSE)
            return (
                node is not None and node.enabled and hmac.compare_digest(node.token_hash, expected)
            )

    async def jobs(self, node_id: uuid.UUID | None = None) -> list[Job]:
        """Availability jobs for every node; each change source for one node only.

        ``node_id`` None lists everything (operators, tests).
        """
        now = self._clock()
        async with self._db.system("prober: list enabled sources of every organization") as s:
            sources = (
                await s.scalars(
                    select(Source)
                    .where(
                        Source.enabled.is_(True), Source.kind.in_([k.value for k in PROBED_KINDS])
                    )
                    .order_by(Source.created_at)
                )
            ).all()
            active = set(
                (
                    await s.scalars(
                        select(ProbeNode.id).where(
                            ProbeNode.enabled.is_(True),
                            ProbeNode.last_seen_at > now - content.NODE_FRESH,
                        )
                    )
                ).all()
            )
            validators = await content.latest_validators(
                s, [src.id for src in sources if src.kind in content.CONTENT_KINDS]
            )
            trusted = set(
                (
                    await s.scalars(
                        select(ProbeNode.id).where(
                            ProbeNode.id.in_(active | ({node_id} if node_id else set())),
                            ProbeNode.handles_credentials.is_(True),
                        )
                    )
                ).all()
            )
            sealed = await self._sealed(s, sources, node_id) if node_id in trusted else {}
        if node_id is not None:
            active.add(node_id)
        grouped: dict[str, list[Source]] = {}
        for source in sources:
            grouped.setdefault(job_key(source), []).append(source)
        jobs = []
        for key, group in grouped.items():
            changes = group[0].kind in content.CONTENT_KINDS
            authenticated = group[0].credential_id is not None
            if authenticated and node_id is not None and node_id not in trusted:
                continue  # credentials only ever reach trusted nodes (ADR 0011)
            fetchers = trusted if authenticated else active
            if changes and node_id is not None and content.owner_of(key, fetchers) != node_id:
                continue
            seen = next((validators[s.id] for s in group if validators.get(s.id)), {})
            jobs.append(
                Job(
                    key=key,
                    kind=group[0].kind,
                    config=probe_config(group[0].config),
                    interval_s=min(src.interval_s for src in group),
                    timeout_s=group[0].timeout_s,
                    allow_http=group[0].allow_http,
                    # robots.txt is optional only for availability (ADR 0003)
                    ignores_robots=group[0].availability_ignores_robots and not changes,
                    source_ids=tuple(str(src.id) for src in group),
                    validators=seen,
                    auth=sealed.get(key) if authenticated else None,
                )
            )
        return jobs

    async def _sealed(
        self, s: AsyncSession, sources: Sequence[Source], node_id: uuid.UUID | None
    ) -> dict[str, str]:
        """Each authenticated job's secret, sealed for ``node_id`` alone."""
        if node_id is None:
            return {}
        found: dict[str, str] = {}
        key = self.signing_key(node_id)
        for source in sources:
            if source.credential_id is None:
                continue
            credential = await s.get(Credential, source.credential_id)
            if credential is None:
                continue
            job = job_key(source)
            payload = {
                "kind": credential.kind,
                "host": credential.host,
                "header_name": credential.header_name,
                "token_url": credential.token_url,
                "scope": credential.scope,
                "secret": await self._vault.secret_for_job(s, credential),
            }
            found[job] = seal_for_node(key, job, payload)
            credential.last_used_at = self._clock()
        return found

    async def ingest(self, node_id: uuid.UUID, batch_id: uuid.UUID, payload: IngestPayload) -> int:
        now = self._clock()
        for result in payload.results:
            if not now - MAX_AGE <= result.observed_at <= now + MAX_AHEAD:
                raise ValueError(f"result {result.id}: observed_at outside the accepted window")
        async with self._db.system("ingest probe results") as s:
            s.add(IngestBatch(id=batch_id, node_id=node_id, results=len(payload.results)))
            try:
                await s.flush()
            except IntegrityError:
                raise ReplayError(str(batch_id)) from None
            wanted = {sid for r in payload.results for sid in r.source_ids}
            owners: dict[uuid.UUID, uuid.UUID | None] = {}
            kinds: dict[uuid.UUID, tuple[uuid.UUID | None, str]] = {}
            if wanted:
                found = await s.execute(
                    select(Source.id, Source.org_id, Source.kind).where(Source.id.in_(wanted))
                )
                for row in found:
                    owners[row.id] = row.org_id
                    kinds[row.id] = (row.org_id, row.kind)
            rows = [
                {
                    "id": uuid.uuid5(result.id, str(source_id)),
                    "org_id": owners[source_id],
                    "source_id": source_id,
                    "node_id": node_id,
                    "observed_at": result.observed_at,
                    "ok": result.ok,
                    "error_class": result.error_class,
                    "status_code": result.status_code,
                    "latency_ms": result.latency_ms,
                    "node_healthy": result.node_healthy,
                    "details": result.details,
                }
                for result in payload.results
                for source_id in result.source_ids
                if source_id in owners  # sources deleted since the job list are skipped
            ]
            inserted = 0
            if rows:
                statement = (
                    insert(Observation).values(rows).on_conflict_do_nothing(index_elements=["id"])
                )
                result_proxy = cast(CursorResult[Any], await s.execute(statement))
                inserted = int(result_proxy.rowcount or 0)
            await content.record(s, payload.results, kinds, now)
            node = await s.get(ProbeNode, node_id)
            if node is not None:
                node.last_seen_at = now
                if payload.results:
                    node.healthy = payload.results[-1].node_healthy
            return inserted
