"""The prober loop: scheduling, per-host limits, buffering, shipping, node health."""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import random
import secrets
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import func, select, update

from probe_support import add_source, client_for, two_orgs
from sentinel.config import ConfigError
from sentinel.models import Observation, Source
from sentinel.probe import runner
from sentinel.probe.buffer import MAX_AGE_S, ResultBuffer
from sentinel.probe.health import NodeHealth
from sentinel.probe.prober import Intervals, Prober
from sentinel.probe.robots import RobotsCache
from sentinel.probe.scheduler import MAX_BACKOFF_FACTOR, Scheduler
from sentinel.probe.settings import load_probe_settings
from sentinel.security.addresses import STRICT, AddressPolicy, IPAddress
from sentinel.security.errors import DNSResolutionError
from sentinel.security.http import OutboundClient
from sentinel.security.internal import CoreClient
from sentinel.security.ratelimit import HostLimiter
from sentinel.sources.adapters import AdapterEnv, ProbeJob, ProbeOutcome
from sentinel.web.services import AppServices


class Clock:
    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class Midpoint(random.Random):
    """Deterministic jitter: always the middle of the range."""

    def uniform(self, a: float, b: float) -> float:
        return (a + b) / 2


class Earliest(random.Random):
    def uniform(self, a: float, b: float) -> float:
        return a


def job(key: str = "k", url: str = "https://a.test/", **overrides: Any) -> ProbeJob:
    values: dict[str, Any] = {
        "key": key,
        "kind": "http",
        "config": {"url": url},
        "interval_s": 60,
        "timeout_s": 2,
        "allow_http": False,
        "ignores_robots": True,
        "source_ids": (str(uuid.uuid4()),),
    }
    values.update(overrides)
    return ProbeJob(**values)


OK = ProbeOutcome(True, None, 200, 5)


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


def stored(n: int) -> list[dict[str, Any]]:
    return [{"id": str(uuid.uuid4()), "n": i} for i in range(n)]


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


# --- fakes for the outbound side ------------------------------------------------------


async def loopback(host: str, port: int) -> Sequence[IPAddress]:
    if host.endswith(".test"):
        return [ipaddress.ip_address("127.0.0.1")]
    raise DNSResolutionError("unknown test host")


def outbound(handler: Callable[[httpx.Request], Any], **limits: Any) -> OutboundClient:
    """An outbound client whose 'internet' is ``handler`` (sync or async)."""

    async def respond(request: httpx.Request) -> httpx.Response:
        answer = handler(request)
        if asyncio.iscoroutine(answer):
            answer = await answer
        assert isinstance(answer, httpx.Response)
        return answer

    return OutboundClient(
        user_agent="EntegratorStatus/test",
        resolver=loopback,
        policy=AddressPolicy.for_tests(),
        transport=httpx.MockTransport(respond),
        limiter=HostLimiter(**limits) if limits else None,
    )


def env_for(http: OutboundClient) -> AdapterEnv:
    async def unused(*args: Any) -> Any:
        raise AssertionError("not used in these tests")

    return AdapterEnv(http=http, robots=RobotsCache(http), tls=unused, dns=unused)


def host_of(request: httpx.Request) -> str:
    return request.headers["host"].split(":")[0]


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


# --- the prober against a fake internet and a fake core ------------------------------


def fake_core(handler: Callable[[httpx.Request], httpx.Response]) -> CoreClient:
    return CoreClient(
        "http://core.test",
        uuid.uuid4(),
        "t" * 43,
        "k" * 43,
        user_agent="test",
        transport=httpx.MockTransport(handler),
    )


def accept_all(request: httpx.Request) -> httpx.Response:
    if request.method == "GET":
        return httpx.Response(200, json={"jobs": []})
    return httpx.Response(202, json={"accepted": len(json.loads(request.content)["results"])})


@pytest.fixture
async def buffer(tmp_path: Path) -> AsyncIterator[ResultBuffer]:
    store = ResultBuffer(tmp_path / "buffer.sqlite3")
    yield store
    store.close()


