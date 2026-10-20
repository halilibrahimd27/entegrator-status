"""Core side of the probe protocol: node registration, job lists and result ingest."""

from __future__ import annotations

import hashlib
import hmac
import json
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Annotated, Any, Literal, cast, get_args

from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import CursorResult, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError

from sentinel import audit
from sentinel.accounts.tokens import digest, new_token
from sentinel.config import Settings
from sentinel.db.base import utcnow
from sentinel.db.session import Database
from sentinel.models import IngestBatch, Observation, ProbeNode, Source, SourceKind
from sentinel.probe.signing import node_signing_key
from sentinel.sources.config import probe_config

# Kinds the prober runs today; grows phase by phase as adapters arrive.
PROBED_KINDS = frozenset(
    {SourceKind.HTTP, SourceKind.TLS_CERT, SourceKind.DNS, SourceKind.DOMAIN_EXPIRY}
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


def job_key(source: Source) -> str:
    identity = {
        "kind": source.kind,
        "config": probe_config(source.config),
        "allow_http": source.allow_http,
        "ignores_robots": source.availability_ignores_robots,
        "timeout_s": source.timeout_s,
    }
    return hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()[:32]


class NodeService:
    def __init__(
        self, db: Database, settings: Settings, clock: Callable[[], datetime] = utcnow
    ) -> None:
        self._db = db
        self._token_key = settings.token_hash_key.get_secret_value()
        self._ingest_key = settings.ingest_hmac_key.get_secret_value()
        self._clock = clock

    def signing_key(self, node_id: uuid.UUID) -> str:
        return node_signing_key(self._ingest_key, node_id)

    async def register(self, name: str, location: str) -> NodeCredentials:
        token = new_token()
        async with self._db.system("register a probe node") as s:
            node = ProbeNode(
                name=name.strip()[:63],
                location=location.strip()[:120],
                token_hash=digest(self._token_key, token, purpose=_PURPOSE),
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

    async def jobs(self) -> list[Job]:
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
        grouped: dict[str, list[Source]] = {}
        for source in sources:
            grouped.setdefault(job_key(source), []).append(source)
        return [
            Job(
                key=key,
                kind=group[0].kind,
                config=probe_config(group[0].config),
                interval_s=min(src.interval_s for src in group),
                timeout_s=group[0].timeout_s,
                allow_http=group[0].allow_http,
                ignores_robots=group[0].availability_ignores_robots,
                source_ids=tuple(str(src.id) for src in group),
            )
            for key, group in grouped.items()
        ]

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
            if wanted:
                found = await s.execute(
                    select(Source.id, Source.org_id).where(Source.id.in_(wanted))
                )
                owners = {row.id: row.org_id for row in found}
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
            node = await s.get(ProbeNode, node_id)
            if node is not None:
                node.last_seen_at = now
                if payload.results:
                    node.healthy = payload.results[-1].node_healthy
            return inserted
