"""``dns``: the name resolves to at least one record of the requested type."""

from __future__ import annotations

from collections.abc import Sequence

import dns.asyncresolver
import dns.exception
import dns.resolver

from sentinel.models.catalog import SourceKind
from sentinel.security.errors import DNSResolutionError
from sentinel.sources.adapters.base import Adapter, AdapterEnv, ProbeJob, ProbeOutcome


async def system_dns(host: str, record_type: str, timeout_s: float) -> Sequence[str]:
    """Query the system's configured resolvers (dnspython). Not a request to the target."""
    resolver = dns.asyncresolver.Resolver()
    resolver.lifetime = timeout_s
    try:
        answer = await resolver.resolve(host, record_type)
    except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer, dns.resolver.NoNameservers) as exc:
        raise DNSResolutionError(type(exc).__name__) from exc
    except dns.exception.Timeout as exc:
        raise DNSResolutionError("timeout") from exc
    return [record.to_text() for record in answer]


class DnsAdapter(Adapter):
    kind = SourceKind.DNS

    async def observe(self, job: ProbeJob, env: AdapterEnv) -> ProbeOutcome:
        host = str(job.config["host"])
        record_type = str(job.config.get("record_type", "A"))
        started = env.monotonic()
        answers = list(await env.dns(host, record_type, float(job.timeout_s)))
        latency = int((env.monotonic() - started) * 1000)
        details = {
            "record_type": record_type,
            "answers": len(answers),
            "values": sorted(answers)[:10],
        }
        if not answers:
            return ProbeOutcome(False, "dns", None, latency, details)
        return ProbeOutcome(True, None, None, latency, details)
