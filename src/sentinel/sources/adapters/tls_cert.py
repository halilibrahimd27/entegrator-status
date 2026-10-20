"""``tls_cert``: days to expiry, issuer and chain validity of a server certificate."""

from __future__ import annotations

from sentinel.models.catalog import SourceKind
from sentinel.sources.adapters.base import Adapter, AdapterEnv, ProbeJob, ProbeOutcome


class TlsCertAdapter(Adapter):
    kind = SourceKind.TLS_CERT

    async def observe(self, job: ProbeJob, env: AdapterEnv) -> ProbeOutcome:
        host = str(job.config["host"])
        port = int(job.config.get("port", 443))
        started = env.monotonic()
        cert = await env.tls(host, port, float(job.timeout_s))
        latency = int((env.monotonic() - started) * 1000)
        days_left = cert.days_left(env.now())
        details = {
            "days_left": days_left,
            "not_after": cert.not_after.isoformat(),
            "issuer": cert.issuer,
            "verified": cert.verified,
            "expiring_soon": days_left <= int(job.config.get("warn_days", 21)),
        }
        if not cert.verified:
            return ProbeOutcome(
                False, "tls", None, latency, {**details, "reason": (cert.problem or "")[:200]}
            )
        if days_left < 0:
            return ProbeOutcome(False, "tls", None, latency, {**details, "reason": "expired"})
        return ProbeOutcome(True, None, None, latency, details)
