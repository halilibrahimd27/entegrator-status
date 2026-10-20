"""Uptime arithmetic over the consensus timeline. Pure functions, no I/O.

Time inside the owner's maintenance windows counts as ``maintenance`` whatever the
timeline says; time the timeline does not cover counts as ``unknown``. Both are left
out of the uptime ratio, as are ``unknown`` intervals. ``degraded`` and ``unverified``
count as up: the service answered, slowly or as seen by one node only.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta

UP_STATES = ("operational", "degraded", "unverified")
STATES = (*UP_STATES, "down", "unknown", "maintenance")
WINDOWS = {
    "24h": timedelta(hours=24),
    "7d": timedelta(days=7),
    "30d": timedelta(days=30),
    "90d": timedelta(days=90),
}


@dataclass(frozen=True)
class Span:
    state: str
    start: datetime
    end: datetime | None  # None: still open


@dataclass
class Tally:
    seconds: dict[str, float] = field(default_factory=lambda: dict.fromkeys(STATES, 0.0))

    @property
    def up(self) -> float:
        return sum(self.seconds[s] for s in UP_STATES)

    @property
    def down(self) -> float:
        return self.seconds["down"]

    @property
    def uptime(self) -> float | None:
        measured = self.up + self.down
        return self.up / measured if measured > 0 else None

    @property
    def worst(self) -> str:
        """The colour of a day bar."""
        if self.seconds["down"] > 0:
            return "down"
        if self.seconds["degraded"] > 0 or self.seconds["unverified"] > 0:
            return "degraded"
        if self.seconds["operational"] > 0:
            return "operational"
        return "maintenance" if self.seconds["maintenance"] > 0 else "unknown"


def merge(windows: Iterable[tuple[datetime, datetime]]) -> list[tuple[datetime, datetime]]:
    merged: list[tuple[datetime, datetime]] = []
    for start, end in sorted(windows):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def _overlap(a: datetime, b: datetime, windows: Sequence[tuple[datetime, datetime]]) -> float:
    total = 0.0
    for start, end in windows:
        lo, hi = max(a, start), min(b, end)
        if hi > lo:
            total += (hi - lo).total_seconds()
    return total


def tally(
    spans: Iterable[Span],
    maintenance: Iterable[tuple[datetime, datetime]],
    start: datetime,
    end: datetime,
    now: datetime,
) -> Tally:
    """Seconds per state in ``[start, min(end, now))``."""
    result = Tally()
    end = min(end, now)
    if end <= start:
        return result
    windows = [(max(a, start), min(b, end)) for a, b in merge(maintenance) if b > start and a < end]
    counted = 0.0
    for span in spans:  # timeline intervals never overlap
        a = max(span.start, start)
        b = min(span.end or now, end)
        if b <= a:
            continue
        seconds = (b - a).total_seconds() - _overlap(a, b, windows)
        result.seconds[span.state] = result.seconds.get(span.state, 0.0) + seconds
        counted += seconds
    in_maintenance = sum((b - a).total_seconds() for a, b in windows)
    result.seconds["maintenance"] = in_maintenance
    total = (end - start).total_seconds()
    result.seconds["unknown"] += max(0.0, total - in_maintenance - counted)
    return result


def day_bounds(day: date) -> tuple[datetime, datetime]:
    start = datetime.combine(day, time(), tzinfo=UTC)
    return start, start + timedelta(days=1)
