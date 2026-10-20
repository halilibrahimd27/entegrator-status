"""``http``: status, latency, optional body assertion, deprecation and sunset headers."""

from __future__ import annotations

from typing import Any

from sentinel.models.catalog import SourceKind
from sentinel.security.http import PAGE_MAX_BYTES, FetchPolicy, Timeouts
from sentinel.sources.adapters.base import (
    Adapter,
    AdapterEnv,
    ProbeJob,
    ProbeOutcome,
    retry_after_seconds,
)


class HttpAdapter(Adapter):
    kind = SourceKind.HTTP

    async def observe(self, job: ProbeJob, env: AdapterEnv) -> ProbeOutcome:
        config = job.config
        url = str(config["url"])
        if not job.ignores_robots:
            decision = await env.robots.check(url, allow_http=job.allow_http)
            if not decision.allowed:
                return ProbeOutcome(
                    ok=False, error_class="blocked_by_robots", details={"robots": decision.reason}
                )
        timeout = float(job.timeout_s)
        policy = FetchPolicy(
            method=config.get("method", "GET"),
            allow_http=job.allow_http,
            max_bytes=PAGE_MAX_BYTES,
            timeouts=Timeouts(connect_s=min(5.0, timeout), read_s=timeout, total_s=timeout * 2),
        )
        result = await env.http.fetch(url, policy)
        latency = int(result.elapsed_s * 1000)
        details: dict[str, Any] = {}
        announced = "deprecation" in result.headers or "sunset" in result.headers
        for header in ("deprecation", "sunset", "link"):
            if header in result.headers and (header != "link" or announced):
                details[header] = result.headers[header][:300]
        if result.redirects:
            details["redirects"] = len(result.redirects)
        expected = config.get("expected_status") or [200]
        status = result.status_code
        if status not in expected:
            error = "http_5xx" if status >= 500 else "http_4xx" if status >= 400 else "assertion"
            retry = (
                retry_after_seconds(result.headers.get("retry-after"), env.now())
                if status in (429, 503)
                else None
            )
            return ProbeOutcome(False, error, status, latency, details, retry_after_s=retry)
        needle = config.get("body_must_contain")
        if needle and needle.encode() not in result.body:
            return ProbeOutcome(
                False, "assertion", status, latency, {**details, "assertion": "body_must_contain"}
            )
        return ProbeOutcome(True, None, status, latency, details)
