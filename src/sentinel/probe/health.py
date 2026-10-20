"""Node self-health: our own network must not become a provider outage.

The node checks a few stable public URLs (``PROBE_CONTROL_TARGETS``). If two of them
fail (one, when only one or two are configured), the node marks its results
``node_healthy = False`` and the engine (Phase 4) leaves them out of consensus.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable

from sentinel.security.errors import OutboundError
from sentinel.security.http import FetchPolicy, OutboundClient, Timeouts

CHECK_EVERY_S = 300.0
_POLICY = FetchPolicy(method="HEAD", max_bytes=64 * 1024, timeouts=Timeouts(5, 10, 15))


class NodeHealth:
    def __init__(
        self,
        targets: list[str],
        http: OutboundClient,
        *,
        every_s: float = CHECK_EVERY_S,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._targets = targets
        self._http = http
        self._every = every_s
        self._clock = clock
        self._checked_at: float | None = None
        self._lock = asyncio.Lock()
        self.healthy = True
        self.failures = 0

    @property
    def enabled(self) -> bool:
        return bool(self._targets)

    @property
    def every_s(self) -> float:
        return self._every

    def _threshold(self) -> int:
        return 1 if len(self._targets) <= 2 else 2

    async def refresh(self, *, force: bool = False) -> bool:
        if not self._targets:
            return True  # nothing to compare against: assume healthy (logged at start-up)
        async with self._lock:
            fresh = self._checked_at is not None and self._clock() - self._checked_at < self._every
            if fresh and not force:
                return self.healthy
            return await self._check()

    async def _check(self) -> bool:
        failures = 0
        for target in self._targets:
            try:
                result = await self._http.fetch(target, _POLICY)
                if result.status_code >= 500:
                    failures += 1
            except OutboundError:
                failures += 1
        self.failures = failures
        self.healthy = failures < self._threshold()
        self._checked_at = self._clock()
        return self.healthy
