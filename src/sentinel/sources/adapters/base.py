"""The adapter contract.

``run`` turns one job into one outcome and never raises: every failure becomes an error
class. Adapters reach the network only through ``AdapterEnv`` (the SSRF-guarded HTTP
client, the guarded TLS reader, the DNS resolver), which tests replace with fakes.
Change sources (Phase 5+) extend this with snapshots and diffs.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, ClassVar

from sentinel.models.catalog import SourceKind
from sentinel.probe.robots import RobotsCache
from sentinel.security.errors import OutboundError
from sentinel.security.http import OutboundClient
from sentinel.security.tls import CertInfo

TlsReader = Callable[[str, int, float], Awaitable[CertInfo]]
DnsLookup = Callable[[str, str, float], Awaitable[Sequence[str]]]


@dataclass(frozen=True)
class ProbeJob:
    key: str
    kind: str
    config: dict[str, Any]
    interval_s: int
    timeout_s: int
    allow_http: bool
    ignores_robots: bool
    source_ids: tuple[str, ...]

    @classmethod
    def from_wire(cls, data: dict[str, Any]) -> ProbeJob:
        return cls(
            key=str(data["key"]),
            kind=str(data["kind"]),
            config=dict(data["config"]),
            interval_s=int(data["interval_s"]),
            timeout_s=int(data["timeout_s"]),
            allow_http=bool(data["allow_http"]),
            ignores_robots=bool(data["ignores_robots"]),
            source_ids=tuple(str(s) for s in data["source_ids"]),
        )

    @property
    def host(self) -> str:
        """The host the job contacts, for per-host limits."""
        from urllib.parse import urlsplit

        for key in ("url",):
            if key in self.config:
                return (urlsplit(str(self.config[key])).hostname or "").lower()
        return str(self.config.get("host") or self.config.get("domain") or "").lower()


@dataclass(frozen=True)
class ProbeOutcome:
    ok: bool
    error_class: str | None = None
    status_code: int | None = None
    latency_ms: int | None = None
    details: dict[str, Any] = field(default_factory=dict)
    retry_after_s: float | None = None  # host asked us to slow down


@dataclass
class AdapterEnv:
    http: OutboundClient
    robots: RobotsCache
    tls: TlsReader
    dns: DnsLookup
    now: Callable[[], datetime] = lambda: datetime.now(UTC)
    monotonic: Callable[[], float] = time.monotonic


class Adapter(ABC):
    kind: ClassVar[SourceKind]

    async def run(self, job: ProbeJob, env: AdapterEnv) -> ProbeOutcome:
        try:
            return await self.observe(job, env)
        except OutboundError as exc:
            return ProbeOutcome(ok=False, error_class=exc.kind, details={"reason": str(exc)[:200]})
        except Exception as exc:  # an adapter bug must not kill the prober
            return ProbeOutcome(
                ok=False, error_class="error", details={"reason": type(exc).__name__}
            )

    @abstractmethod
    async def observe(self, job: ProbeJob, env: AdapterEnv) -> ProbeOutcome: ...


REGISTRY: dict[SourceKind, Adapter] = {}


def adapter_for(kind: str) -> Adapter | None:
    try:
        return REGISTRY.get(SourceKind(kind))
    except ValueError:
        return None


def retry_after_seconds(value: str | None, now: datetime) -> float | None:
    """``Retry-After`` as seconds (delta or HTTP date), capped at one hour."""
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        return min(float(value), 3600.0)
    from email.utils import parsedate_to_datetime

    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return max(0.0, min((when - now).total_seconds(), 3600.0))
