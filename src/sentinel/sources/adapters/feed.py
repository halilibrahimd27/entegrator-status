"""``feed``: RSS / Atom announcements, fetched conditionally (``ETag``, ``If-Modified-Since``).

Content watchers always honour robots.txt, every cycle (ADR 0003): a disallowed feed
is not fetched and the source shows why.
"""

from __future__ import annotations

from sentinel.models.catalog import SourceKind
from sentinel.sources.adapters.base import Adapter, AdapterEnv, ProbeJob, ProbeOutcome
from sentinel.sources.adapters.content import fetch_content, parse_failure, snapshot_outcome
from sentinel.sources.feeds import FeedError, parse_feed

ACCEPT = "application/rss+xml, application/atom+xml, application/xml;q=0.9, text/xml;q=0.8"


class FeedAdapter(Adapter):
    kind = SourceKind.FEED

    async def observe(self, job: ProbeJob, env: AdapterEnv) -> ProbeOutcome:
        result = await fetch_content(job, env, str(job.config["url"]), ACCEPT)
        if isinstance(result, ProbeOutcome):
            return result
        try:
            items = parse_feed(result.body, base=result.url)
        except FeedError as exc:
            return parse_failure(result, str(exc))
        return snapshot_outcome(result, items)
