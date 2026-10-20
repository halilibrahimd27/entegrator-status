"""``status_page``: what the vendor itself declares (Statuspage, Instatus, RSS/Atom).

Shown next to our own consensus, never merged into it (CLAUDE.md "Status semantics").
Like every change source it honours robots.txt every cycle and fetches conditionally.
"""

from __future__ import annotations

import json
from urllib.parse import urljoin

from sentinel.models.catalog import SourceKind
from sentinel.security.http import PAGE_MAX_BYTES, FetchPolicy, Timeouts
from sentinel.sources.adapters.base import (
    Adapter,
    AdapterEnv,
    ProbeJob,
    ProbeOutcome,
    retry_after_seconds,
)
from sentinel.sources.adapters.feed import ACCEPT as FEED_ACCEPT
from sentinel.sources.adapters.feed import conditional_headers, robots_refusal
from sentinel.sources.feeds import FeedError, parse_feed
from sentinel.sources.snapshots import make_snapshot
from sentinel.sources.statuspages import StatusPageError, parse_instatus, parse_statuspage

ENDPOINTS = {"statuspage": "/api/v2/summary.json", "instatus": "/summary.json"}


def endpoint(provider: str, url: str) -> str:
    path = ENDPOINTS.get(provider)
    return urljoin(url, path) if path else url


class StatusPageAdapter(Adapter):
    kind = SourceKind.STATUS_PAGE

    async def observe(self, job: ProbeJob, env: AdapterEnv) -> ProbeOutcome:
        provider = str(job.config.get("provider", ""))
        url = endpoint(provider, str(job.config["url"]))
        if refusal := await robots_refusal(job, env, url):
            return refusal
        timeout = float(job.timeout_s)
        accept = FEED_ACCEPT if provider == "rss" else "application/json"
        policy = FetchPolicy(
            allow_http=job.allow_http,
            max_bytes=PAGE_MAX_BYTES,
            timeouts=Timeouts(connect_s=min(5.0, timeout), read_s=timeout, total_s=timeout * 2),
            headers=conditional_headers(job, accept),
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
            if provider == "rss":
                items = [
                    {**item, "type": "incident"} for item in parse_feed(result.body, result.url)
                ]
            else:
                parse = parse_statuspage if provider == "statuspage" else parse_instatus
                items = parse(json.loads(result.body), result.url)
        except (FeedError, StatusPageError, ValueError) as exc:
            return ProbeOutcome(False, "parse_error", 200, latency, {"reason": str(exc)[:200]})
        if not items:
            return ProbeOutcome(False, "no_items", 200, latency)
        snapshot = make_snapshot(
            items,
            etag=result.headers.get("etag"),
            last_modified=result.headers.get("last-modified"),
        )
        return ProbeOutcome(True, None, 200, latency, {"items": len(items)}, snapshot=snapshot)
