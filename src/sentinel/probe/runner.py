"""The prober process. Phase 3a: validate configuration and wait; the loop arrives in 3c."""

from __future__ import annotations

import asyncio
import logging

from sentinel.probe.settings import load_probe_settings_or_exit
from sentinel.runtime import wait_for_shutdown

log = logging.getLogger("sentinel.prober")


def main() -> None:
    settings = load_probe_settings_or_exit()
    logging.basicConfig(
        level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    log.info("prober started (node %s)", settings.probe_node_id)
    asyncio.run(wait_for_shutdown())
    log.info("prober stopped")
