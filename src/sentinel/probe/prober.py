"""The prober loop: fetch jobs, run them when due, buffer results, ship them signed.

Delivery is at-least-once. A result leaves the local buffer only after the core
accepted it; the core deduplicates by result id, so a batch that landed but whose
answer was lost is harmless to resend. Every attempt uses a fresh batch id, so a
replay answer (409) can only mean the very same bytes arrived before.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from sentinel.probe.buffer import ResultBuffer
from sentinel.probe.health import NodeHealth
from sentinel.probe.scheduler import Scheduler
from sentinel.security.internal import CoreClient, CoreRejectedError, CoreUnavailableError
from sentinel.sources.adapters import AdapterEnv, ProbeJob, ProbeOutcome, adapter_for

log = logging.getLogger("sentinel.prober")

BATCH_SIZE = 500
BATCH_BYTES = 2 * 1024 * 1024  # the core accepts request bodies up to 3 MB
MAX_DETAILS_BYTES = 4096  # the core refuses larger details
MAX_CONCURRENT_JOBS = 16
MAX_CRAWL_DELAY_S = 24 * 3600.0
STOP_GRACE_S = 10.0
_AUTH_FAILURES = frozenset({401, 403})


@dataclass(frozen=True)
class Intervals:
    jobs_refresh_s: float = 60.0
    ship_every_s: float = 10.0
    tick_s: float = 1.0  # longest sleep between scheduler checks


class Prober:
    def __init__(
        self,
        *,
        core: CoreClient,
        env: AdapterEnv,
        buffer: ResultBuffer,
        health: NodeHealth,
        scheduler: Scheduler | None = None,
        intervals: Intervals | None = None,
        max_concurrent: int = MAX_CONCURRENT_JOBS,
    ) -> None:
        self.core = core
        self.env = env
        self.buffer = buffer
        self.health = health
        self.scheduler = scheduler if scheduler is not None else Scheduler(clock=env.monotonic)
        self._intervals = intervals or Intervals()
        self._slots = asyncio.Semaphore(max_concurrent)
        self._running: set[asyncio.Task[None]] = set()

    # --- jobs ------------------------------------------------------------------

    async def refresh_jobs(self) -> bool:
        """Replace the job set from the core; on failure keep probing the old one."""
        try:
            wire = await self.core.jobs()
        except (CoreUnavailableError, CoreRejectedError) as exc:
            log.warning("job list unavailable, keeping %d jobs: %s", len(self.scheduler), exc)
            return False
        jobs: list[ProbeJob] = []
        for item in wire:
            try:
                jobs.append(ProbeJob.from_wire(item))
            except (KeyError, TypeError, ValueError):
                log.warning("skipping a malformed job from the core")
        self.scheduler.update(jobs)
        return True

    async def run_due(self) -> list[asyncio.Task[None]]:
        """Start every due job; returns the started tasks (tests await them)."""
        to_run, limited = self.scheduler.due()
        if limited:
            await self._store(
                [self._result(job, ProbeOutcome(False, "rate_limited_by_us")) for job in limited]
            )
        started = []
        for job in to_run:
            task = asyncio.create_task(self._run_one(job), name=f"probe:{job.key}")
            self._running.add(task)
            task.add_done_callback(self._running.discard)
            started.append(task)
        return started

    async def _run_one(self, job: ProbeJob) -> None:
        outcome = ProbeOutcome(False, "error")
        try:
            async with self._slots:
                adapter = adapter_for(job.kind)
                if adapter is None:
                    outcome = ProbeOutcome(False, "unsupported", details={"kind": job.kind[:40]})
                else:
                    outcome = await adapter.run(job, self.env)
            await self._store([self._result(job, outcome)])
        finally:
            self.scheduler.done(job, outcome)
            self._honour_crawl_delay(job)

    def _honour_crawl_delay(self, job: ProbeJob) -> None:
        url = job.config.get("url")
        if job.ignores_robots or not isinstance(url, str):
            return
        delay = self.env.robots.crawl_delay(url)
        if delay:
            self.scheduler.hold_host(job.host, min(delay, MAX_CRAWL_DELAY_S))

    def _result(self, job: ProbeJob, outcome: ProbeOutcome) -> dict[str, Any]:
        details = outcome.details
        if len(json.dumps(details, default=str)) > MAX_DETAILS_BYTES:
            details = {"truncated": True}
        result: dict[str, Any] = {
            "id": str(uuid.uuid4()),
            "source_ids": list(job.source_ids),
            "observed_at": self.env.now().isoformat(),
            "ok": outcome.ok,
            "error_class": outcome.error_class,
            "status_code": outcome.status_code,
            "latency_ms": outcome.latency_ms,
            "node_healthy": self.health.healthy,
            "details": details,
        }
        if outcome.snapshot is not None:
            result["snapshot"] = outcome.snapshot.to_wire()
        return result

    async def _store(self, results: list[dict[str, Any]]) -> None:
        dropped = await self.buffer.add(results)
        if dropped:
            log.warning("buffer full: dropped the %d oldest results", dropped)

    # --- shipping --------------------------------------------------------------

    async def ship(self) -> int:
        """Send buffered results until the buffer is empty or the core is unreachable."""
        shipped = 0
        while batch := await self.buffer.take(BATCH_SIZE, BATCH_BYTES):
            try:
                await self.core.send(uuid.uuid4(), batch)
            except CoreUnavailableError as exc:
                log.warning("core unavailable, %d results stay buffered: %s", len(batch), exc)
                break
            except CoreRejectedError as exc:
                if exc.status_code in _AUTH_FAILURES:
                    log.error(
                        "core refused this node (HTTP %d); results stay buffered", exc.status_code
                    )
                    break
                isolated = await self._isolate(batch)
                if isolated is None:
                    break
                shipped += isolated
                continue
            await self.buffer.ack([r["id"] for r in batch])
            shipped += len(batch)
        return shipped

    async def _isolate(self, batch: list[dict[str, Any]]) -> int | None:
        """One bad result must not block the rest: send one by one, drop the refused.

        Returns ``None`` when the core became unreachable or refused the node midway;
        the results not sent yet stay buffered.
        """
        shipped = 0
        for result in batch:
            try:
                await self.core.send(uuid.uuid4(), [result])
            except CoreUnavailableError as exc:
                log.warning("core unavailable while isolating a refused batch: %s", exc)
                return None
            except CoreRejectedError as exc:
                if exc.status_code in _AUTH_FAILURES:
                    log.error("core refused this node (HTTP %d)", exc.status_code)
                    return None
                log.error("core refused result %s, dropping it: %s", result["id"], exc)
            else:
                shipped += 1
            await self.buffer.ack([result["id"]])
        return shipped

    # --- process ---------------------------------------------------------------

    async def run(self, stop: asyncio.Event) -> None:
        """Probe until ``stop`` is set, then let running jobs finish and ship once more."""
        if not self.health.enabled:
            log.warning("PROBE_CONTROL_TARGETS is empty: node self-health is not checked")
        await self.health.refresh(force=True)
        await self.refresh_jobs()
        log.info("probing %d jobs", len(self.scheduler))
        loops = [
            self._every(self.refresh_jobs, self._intervals.jobs_refresh_s, stop),
            self._every(self.ship, self._intervals.ship_every_s, stop),
            self._every(self.health.refresh, self.health.every_s, stop),
            self._probe_loop(stop),
        ]
        try:
            await asyncio.gather(*loops)
        finally:
            await self._drain()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self.ship(), timeout=STOP_GRACE_S)

    async def _probe_loop(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            await self.run_due()
            pause = min(self._intervals.tick_s, self.scheduler.seconds_until_next())
            await _sleep_or_stop(stop, max(pause, 0.05))

    async def _drain(self) -> None:
        if not self._running:
            return
        _, pending = await asyncio.wait(set(self._running), timeout=STOP_GRACE_S)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)

    async def _every(
        self, job: Callable[[], Awaitable[object]], every_s: float, stop: asyncio.Event
    ) -> None:
        await _sleep_or_stop(stop, every_s)  # run() already did the first round
        while not stop.is_set():
            try:
                await job()
            except Exception:
                log.exception("prober task failed")
            await _sleep_or_stop(stop, every_s)


async def _sleep_or_stop(stop: asyncio.Event, seconds: float) -> None:
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(stop.wait(), timeout=seconds)
