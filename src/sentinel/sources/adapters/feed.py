"""``feed``: RSS / Atom announcements, fetched conditionally (``ETag``, ``If-Modified-Since``).

Content watchers always honour robots.txt, every cycle (ADR 0003): a disallowed feed
is not fetched and the source shows why.
"""

from __future__ import annotations

from sentinel.models.catalog import SourceKind
from sentinel.security.http import PAGE_MAX_BYTES, FetchPolicy, Timeouts
from sentinel.sources.adapters.base import (
    Adapter,
    AdapterEnv,
    ProbeJob,
    ProbeOutcome,
    retry_after_seconds,
)
from sentinel.sources.feeds import FeedError, parse_feed
from sentinel.sources.snapshots import make_snapshot

ACCEPT = "application/rss+xml, application/atom+xml, application/xml;q=0.9, text/xml;q=0.8"


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


class FeedAdapter(Adapter):
    kind = SourceKind.FEED

    async def observe(self, job: ProbeJob, env: AdapterEnv) -> ProbeOutcome:
        url = str(job.config["url"])
        if refusal := await robots_refusal(job, env, url):
            return refusal
        timeout = float(job.timeout_s)
        policy = FetchPolicy(
            allow_http=job.allow_http,
            max_bytes=PAGE_MAX_BYTES,
            timeouts=Timeouts(connect_s=min(5.0, timeout), read_s=timeout, total_s=timeout * 2),
            headers=conditional_headers(job, ACCEPT),
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
        try:
            items = parse_feed(result.body, base=result.url)
        except FeedError as exc:
            return ProbeOutcome(False, "parse_error", 200, latency, {"reason": str(exc)[:200]})
        if not items:
            return ProbeOutcome(False, "no_items", 200, latency)
        snapshot = make_snapshot(
            items,
            etag=result.headers.get("etag"),
            last_modified=result.headers.get("last-modified"),
        )
        return ProbeOutcome(True, None, 200, latency, {"items": len(items)}, snapshot=snapshot)
