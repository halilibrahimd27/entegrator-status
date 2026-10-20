"""Prober parts on their own: scheduler, result buffer, node self-health."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

from probe_support import OK, Clock, Midpoint, host_of, job, outbound, stored
from sentinel.probe.buffer import MAX_AGE_S, ResultBuffer
from sentinel.probe.health import NodeHealth
from sentinel.probe.scheduler import MAX_BACKOFF_FACTOR, Scheduler
from sentinel.sources.adapters import ProbeOutcome

# --- scheduler ----------------------------------------------------------------------


def test_first_runs_are_jittered_then_follow_the_interval() -> None:
    clock = Clock()
    scheduler = Scheduler(clock=clock, rng=Midpoint())
    a, b = job("a", interval_s=60), job("b", interval_s=600)
    scheduler.update([a, b])
    assert len(scheduler) == 2
    assert scheduler.due() == ([], [])
    assert scheduler.seconds_until_next() == 30  # jitter is capped at 60 s
    clock.advance(30)
    assert scheduler.due() == ([a, b], [])
    assert scheduler.due() == ([], [])  # running jobs are not due twice
    scheduler.done(a, OK)
    clock.advance(59)
    assert scheduler.due() == ([], [])
    clock.advance(1)
    assert scheduler.due() == ([a], [])


def test_update_keeps_timing_and_drops_removed_jobs() -> None:
    clock = Clock()
    scheduler = Scheduler(clock=clock, rng=Midpoint())
    a, b = job("a"), job("b")
    scheduler.update([a])
    clock.advance(30)
    assert scheduler.due() == ([a], [])
    scheduler.done(a, OK)
    scheduler.update([a, b])  # a keeps its next run in 60 s, b is new (30 s)
    clock.advance(30)
    assert scheduler.due() == ([b], [])
    scheduler.update([b])
    assert len(scheduler) == 1
    scheduler.done(a, OK)  # finished after removal: ignored
    assert len(scheduler) == 1
    assert Scheduler().seconds_until_next() == 1.0


def test_held_hosts_wait_and_long_holds_are_reported() -> None:
    clock = Clock()
    scheduler = Scheduler(clock=clock, rng=Midpoint())
    a, other = job("a"), job("o", url="https://other.test/")
    scheduler.update([a, other])
    scheduler.hold_host("a.test", 40)
    clock.advance(30)
    assert scheduler.due() == ([other], [])  # other hosts are unaffected
    assert scheduler.seconds_until_next() == 10  # a runs when the hold ends
    clock.advance(10)
    assert scheduler.due() == ([a], [])

    scheduler.hold_host("a.test", 600)  # longer than the interval
    scheduler.done(a, OK)
    clock.advance(60)
    assert scheduler.due() == ([], [a])  # reported as rate_limited_by_us
    assert scheduler.due() == ([], [])
    clock.advance(60)
    assert scheduler.due() == ([], [a])  # once per interval while held
    scheduler.hold_host("", 10)  # no host: ignored
    assert scheduler.held_for("") == 0


def test_retry_after_holds_the_host_and_429_backs_off() -> None:
    clock = Clock()
    scheduler = Scheduler(clock=clock, rng=Midpoint())
    a = job("a")
    scheduler.update([a])
    clock.advance(30)
    scheduler.due()
    scheduler.done(a, ProbeOutcome(False, "http_5xx", 503))
    assert scheduler.seconds_until_next() == 60  # a plain outage does not back off
    clock.advance(60)
    scheduler.due()
    scheduler.done(a, ProbeOutcome(False, "http_5xx", 503, retry_after_s=120))
    assert scheduler.held_for("a.test") == 120
    assert scheduler.seconds_until_next() == 120  # asked to slow down: 2x
    clock.advance(120)
    scheduler.due()
    scheduler.done(a, OK)

    waits = []
    for _ in range(5):
        clock.advance(scheduler.seconds_until_next() + scheduler.held_for("a.test"))
        assert scheduler.due() == ([a], [])
        scheduler.done(a, ProbeOutcome(False, "http_4xx", 429))
        waits.append(scheduler.seconds_until_next())
    assert waits == [120, 240, 480, 480, 480]
    assert MAX_BACKOFF_FACTOR == 8
    clock.advance(480)
    scheduler.due()
    scheduler.done(a, OK)
    assert scheduler.seconds_until_next() == 60  # success resets the backoff


# --- buffer -------------------------------------------------------------------------


async def test_buffer_keeps_results_across_restarts(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "buffer.sqlite3"
    clock = Clock()
    first = ResultBuffer(path, clock=clock)
    results = stored(3)
    for result in results:
        clock.advance(1)
        await first.add([result])
    await first.add(results[:1])  # same id twice: stored once
    first.close()

    second = ResultBuffer(path, clock=clock)
    assert await second.size() == 3
    oldest = await second.take(2)
    assert [r["n"] for r in oldest] == [0, 1]
    await second.ack([r["id"] for r in oldest])
    await second.ack([])
    assert [r["n"] for r in await second.take(10)] == [2]
    second.close()


async def test_buffer_cap_and_age(tmp_path: Path) -> None:
    clock = Clock()
    buffer = ResultBuffer(tmp_path / "b.sqlite3", max_rows=2, clock=clock)
    for result in stored(3):
        clock.advance(1)
        dropped = await buffer.add([result])
    assert dropped == 1
    assert [r["n"] for r in await buffer.take(10)] == [1, 2]
    clock.advance(MAX_AGE_S + 1)  # the core would refuse them now
    assert await buffer.take(10) == []
    assert await buffer.size() == 0
    buffer.close()


# --- node health --------------------------------------------------------------------


def health_handler(seen: list[str]) -> Callable[[httpx.Request], httpx.Response]:
    def handle(request: httpx.Request) -> httpx.Response:
        host = host_of(request)
        seen.append(host)
        if host.startswith("gone"):
            raise httpx.ConnectError("refused")
        return httpx.Response(503 if host.startswith("down") else 200)

    return handle


@pytest.mark.parametrize(
    ("targets", "healthy"),
    [
        (["up1", "down1", "up2"], True),  # one of three failing: the provider's problem
        (["up1", "down1", "gone1"], False),  # two failing: our network
        (["up1", "up2", "up3", "down1", "down2"], False),
        (["down1"], False),
        (["up1", "gone1"], False),
        (["up1", "up2"], True),
    ],
)
async def test_node_health_threshold(targets: list[str], healthy: bool) -> None:
    async with outbound(health_handler([]), rate_per_s=100, burst=10) as http:
        health = NodeHealth([f"https://{t}.test/" for t in targets], http)
        assert await health.refresh() is healthy
        assert health.healthy is healthy


async def test_node_health_is_cached_and_optional() -> None:
    seen: list[str] = []
    clock = Clock()
    async with outbound(health_handler(seen)) as http:
        health = NodeHealth(["https://up1.test/"], http, every_s=300, clock=clock)
        assert health.enabled
        await health.refresh()
        await health.refresh()
        assert len(seen) == 1
        clock.advance(301)
        await health.refresh()
        await health.refresh(force=True)
        assert len(seen) == 3

        disabled = NodeHealth([], http)
        assert not disabled.enabled
        assert await disabled.refresh() is True


async def test_buffer_batches_respect_a_byte_budget(tmp_path: Path) -> None:
    clock = Clock()
    buffer = ResultBuffer(tmp_path / "b.sqlite3", clock=clock)
    for i in range(4):
        clock.advance(1)
        await buffer.add([{"id": str(uuid.uuid4()), "n": i, "pad": "x" * 1000}])
    assert len(await buffer.take(10, max_bytes=2500)) == 2
    assert len(await buffer.take(10, max_bytes=10)) == 1  # always at least one
    assert len(await buffer.take(10)) == 4
    buffer.close()
