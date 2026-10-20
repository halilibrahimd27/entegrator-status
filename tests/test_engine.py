from __future__ import annotations

import asyncio
import logging

import pytest

from sentinel.db.session import Database
from sentinel.engine import service


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
        service.serve(db, stop, retention_every_s=0.01, status_every_s=0.01), stop_soon()
    )
    assert "retention:" in caplog.text


@pytest.mark.usefixtures("valid_env")
def test_engine_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SESSION_KEY")
    with pytest.raises(SystemExit) as caught:
        service.main()
    assert caught.value.code == 2
