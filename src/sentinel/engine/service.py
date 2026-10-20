"""The engine process: periodic jobs until SIGTERM. Phase 3+ adds consensus and rollups."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
from collections.abc import Awaitable, Callable
from dataclasses import asdict

from sentinel.config import load_settings_or_exit
from sentinel.db.session import Database
from sentinel.engine.retention import run_retention
from sentinel.runtime import configure_logging

log = logging.getLogger("sentinel.engine")
RETENTION_EVERY_S = 3600.0

Job = Callable[[], Awaitable[None]]


async def run_periodically(job: Job, every_s: float, stop: asyncio.Event) -> None:
    """Run ``job`` now and then every ``every_s`` seconds until ``stop`` is set.

    A failing run is logged and retried next time; it never kills the loop.
    """
    while not stop.is_set():
        try:
            await job()
        except Exception:
            log.exception("periodic job failed")
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=every_s)


async def serve(db: Database, stop: asyncio.Event, every_s: float = RETENTION_EVERY_S) -> None:
    async def retention() -> None:
        report = await run_retention(db)
        log.info("retention: %s", asdict(report))

    await run_periodically(retention, every_s, stop)


def main() -> None:
    settings = load_settings_or_exit()
    configure_logging(settings)

    async def run() -> None:
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, stop.set)
        db = Database(settings.database_url.get_secret_value(), pool_size=2)
        log.info("engine started (environment=%s)", settings.environment)
        try:
            await serve(db, stop)
        finally:
            await db.dispose()
            log.info("engine stopped")

    asyncio.run(run())