def make_prober(
    http: OutboundClient,
    buffer: ResultBuffer,
    *,
    core: CoreClient | None = None,
    clock: Clock | None = None,
    **kwargs: Any,
) -> Prober:
    return Prober(
        core=core or fake_core(accept_all),
        env=env_for(http),
        buffer=buffer,
        health=NodeHealth([], http),
        scheduler=Scheduler(clock=clock or Clock(), rng=Midpoint()),
        **kwargs,
    )


async def run_all_due(prober: Prober, clock: Clock, seconds: float = 30) -> None:
    clock.advance(seconds)
    await asyncio.gather(*await prober.run_due())


async def test_never_two_requests_at_once_per_host(buffer: ResultBuffer) -> None:
    in_flight: dict[str, int] = {}
    peak: dict[str, int] = {}
    peak_total = 0

    async def slow(request: httpx.Request) -> httpx.Response:
        nonlocal peak_total
        host = host_of(request)
        in_flight[host] = in_flight.get(host, 0) + 1
        peak[host] = max(peak.get(host, 0), in_flight[host])
        peak_total = max(peak_total, sum(in_flight.values()))
        await asyncio.sleep(0.02)
        in_flight[host] -= 1
        return httpx.Response(200)

    clock = Clock()
    async with outbound(slow, rate_per_s=1000, burst=100) as http:
        prober = make_prober(http, buffer, clock=clock)
        jobs = [job(f"a{i}", url=f"https://a.test/{i}") for i in range(5)]
        jobs += [job(f"b{i}", url=f"https://b.test/{i}") for i in range(3)]
        prober.scheduler.update(jobs)
        await run_all_due(prober, clock)
    assert peak == {"a.test": 1, "b.test": 1}
    assert peak_total == 2  # different hosts do run in parallel
    assert await buffer.size() == 8


async def test_unsupported_kinds_long_holds_and_huge_details(buffer: ResultBuffer) -> None:
    clock = Clock()
    async with outbound(lambda r: httpx.Response(200)) as http:
        prober = make_prober(http, buffer, clock=clock)
        odd = job("odd", kind="carrier_pigeon")
        held = job("held", url="https://held.test/")
        prober.scheduler.update([odd, held])
        prober.scheduler.hold_host("held.test", 3600)
        await run_all_due(prober, clock)
        results = {r["source_ids"][0]: r for r in await buffer.take(10)}
        assert results[odd.source_ids[0]]["error_class"] == "unsupported"
        assert results[held.source_ids[0]]["error_class"] == "rate_limited_by_us"
        assert all(r["node_healthy"] for r in results.values())

        big = prober._result(odd, ProbeOutcome(False, "error", details={"x": "y" * 5000}))
        assert big["details"] == {"truncated": True}


