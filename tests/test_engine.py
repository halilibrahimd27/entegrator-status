from __future__ import annotations

import asyncio
import ipaddress
import logging
from collections.abc import Sequence

import httpx
import pytest
from redis.asyncio import Redis

from sentinel.config import load_settings
from sentinel.db.session import Database
from sentinel.engine import service
from sentinel.security.addresses import IPAddress
from sentinel.security.http import OutboundClient


async def test_periodic_job_runs_until_stopped() -> None:
    stop = asyncio.Event()
    runs: list[int] = []

    async def job() -> None:
        runs.append(1)
        if len(runs) == 3:
            stop.set()

    await asyncio.wait_for(service.run_periodically(job, 0.001, stop), timeout=2)
    assert len(runs) == 3


async def test_failing_job_does_not_stop_the_loop(caplog: pytest.LogCaptureFixture) -> None:
    stop = asyncio.Event()
    runs: list[int] = []

    async def job() -> None:
        runs.append(1)
        if len(runs) == 2:
            stop.set()
        raise RuntimeError("boom")

    await asyncio.wait_for(service.run_periodically(job, 0.001, stop), timeout=2)
    assert len(runs) == 2
    assert "periodic job failed" in caplog.text


async def test_serve_runs_retention(db: Database, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="sentinel.engine")
    stop = asyncio.Event()

    async def stop_soon() -> None:
        await asyncio.sleep(0.05)
        stop.set()

    await asyncio.gather(
        service.serve(db, stop, retention_every_s=0.01, status_every_s=0.01, rollups_every_s=0.01),
        stop_soon(),
    )
    assert "retention:" in caplog.text


async def anywhere(host: str, port: int) -> Sequence[IPAddress]:
    return [ipaddress.ip_address("1.1.1.1")]


async def test_serve_runs_the_alert_jobs(
    db: Database,
    redis_client: Redis,
    valid_env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG, logger="sentinel.engine")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:abc")
    polls: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        polls.append(request.url.path.rsplit("/", 1)[1])
        return httpx.Response(200, json={"ok": True, "result": []})

    http = OutboundClient(
        user_agent="EntegratorStatus/test", resolver=anywhere, transport=httpx.MockTransport(handle)
    )
    alerts = service.build_alerts(load_settings(), db, redis_client, http)
    assert alerts.bot is not None
    stop = asyncio.Event()

    async def stop_soon() -> None:
        await asyncio.sleep(0.3)
        stop.set()

    async with http:
        await asyncio.gather(
            service.serve(
                db,
                stop,
                retention_every_s=10,
                status_every_s=10,
                rollups_every_s=10,
                alerts=alerts,
                alerts_every_s=0.01,
            ),
            stop_soon(),
        )
    assert "alerts: 0 DeliveryReport" in caplog.text
    assert polls[0] == "getUpdates"


@pytest.mark.usefixtures("valid_env")
def test_engine_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SESSION_KEY")
    with pytest.raises(SystemExit) as caught:
        service.main()
    assert caught.value.code == 2
