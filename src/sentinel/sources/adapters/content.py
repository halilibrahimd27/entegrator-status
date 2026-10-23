"""What every change-source adapter does before it parses anything.

robots.txt every cycle (ADR 0003), a conditional GET with the job's validators, and the
same outcome for every non-200 answer. ``fetch_content`` returns either the response
to parse or the outcome to report.
"""

from __future__ import annotations

from sentinel.security.http import (
    PAGE_MAX_BYTES,
    BoundCredential,
    FetchPolicy,
    FetchResult,
    Timeouts,
)
from sentinel.sources.adapters.base import AdapterEnv, ProbeJob, ProbeOutcome, retry_after_seconds
from sentinel.sources.snapshots import make_snapshot


def conditional_headers(job: ProbeJob, accept: str) -> dict[str, str]:
    headers = {"Accept": accept}
    if etag := job.validators.get("etag"):
        headers["If-None-Match"] = etag
    if modified := job.validators.get("last_modified"):
        headers["If-Modified-Since"] = modified
    return headers


async def robots_refusal(job: ProbeJob, env: AdapterEnv, url: str) -> ProbeOutcome | None:
    decision = await env.robots.check(url, allow_http=job.allow_http)
    if decision.allowed:
        return None
    return ProbeOutcome(False, "blocked_by_robots", details={"robots": decision.reason})


async def fetch_content(
    job: ProbeJob,
    env: AdapterEnv,
    url: str,
    accept: str,
    *,
    max_bytes: int = PAGE_MAX_BYTES,
    conditional: bool = True,
    credential: BoundCredential | None = None,
) -> FetchResult | ProbeOutcome:
    if refusal := await robots_refusal(job, env, url):
        return refusal
    timeout = float(job.timeout_s)
    policy = FetchPolicy(
        allow_http=job.allow_http,
        max_bytes=max_bytes,
        timeouts=Timeouts(connect_s=min(5.0, timeout), read_s=timeout, total_s=timeout * 2),
        headers=conditional_headers(job, accept) if conditional else {"Accept": accept},
        credential=credential,
    )
    result = await env.http.fetch(url, policy)
    latency = int(result.elapsed_s * 1000)
    if result.status_code == 304:
        return ProbeOutcome(True, None, 304, latency, {"unchanged": True})
    if result.status_code != 200:
        status = result.status_code
        error = "http_5xx" if status >= 500 else "http_4xx" if status >= 400 else "assertion"
        retry = (
            retry_after_seconds(result.headers.get("retry-after"), env.now())
            if status in (429, 503)
            else None
        )
        return ProbeOutcome(False, error, status, latency, retry_after_s=retry)
    return result


def snapshot_outcome(result: FetchResult, items: list[dict[str, object]]) -> ProbeOutcome:
    latency = int(result.elapsed_s * 1000)
    if not items:
        return ProbeOutcome(False, "no_items", 200, latency)
    snapshot = make_snapshot(
        items, etag=result.headers.get("etag"), last_modified=result.headers.get("last-modified")
    )
    return ProbeOutcome(True, None, 200, latency, {"items": len(items)}, snapshot=snapshot)


def parse_failure(result: FetchResult, reason: str) -> ProbeOutcome:
    return ProbeOutcome(
        False, "parse_error", 200, int(result.elapsed_s * 1000), {"reason": reason[:200]}
    )
