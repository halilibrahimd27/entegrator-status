"""The engine process: status, changes, risks, alerts, rollups and retention until SIGTERM."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass

from redis.asyncio import Redis

from sentinel.accounts.mail import build_mailer
from sentinel.alerts.channels import Senders
from sentinel.alerts.delivery import DeliveryWorker
from sentinel.alerts.events import run_events
from sentinel.alerts.subscriptions import SubscriptionService
from sentinel.alerts.telegram import TelegramBot
from sentinel.config import Settings, load_settings_or_exit
from sentinel.db.session import Database
from sentinel.engine.changes import run_changes
from sentinel.engine.retention import run_retention
from sentinel.engine.rollups import run_rollups
from sentinel.engine.status import StatusEngine
from sentinel.graph.service import GraphService
from sentinel.risk.digest import DigestViews
from sentinel.risk.drift import run_drift
from sentinel.risk.impact import ImpactViews
from sentinel.risk.lifecycle import run_lifecycle
from sentinel.risk.register import RiskRegister
from sentinel.risk.scan import run_risks
from sentinel.runtime import configure_logging
from sentinel.security.http import OutboundClient, build_user_agent
from sentinel.tenancy.credentials import CredentialService

log = logging.getLogger("sentinel.engine")
RETENTION_EVERY_S = 3600.0
STATUS_EVERY_S = 30.0
ROLLUPS_EVERY_S = 600.0
ALERTS_EVERY_S = 15.0
WEEKLY_EVERY_S = 300.0
TELEGRAM_EVERY_S = 5.0

Job = Callable[[], Awaitable[None]]


@dataclass
class AlertJobs:
    worker: DeliveryWorker
    digests: DigestViews
    bot: TelegramBot | None = None


def build_alerts(settings: Settings, db: Database, redis: Redis, http: OutboundClient) -> AlertJobs:
    mailer = build_mailer(settings)
    token = settings.telegram_bot_token.get_secret_value() if settings.telegram_bot_token else None
    senders = Senders(http, mailer, telegram_token=token)
    subscriptions = SubscriptionService(db, settings, mailer)
    worker = DeliveryWorker(
        db,
        senders,
        CredentialService(db, settings),
        public_base_url=settings.public_base,
        unsubscribe_key=subscriptions.unsubscribe_key,
    )
    impact = ImpactViews(db, GraphService(db))
    digests = DigestViews(impact, RiskRegister(db, impact))
    bot = TelegramBot(http, senders, subscriptions, redis, token=token) if token else None
    return AlertJobs(worker, digests, bot)


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


async def serve(
    db: Database,
    stop: asyncio.Event,
    *,
    retention_every_s: float = RETENTION_EVERY_S,
    status_every_s: float = STATUS_EVERY_S,
    rollups_every_s: float = ROLLUPS_EVERY_S,
    alerts: AlertJobs | None = None,
    alerts_every_s: float = ALERTS_EVERY_S,
) -> None:
    engine = StatusEngine(db)

    async def status() -> None:
        report = await engine.run()
        level = logging.INFO if report.changes else logging.DEBUG
        log.log(level, "status: %s", asdict(report))
        found = await run_changes(db)
        log.log(logging.INFO if found.changes else logging.DEBUG, "changes: %s", asdict(found))

    async def rollups() -> None:
        report = await run_rollups(db)
        log.debug("rollups: %s", asdict(report))
        drift = await run_drift(db)
        log.log(
            logging.INFO if drift.opened or drift.resolved else logging.DEBUG,
            "drift: %s",
            asdict(drift),
        )
        lifecycle = await run_lifecycle(db)
        level = logging.INFO if lifecycle.opened or lifecycle.resolved else logging.DEBUG
        log.log(level, "lifecycle: %s", asdict(lifecycle))
        risks = await run_risks(db)
        log.log(
            logging.INFO if risks.opened or risks.resolved else logging.DEBUG,
            "risks: %s",
            asdict(risks),
        )

    async def retention() -> None:
        report = await run_retention(db)
        log.info("retention: %s", asdict(report))

    loops = [
        run_periodically(status, status_every_s, stop),
        run_periodically(rollups, rollups_every_s, stop),
        run_periodically(retention, retention_every_s, stop),
    ]
    if alerts is not None:
        jobs = alerts

        async def deliver() -> None:
            events = await run_events(db)
            sent = await jobs.worker.run()
            busy = events.events or sent.sent or sent.failed
            log.log(logging.INFO if busy else logging.DEBUG, "alerts: %s %s", events.events, sent)

        async def weekly() -> None:
            if count := await jobs.worker.run_weekly(jobs.digests):
                log.info("weekly digests sent: %d", count)

        loops += [
            run_periodically(deliver, alerts_every_s, stop),
            run_periodically(weekly, WEEKLY_EVERY_S, stop),
        ]
        if (bot := jobs.bot) is not None:

            async def telegram() -> None:
                await bot.poll()

            loops.append(run_periodically(telegram, TELEGRAM_EVERY_S, stop))
    await asyncio.gather(*loops)


def main() -> None:
    settings = load_settings_or_exit()
    configure_logging(settings)

    async def run() -> None:
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, stop.set)
        db = Database(settings.database_url.get_secret_value(), pool_size=3)
        redis = Redis.from_url(settings.redis_url.get_secret_value())
        http = OutboundClient(
            user_agent=build_user_agent(settings.public_base, settings.contact_email)
        )
        log.info("engine started (environment=%s)", settings.environment)
        try:
            await serve(db, stop, alerts=build_alerts(settings, db, redis, http))
        finally:
            await http.aclose()
            await redis.aclose()
            await db.dispose()
            log.info("engine stopped")

    asyncio.run(run())
