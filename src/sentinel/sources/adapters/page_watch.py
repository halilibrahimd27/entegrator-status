"""``page_watch``: announcements on an ordinary web page, picked out with CSS selectors.

robots.txt is checked every cycle; a disallowed page is never fetched. A page that
yields no items (the site changed its markup, or the selector is wrong) is reported
as ``no_items`` so the source shows "needs attention" instead of silent "no news".
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
from sentinel.sources.adapters.feed import conditional_headers, robots_refusal
from sentinel.sources.pages import PageError, charset_of, parse_page
from sentinel.sources.snapshots import make_snapshot

ACCEPT = "text/html, application/xhtml+xml;q=0.9"


class PageWatchAdapter(Adapter):
    kind = SourceKind.PAGE_WATCH

    async def observe(self, job: ProbeJob, env: AdapterEnv) -> ProbeOutcome:
        config = job.config
        url = str(config["url"])
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
            items = parse_page(
                result.body,
                result.url,
                item_selector=str(config["item_selector"]),
                title_selector=str(config["title_selector"]),
                date_selector=config.get("date_selector"),
                link_selector=config.get("link_selector"),
                charset=charset_of(result.headers.get("content-type")),
            )
        except PageError as exc:
            return ProbeOutcome(False, "parse_error", 200, latency, {"reason": str(exc)[:200]})
        if not items:
            return ProbeOutcome(False, "no_items", 200, latency)
        snapshot = make_snapshot(
            items,
            etag=result.headers.get("etag"),
            last_modified=result.headers.get("last-modified"),
        )
        return ProbeOutcome(True, None, 200, latency, {"items": len(items)}, snapshot=snapshot)
