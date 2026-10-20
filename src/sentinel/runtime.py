"""Process helpers shared by the long-running services."""

from __future__ import annotations

import asyncio
import logging
import signal

from sentinel.config import Settings, load_settings_or_exit


def configure_logging(settings: Settings | None = None, *, level: str | None = None) -> None:
    logging.basicConfig(
        level=level or (settings.log_level if settings else "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    # httpx logs every request URL at INFO; a source URL can carry a token in its query.
    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


async def wait_for_shutdown() -> None:
    """Block until SIGTERM or SIGINT."""
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    try:
        await stop.wait()
    finally:
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.remove_signal_handler(sig)


def run_service(name: str) -> None:
    """Validate config (fail closed), then idle until asked to stop.

    Phase 0 only proves the process shape; the prober's checks arrive in Phase 2 and
    the engine's jobs in Phase 3.
    """
    settings = load_settings_or_exit()
    configure_logging(settings)
    log = logging.getLogger(f"sentinel.{name}")
    log.info("%s started (environment=%s)", name, settings.environment)
    asyncio.run(wait_for_shutdown())
    log.info("%s stopped", name)
