"""In-process rate limiting: a token bucket and a per-host concurrency limiter."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

Clock = Callable[[], float]
Sleep = Callable[[float], Awaitable[None]]


class TokenBucket:
    """Classic token bucket. ``rate`` tokens per second, at most ``capacity`` stored."""

    def __init__(
        self,
        rate: float,
        capacity: float,
        *,
        clock: Clock = time.monotonic,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        if rate <= 0:
            raise ValueError("rate must be positive")
        if capacity < 1:
            raise ValueError("capacity must be at least 1")
        self._rate = rate
        self._capacity = capacity
        self._tokens = capacity
        self._clock = clock
        self._sleep = sleep
        self._updated = clock()
        self._lock = asyncio.Lock()

    def _refill(self) -> None:
        now = self._clock()
        self._tokens = min(self._capacity, self._tokens + (now - self._updated) * self._rate)
        self._updated = now

    def try_acquire(self) -> bool:
        """Take a token if one is available right now."""
        self._refill()
        if self._tokens >= 1:
            self._tokens -= 1
            return True
        return False

    async def acquire(self) -> None:
        """Wait until a token is available, then take it. Waiters are served in order."""
        async with self._lock:
            while not self.try_acquire():
                await self._sleep((1 - self._tokens) / self._rate)


class HostLimiter:
    """At most ``concurrency`` requests in flight per host, each paid for by a token."""

    def __init__(
        self,
        *,
        concurrency: int = 1,
        rate_per_s: float = 1.0,
        burst: int = 3,
        clock: Clock = time.monotonic,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        if concurrency < 1:
            raise ValueError("concurrency must be at least 1")
        self._concurrency = concurrency
        self._rate = rate_per_s
        self._burst = burst
        self._clock = clock
        self._sleep = sleep
        self._semaphores: dict[str, asyncio.Semaphore] = {}
        self._buckets: dict[str, TokenBucket] = {}

    @asynccontextmanager
    async def slot(self, host: str) -> AsyncIterator[None]:
        key = host.lower()
        semaphore = self._semaphores.setdefault(key, asyncio.Semaphore(self._concurrency))
        bucket = self._buckets.get(key)
        if bucket is None:
            bucket = TokenBucket(self._rate, self._burst, clock=self._clock, sleep=self._sleep)
            self._buckets[key] = bucket
        async with semaphore:
            await bucket.acquire()
            yield
