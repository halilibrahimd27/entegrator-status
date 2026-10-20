"""The prober process: wire the real clients from ``ProbeSettings`` and run until SIGTERM."""

from __future__ import annotations

import asyncio
import logging
import signal

from sentinel.probe.buffer import ResultBuffer
from sentinel.probe.health import NodeHealth
from sentinel.probe.prober import Intervals, Prober
from sentinel.probe.robots import RobotsCache
from sentinel.probe.settings import ProbeSettings, load_probe_settings_or_exit
from sentinel.runtime import configure_logging
from sentinel.security.http import OutboundClient, build_user_agent
from sentinel.security.internal import CoreClient
from sentinel.security.tls import CertInfo, fetch_certificate
from sentinel.sources.adapters import AdapterEnv
from sentinel.sources.adapters.dns import system_dns
from sentinel.sources.auth import AuthProvider

log = logging.getLogger("sentinel.prober")


def adapter_env(http: OutboundClient, signing_key: str | None = None) -> AdapterEnv:
    async def tls(host: str, port: int, limit_s: float) -> CertInfo:
        return await fetch_certificate(
            host, port, timeout_s=limit_s, resolver=http.resolver, policy=http.address_policy
        )

    auth = AuthProvider(signing_key, http) if signing_key else None
    return AdapterEnv(http=http, robots=RobotsCache(http), tls=tls, dns=system_dns, auth=auth)


def build_prober(settings: ProbeSettings) -> Prober:
    user_agent = build_user_agent(str(settings.public_base_url), settings.contact_email)
    http = OutboundClient(user_agent=user_agent)
    core = CoreClient(
        str(settings.probe_ingest_url),
        settings.probe_node_id,
        settings.probe_node_token.get_secret_value(),
        settings.probe_signing_key.get_secret_value(),
        user_agent=user_agent,
    )
    return Prober(
        core=core,
        env=adapter_env(http, settings.probe_signing_key.get_secret_value()),
        buffer=ResultBuffer(settings.probe_buffer_path),
        health=NodeHealth(settings.control_targets, http),
        intervals=Intervals(
            jobs_refresh_s=settings.probe_jobs_refresh_s, ship_every_s=settings.probe_ship_every_s
        ),
    )


async def close(prober: Prober) -> None:
    await prober.env.http.aclose()
    await prober.core.aclose()
    prober.buffer.close()


def main() -> None:
    settings = load_probe_settings_or_exit()
    configure_logging(level=settings.log_level)

    async def run() -> None:
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, stop.set)
        prober = build_prober(settings)
        log.info("prober started (node %s)", settings.probe_node_id)
        try:
            await prober.run(stop)
        finally:
            await close(prober)
            log.info("prober stopped")

    asyncio.run(run())