async def test_crawl_delay_holds_the_host(buffer: ResultBuffer) -> None:
    def site(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nCrawl-delay: 30\n")
        return httpx.Response(200)

    clock = Clock()
    async with outbound(site, rate_per_s=100, burst=10) as http:
        prober = make_prober(http, buffer, clock=clock)
        polite = job("p", url="https://polite.test/x", ignores_robots=False)
        prober.scheduler.update([polite])
        await run_all_due(prober, clock)
        assert prober.scheduler.held_for("polite.test") == 30
        assert prober.env.robots.crawl_delay("https://unknown.test/") is None


async def test_job_refresh_keeps_old_jobs_on_failure(buffer: ResultBuffer) -> None:
    answers = [
        httpx.Response(
            200, json={"jobs": [{**job().__dict__, "source_ids": ["s"]}, {"key": "broken"}]}
        ),
        httpx.Response(503),
        httpx.Response(401),
    ]
    core = fake_core(lambda request: answers.pop(0))
    async with outbound(lambda r: httpx.Response(200)) as http:
        prober = make_prober(http, buffer, core=core)
        assert await prober.refresh_jobs() is True
        assert len(prober.scheduler) == 1  # the malformed job is skipped
        assert await prober.refresh_jobs() is False
        assert await prober.refresh_jobs() is False
        assert len(prober.scheduler) == 1


async def test_refused_results_are_isolated_and_auth_failures_keep_everything(
    buffer: ResultBuffer, caplog: pytest.LogCaptureFixture
) -> None:
    mode = {"status": 202}

    def core(request: httpx.Request) -> httpx.Response:
        results = json.loads(request.content)["results"]
        if mode["status"] != 202:
            return httpx.Response(mode["status"])
        if any(r.get("poison") for r in results):
            return httpx.Response(422, json={"error": "invalid"})
        return httpx.Response(202, json={"accepted": len(results)})

    async with outbound(lambda r: httpx.Response(200)) as http:
        prober = make_prober(http, buffer, core=fake_core(core))
        await buffer.add([*stored(2), {"id": str(uuid.uuid4()), "poison": True}])
        with caplog.at_level(logging.ERROR, "sentinel.prober"):
            assert await prober.ship() == 2
        assert await buffer.size() == 0
        assert "dropping it" in caplog.text

        await buffer.add(stored(2))
        mode["status"] = 401
        assert await prober.ship() == 0
        assert await buffer.size() == 2  # a refused node keeps its results

        await buffer.add([{"id": str(uuid.uuid4()), "poison": True}])
        calls = {"n": 0}

        def poison_then_auth(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            return httpx.Response(422 if calls["n"] == 1 else 403)

        prober.core = fake_core(poison_then_auth)
        assert await prober.ship() == 0
        assert await buffer.size() == 3


# --- end to end against the real ingest API -----------------------------------------


class SwitchableCore(httpx.AsyncBaseTransport):
    """The ASGI core behind a switch: down, up, or 'answer lost after delivery'."""

    def __init__(self, app: FastAPI) -> None:
        self._inner = httpx.ASGITransport(app=app)
        self.mode = "up"

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if self.mode == "down":
            raise httpx.ConnectError("core is down")
        response = await self._inner.handle_async_request(request)
        if self.mode == "lost" and request.method == "POST":
            raise httpx.ReadError("answer lost")
        return response


async def eventually(check: Callable[[], Awaitable[bool]], limit_s: float = 10) -> None:
    for _ in range(int(limit_s / 0.02)):
        if await check():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("condition not reached in time")


async def observations(services: AppServices) -> int:
    async with services.db.system("test check") as s:
        return int(await s.scalar(select(func.count()).select_from(Observation)) or 0)


async def test_shared_source_is_probed_once_and_fanned_out(
    app: FastAPI, services: AppServices, buffer: ResultBuffer
) -> None:
    a, b = await two_orgs(services)
    url = "https://portal.test/health"
    sources = [await add_source(services, org, url) for org in (a, b)]
    creds = await services.nodes.register("n1", "test")
    probes: list[str] = []

    def portal(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)  # no robots.txt: everything allowed
        probes.append(request.url.path)
        return httpx.Response(200)

    switch = SwitchableCore(app)
    core = CoreClient(
        "http://core.test",
        creds.node_id,
        creds.token,
        creds.signing_key,
        user_agent="test",
        transport=switch,
    )
    clock = Clock()
    async with outbound(portal) as http:
        prober = make_prober(http, buffer, core=core, clock=clock)
        assert await prober.refresh_jobs()
        assert len(prober.scheduler) == 1  # two orgs, one job
        await run_all_due(prober, clock)
        assert probes == ["/health"]  # probed once for both orgs

        switch.mode = "down"
        assert await prober.ship() == 0
        assert await buffer.size() == 1  # kept while the core is unreachable

        switch.mode = "lost"
        assert await prober.ship() == 0
        assert await observations(services) == 2  # it landed, the answer did not
        assert await buffer.size() == 1

        switch.mode = "up"
        assert await prober.ship() == 1
        assert await buffer.size() == 0
        assert await observations(services) == 2  # resending was harmless

    async with services.db.system("test check") as s:
        rows = (await s.scalars(select(Observation))).all()
    assert sorted(r.source_id for r in rows) == sorted(sources)
    assert all(r.ok and r.status_code == 200 and r.node_healthy for r in rows)
    assert {r.org_id for r in rows} == {a, b}


async def test_run_loop_probes_ships_and_stops(
    app: FastAPI, services: AppServices, buffer: ResultBuffer
) -> None:
    source = await add_source(services, None, "https://portal.test/")
    async with services.db.system("test change") as s:
        await s.execute(
            update(Source).where(Source.id == source).values(availability_ignores_robots=True)
        )
    creds = await services.nodes.register("n1", "test")
    async with outbound(lambda r: httpx.Response(200)) as http:
        prober = Prober(
            core=client_for(app, creds),
            env=env_for(http),
            buffer=buffer,
            health=NodeHealth(["https://control.test/"], http, every_s=0.05),
            scheduler=Scheduler(rng=Earliest()),
            intervals=Intervals(jobs_refresh_s=0.05, ship_every_s=0.05, tick_s=0.01),
        )
        stop = asyncio.Event()
        task = asyncio.create_task(prober.run(stop))

        async def landed() -> bool:
            return await observations(services) > 0

        await eventually(landed)
        stop.set()
        await task
    assert await buffer.size() == 0
    assert await observations(services) == 1


async def test_failing_periodic_task_does_not_stop_the_loop(
    buffer: ResultBuffer, caplog: pytest.LogCaptureFixture
) -> None:
    calls = {"n": 0}
    third = asyncio.Event()

    async def flaky() -> None:
        calls["n"] += 1
        if calls["n"] == 3:
            third.set()
        raise RuntimeError("boom")

    async with outbound(lambda r: httpx.Response(200)) as http:
        prober = make_prober(http, buffer)
        stop = asyncio.Event()
        task = asyncio.create_task(prober._every(flaky, 0.01, stop))
        async with asyncio.timeout(5):
            await third.wait()
        stop.set()
        await task
    assert "prober task failed" in caplog.text


# --- process wiring -------------------------------------------------------------------


@pytest.fixture
def probe_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    values = {
        "PUBLIC_BASE_URL": "https://status.example.invalid/",
        "CONTACT_EMAIL": "bot@status.example.invalid",
        "PROBE_INGEST_URL": "http://core.example.invalid:8000",
        "PROBE_NODE_ID": str(uuid.uuid4()),
        "PROBE_NODE_TOKEN": secrets.token_urlsafe(32),
        "PROBE_SIGNING_KEY": secrets.token_urlsafe(32),
        "PROBE_BUFFER_PATH": str(tmp_path / "buffer.sqlite3"),
        "PROBE_CONTROL_TARGETS": "https://one.example.invalid/, https://two.example.invalid/",
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)


@pytest.mark.parametrize(
    "targets",
    [
        "http://plain.example.invalid/",
        "<CONTROL_URL>",
        "https://127.0.0.1/",
        ",".join(f"https://t{i}.example.invalid/" for i in range(11)),
    ],
)
def test_bad_control_targets_refuse_to_start(
    probe_env: None, monkeypatch: pytest.MonkeyPatch, targets: str
) -> None:
    monkeypatch.setenv("PROBE_CONTROL_TARGETS", targets)
    with pytest.raises(ConfigError, match="PROBE_CONTROL_TARGETS"):
        load_probe_settings()


async def test_build_prober_uses_the_strict_address_policy(probe_env: None) -> None:
    prober = runner.build_prober(load_probe_settings())
    try:
        assert prober.env.http.address_policy is STRICT
        assert "EntegratorStatus/" in prober.env.http.user_agent
        assert prober.health.enabled
    finally:
        await runner.close(prober)


def test_main_runs_until_stopped(
    probe_env: None, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    async def finish(self: Prober, stop: asyncio.Event) -> None:
        assert not stop.is_set()

    monkeypatch.setattr(Prober, "run", finish)
    with caplog.at_level(logging.INFO, "sentinel.prober"):
        runner.main()
    assert "prober stopped" in caplog.text
    assert logging.getLogger("httpx").level == logging.WARNING  # URLs stay out of logs
