"""When each job runs next. Pure logic over an injectable clock (no I/O).

- First runs are spread with random jitter so a fresh node does not hit every host at
  once; later runs keep their interval with ±10 % jitter.
- A host that asks us to slow down (``Retry-After``, ``Crawl-delay``) is held; jobs on
  other hosts are unaffected.
- Consecutive ``429``, or ``503`` with ``Retry-After``, back the job off exponentially
  (up to 8x its interval). A plain 5xx does not: an outage must not hide its recovery.
- While its host is held for longer than its own interval, a job is reported as
  ``rate_limited_by_us`` once per interval instead of silently disappearing.
"""

from __future__ import annotations

import random
import time
from collections.abc import Callable
from dataclasses import dataclass

from sentinel.sources.adapters.base import ProbeJob, ProbeOutcome

MAX_INITIAL_JITTER_S = 60.0
MAX_BACKOFF_FACTOR = 8


@dataclass
class _Entry:
    job: ProbeJob
    next_at: float
    failures: int = 0


class Scheduler:
    def __init__(
        self, *, clock: Callable[[], float] = time.monotonic, rng: random.Random | None = None
    ) -> None:
        self._clock = clock
        self._rng = rng or random.Random()  # noqa: S311  # nosec B311 - jitter, not crypto
        self._entries: dict[str, _Entry] = {}
        self._held_until: dict[str, float] = {}

    def __len__(self) -> int:
        return len(self._entries)

    def update(self, jobs: list[ProbeJob]) -> None:
        """Replace the job set; unchanged keys keep their timing."""
        now = self._clock()
        fresh = {job.key: job for job in jobs}
        for key in list(self._entries):
            if key not in fresh:
                del self._entries[key]
        for key, job in fresh.items():
            if key in self._entries:
                self._entries[key].job = job
            else:
                jitter = self._rng.uniform(0, min(float(job.interval_s), MAX_INITIAL_JITTER_S))
                self._entries[key] = _Entry(job=job, next_at=now + jitter)

    def hold_host(self, host: str, seconds: float) -> None:
        if host and seconds > 0:
            until = self._clock() + seconds
            self._held_until[host] = max(self._held_until.get(host, 0.0), until)

    def held_for(self, host: str) -> float:
        return max(0.0, self._held_until.get(host, 0.0) - self._clock())

    def due(self) -> tuple[list[ProbeJob], list[ProbeJob]]:
        """``(to_run, rate_limited)``: jobs due now, and jobs skipped because of a long hold."""
        now = self._clock()
        to_run: list[ProbeJob] = []
        limited: list[ProbeJob] = []
        for entry in self._entries.values():
            if entry.next_at > now:
                continue
            hold = self.held_for(entry.job.host)
            if hold <= 0:
                to_run.append(entry.job)
            elif hold > entry.job.interval_s:
                entry.next_at = now + entry.job.interval_s
                limited.append(entry.job)
            else:
                entry.next_at = now + hold  # runs as soon as the host is released
        for job in to_run:  # not due again until done() reschedules it
            self._entries[job.key].next_at = float("inf")
        return to_run, limited

    def done(self, job: ProbeJob, outcome: ProbeOutcome) -> None:
        entry = self._entries.get(job.key)
        if entry is None:
            return  # removed by an update while running
        if outcome.retry_after_s:
            self.hold_host(job.host, outcome.retry_after_s)
        slow_down = outcome.status_code == 429 or (
            outcome.status_code == 503 and outcome.retry_after_s is not None
        )
        entry.failures = entry.failures + 1 if slow_down else 0
        factor = min(2**entry.failures, MAX_BACKOFF_FACTOR)
        entry.next_at = self._clock() + job.interval_s * factor * self._rng.uniform(0.9, 1.1)

    def seconds_until_next(self) -> float:
        if not self._entries:
            return 1.0
        return max(0.0, min(e.next_at for e in self._entries.values()) - self._clock())
