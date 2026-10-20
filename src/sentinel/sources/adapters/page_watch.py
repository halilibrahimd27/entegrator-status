"""``page_watch``: announcements on an ordinary web page, picked out with CSS selectors.

robots.txt is checked every cycle; a disallowed page is never fetched. A page that
yields no items (the site changed its markup, or the selector is wrong) is reported
as ``no_items`` so the source shows "needs attention" instead of silent "no news".
"""

from __future__ import annotations

from sentinel.models.catalog import SourceKind
from sentinel.sources.adapters.base import Adapter, AdapterEnv, ProbeJob, ProbeOutcome
from sentinel.sources.adapters.content import fetch_content, parse_failure, snapshot_outcome
from sentinel.sources.pages import PageError, charset_of, parse_page

ACCEPT = "text/html, application/xhtml+xml;q=0.9"


class PageWatchAdapter(Adapter):
    kind = SourceKind.PAGE_WATCH

    async def observe(self, job: ProbeJob, env: AdapterEnv) -> ProbeOutcome:
        config = job.config
        result = await fetch_content(job, env, str(config["url"]), ACCEPT)
        if isinstance(result, ProbeOutcome):
            return result
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
            return parse_failure(result, str(exc))
        return snapshot_outcome(result, items)
