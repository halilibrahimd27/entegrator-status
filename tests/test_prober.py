"""The prober loop: per-host limits, shipping, delivery guarantees, process wiring."""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import func, select, update

from probe_support import (
    Clock,
    Earliest,
    Midpoint,
    add_source,
    client_for,
    env_for,
    host_of,
    job,
    outbound,
    stored,
    two_orgs,
)
from sentinel.config import ConfigError
from sentinel.models import Observation, Source
from sentinel.probe import runner
from sentinel.probe.buffer import ResultBuffer
from sentinel.probe.health import NodeHealth
from sentinel.probe.prober import Intervals, Prober
from sentinel.probe.scheduler import Scheduler
from sentinel.probe.settings import load_probe_settings
from sentinel.security.addresses import STRICT
from sentinel.security.http import OutboundClient
from sentinel.security.internal import CoreClient
from sentinel.sources.adapters import ProbeOutcome
from sentinel.web.services import AppServices

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
