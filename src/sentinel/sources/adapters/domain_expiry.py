"""``domain_expiry``: registration expiry from RDAP (IANA bootstrap → registry server)."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from sentinel.models.catalog import SourceKind
from sentinel.security.http import FetchPolicy, Timeouts
from sentinel.sources.adapters.base import Adapter, AdapterEnv, ProbeJob, ProbeOutcome

BOOTSTRAP_URL = "https://data.iana.org/rdap/dns.json"
BOOTSTRAP_TTL_S = 24 * 3600
_POLICY = FetchPolicy(max_bytes=1024 * 1024, timeouts=Timeouts(5, 15, 30))


class DomainExpiryAdapter(Adapter):
    kind = SourceKind.DOMAIN_EXPIRY

    def __init__(self) -> None:
        self._bootstrap: dict[str, str] = {}
        self._bootstrap_until = 0.0

    async def _rdap_base(self, tld: str, env: AdapterEnv) -> str | None:
        if env.monotonic() >= self._bootstrap_until:
            result = await env.http.fetch(BOOTSTRAP_URL, _POLICY)
            data: dict[str, Any] = json.loads(result.body)
            self._bootstrap = {
                suffix.lower(): urls[0].rstrip("/") + "/"
                for suffixes, urls in data.get("services", [])
                for suffix in suffixes
                if urls
            }
            self._bootstrap_until = env.monotonic() + BOOTSTRAP_TTL_S
        return self._bootstrap.get(tld)

    async def observe(self, job: ProbeJob, env: AdapterEnv) -> ProbeOutcome:
        domain = str(job.config["domain"]).lower().rstrip(".")
        base = await self._rdap_base(domain.rsplit(".", 1)[-1], env)
        if base is None:
            return ProbeOutcome(
                False, "unsupported", details={"reason": "no RDAP server for this TLD"}
            )
        started = env.monotonic()
        result = await env.http.fetch(f"{base}domain/{domain}", _POLICY)
        latency = int((env.monotonic() - started) * 1000)
        if result.status_code != 200:
            error = "http_5xx" if result.status_code >= 500 else "http_4xx"
            return ProbeOutcome(False, error, result.status_code, latency)
        events = json.loads(result.body).get("events", [])
        expiry = next(
            (e.get("eventDate") for e in events if e.get("eventAction") == "expiration"), None
        )
        if not expiry:
            return ProbeOutcome(
                False, "unsupported", 200, latency, {"reason": "registry publishes no expiry"}
            )
        when = datetime.fromisoformat(str(expiry).replace("Z", "+00:00"))
        days_left = (when - env.now()).days
        details = {
            "expires_at": when.isoformat(),
            "days_left": days_left,
            "expiring_soon": days_left <= int(job.config.get("warn_days", 30)),
        }
        if days_left < 0:
            return ProbeOutcome(False, "assertion", 200, latency, {**details, "reason": "expired"})
        return ProbeOutcome(True, None, 200, latency, details)
