"""robots.txt, cached per origin for up to 24 h (RFC 9309, see ``robots_rules``).

4xx (no robots.txt) allows everything; 5xx or an unreachable file disallows everything
until the next attempt. ``Crawl-delay`` is honoured by the scheduler.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import urlsplit

from sentinel.probe.robots_rules import ALLOW_ALL, Robots, parse
from sentinel.security.errors import OutboundError
from sentinel.security.http import FetchPolicy, OutboundClient, Timeouts

ROBOTS_MAX_BYTES = 512 * 1024
TTL_S = 24 * 3600
RETRY_UNREACHABLE_S = 3600


@dataclass(frozen=True)
class RobotsDecision:
    allowed: bool
    crawl_delay_s: float | None
    reason: str  # "allowed", "disallowed", "unreachable"


@dataclass
class _Entry:
    rules: Robots | None  # None: unreachable, disallow all
    expires: float


class RobotsCache:
    def __init__(
        self, http: OutboundClient, *, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self._http = http
        self._clock = clock
        self._entries: dict[str, _Entry] = {}

    async def check(self, url: str, *, allow_http: bool = False) -> RobotsDecision:
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        entry = self._entries.get(origin)
        if entry is None or entry.expires <= self._clock():
            entry = await self._load(origin, allow_http)
            self._entries[origin] = entry
        if entry.rules is None:
            return RobotsDecision(False, None, "unreachable")
        allowed = entry.rules.allows(url)
        return RobotsDecision(
            allowed, entry.rules.crawl_delay, "allowed" if allowed else "disallowed"
        )

    def crawl_delay(self, url: str) -> float | None:
        """The cached ``Crawl-delay`` for ``url``'s origin; never fetches."""
        parts = urlsplit(url)
        entry = self._entries.get(f"{parts.scheme}://{parts.netloc}")
        if entry is None or entry.rules is None:
            return None
        return entry.rules.crawl_delay

    async def _load(self, origin: str, allow_http: bool) -> _Entry:
        policy = FetchPolicy(
            allow_http=allow_http, max_bytes=ROBOTS_MAX_BYTES, timeouts=Timeouts(5, 10, 15)
        )
        try:
            result = await self._http.fetch(f"{origin}/robots.txt", policy)
        except OutboundError:
            return _Entry(None, self._clock() + RETRY_UNREACHABLE_S)
        if 400 <= result.status_code < 500:
            rules = ALLOW_ALL  # RFC 9309: an unavailable robots.txt allows everything
        elif result.status_code >= 300:
            return _Entry(None, self._clock() + RETRY_UNREACHABLE_S)  # unreachable: disallow
        else:
            rules = parse(result.body.decode("utf-8", errors="replace"))
        return _Entry(rules, self._clock() + TTL_S)
