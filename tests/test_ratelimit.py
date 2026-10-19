from __future__ import annotations

import asyncio

import pytest

from sentinel.security.ratelimit import HostLimiter, TokenBucket


class FakeTime:
    """Deterministic clock; ``sleep`` advances it instead of waiting."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.slept: list[float] = []

    def clock(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


@pytest.mark.parametrize(("rate", "capacity"), [(0, 1), (-1, 1), (1, 0.5)])
def test_bucket_rejects_bad_parameters(rate: float, capacity: float) -> None:
    with pytest.raises(ValueError, match="must be"):
        TokenBucket(rate, capacity)


def test_burst_then_refill() -> None:
    time = FakeTime()
    bucket = TokenBucket(rate=0.5, capacity=2, clock=time.clock)
    assert bucket.try_acquire()
    assert bucket.try_acquire()
    assert not bucket.try_acquire()
    time.now += 1.9
    assert not bucket.try_acquire()
    time.now += 0.1
    assert bucket.try_acquire()


def test_refill_never_exceeds_capacity() -> None:
    time = FakeTime()
    bucket = TokenBucket(rate=10, capacity=2, clock=time.clock)
    time.now += 3600
    assert [bucket.try_acquire() for _ in range(3)] == [True, True, False]


async def test_acquire_waits_exactly_long_enough() -> None:
    time = FakeTime()
    bucket = TokenBucket(rate=0.5, capacity=1, clock=time.clock, sleep=time.sleep)
    start = time.now
    for _ in range(3):
        await bucket.acquire()
    assert time.now - start == pytest.approx(4.0)


def test_limiter_rejects_zero_concurrency() -> None:
    with pytest.raises(ValueError, match="concurrency"):
        HostLimiter(concurrency=0)


async def _max_parallel(limiter: HostLimiter, hosts: list[str]) -> dict[str, int]:
    active: dict[str, int] = {}
    peak: dict[str, int] = {}

    async def work(host: str) -> None:
        key = host.lower()
        async with limiter.slot(host):
            active[key] = active.get(key, 0) + 1
            peak[key] = max(peak.get(key, 0), active[key])
            await asyncio.sleep(0.01)
            active[key] -= 1

    await asyncio.gather(*(work(h) for h in hosts))
    return peak


async def test_never_two_requests_to_one_host_at_once() -> None:
    limiter = HostLimiter(concurrency=1, rate_per_s=1000, burst=100)
    peak = await _max_parallel(limiter, ["a.example"] * 5 + ["A.EXAMPLE"] * 5)
    assert peak == {"a.example": 1}


async def test_hosts_do_not_block_each_other() -> None:
    limiter = HostLimiter(concurrency=1, rate_per_s=1000, burst=100)
    started: list[str] = []
    release = asyncio.Event()

    async def hold(host: str) -> None:
        async with limiter.slot(host):
            started.append(host)
            await release.wait()

    tasks = [asyncio.create_task(hold(h)) for h in ("a.example", "b.example")]
    for _ in range(20):
        if len(started) == 2:
            break
        await asyncio.sleep(0)
    assert sorted(started) == ["a.example", "b.example"]
    release.set()
    await asyncio.gather(*tasks)


async def test_limiter_spends_tokens_per_host() -> None:
    time = FakeTime()
    limiter = HostLimiter(rate_per_s=1.0, burst=1, clock=time.clock, sleep=time.sleep)
    for _ in range(3):
        async with limiter.slot("a.example"):
            pass
    async with limiter.slot("b.example"):
        pass
    assert time.slept == [pytest.approx(1.0), pytest.approx(1.0)]
