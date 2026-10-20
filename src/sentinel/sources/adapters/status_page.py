"""``status_page``: what the vendor itself declares (Statuspage, Instatus, RSS/Atom).

Shown next to our own consensus, never merged into it (CLAUDE.md "Status semantics").
Like every change source it honours robots.txt every cycle and fetches conditionally.
"""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import urljoin

from sentinel.models.catalog import SourceKind
from sentinel.sources.adapters.base import Adapter, AdapterEnv, ProbeJob, ProbeOutcome
from sentinel.sources.adapters.content import fetch_content, parse_failure, snapshot_outcome
from sentinel.sources.adapters.feed import ACCEPT as FEED_ACCEPT
from sentinel.sources.feeds import FeedError, parse_feed
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
        accept = FEED_ACCEPT if provider == "rss" else "application/json"
        result = await fetch_content(job, env, url, accept)
        if isinstance(result, ProbeOutcome):
            return result
        try:
            items: list[dict[str, Any]]
            if provider == "rss":
                items = [
                    {**item, "type": "incident"} for item in parse_feed(result.body, result.url)
                ]
            else:
                parse = parse_statuspage if provider == "statuspage" else parse_instatus
                items = parse(json.loads(result.body), result.url)
        except (FeedError, StatusPageError, ValueError) as exc:
            return parse_failure(result, str(exc))
        return snapshot_outcome(result, items)
