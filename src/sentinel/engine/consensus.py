"""Consensus: node results → round verdicts → per-source state, with hysteresis.

Pure logic, no I/O (CLAUDE.md "Status semantics"):

- A **round** is one interval of a source (aligned to the epoch). Each healthy node
  counts once per round, by its latest result within ``LOOKBACK`` intervals before the
  round ends: nodes jitter their schedule, so a node may have no result inside one
  particular round. Latency percentiles use only results inside the round. Results of
  unhealthy nodes and results that say nothing about the provider
  (``blocked_by_robots``, ``rate_limited_by_us``, our own errors, …) are left out.
- Round verdict: ``fail`` when ≥ 2 nodes fail, ``suspect`` when exactly one does,
  ``ok`` when nodes answered and none failed, ``unknown`` when no node counted.
- ``down`` needs ``confirm_rounds`` consecutive ``fail`` rounds and is dated back to the
  first of them; recovery needs ``recover_rounds`` consecutive ``ok`` rounds and is
  dated back to the first of those. A data gap never ends ``down``.
- ``degraded``: the round p95 latency stayed above the source's threshold for
  ``degraded_after``; dated back to when it started.
"""

from __future__ import annotations

import statistics
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass, field, fields, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any

# Kinds whose failures mean "callers cannot reach it". Expiry signals (domain_expiry)
# are risks with a date, handled by alerts, not availability.
AVAILABILITY_KINDS = frozenset({"http", "dns", "tls_cert"})
FAILURE_CLASSES = frozenset(
    {
        "dns",
        "connect",
        "connect_timeout",
        "read_timeout",
        "tls",
        "http_4xx",
        "http_5xx",
        "assertion",
        "too_many_redirects",
    }
)
CONFIRM_ROUNDS = 3
RECOVER_ROUNDS = 3
UNKNOWN_ROUNDS = 3
DEGRADED_AFTER = timedelta(minutes=15)
LOOKBACK = 1.5  # intervals; a node's period is at most 1.1 intervals
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


class Verdict(StrEnum):
    OK = "ok"
    SUSPECT = "suspect"
    FAIL = "fail"
    UNKNOWN = "unknown"


class State(StrEnum):
    OPERATIONAL = "operational"
    DEGRADED = "degraded"
    UNVERIFIED = "unverified"
    DOWN = "down"
    UNKNOWN = "unknown"  # no usable data yet, or for a while


SEVERITY = {
    State.UNKNOWN: 0,
    State.OPERATIONAL: 1,
    State.DEGRADED: 2,
    State.UNVERIFIED: 3,
    State.DOWN: 4,
}


@dataclass(frozen=True)
class Sample:
    node_id: str
    observed_at: datetime
    ok: bool
    error_class: str | None = None
    latency_ms: int | None = None
    node_healthy: bool = True

    @property
    def counts(self) -> bool:
        return self.node_healthy and (self.ok or self.error_class in FAILURE_CLASSES)


@dataclass(frozen=True)
class Round:
    start: datetime
    interval_s: int
    verdict: Verdict
    ok_nodes: int = 0
    fail_nodes: int = 0
    p50_ms: int | None = None
    p95_ms: int | None = None
    error_class: str | None = None

    @property
    def end(self) -> datetime:
        return self.start + timedelta(seconds=self.interval_s)


def round_start(at: datetime, interval_s: int) -> datetime:
    elapsed = int((at - _EPOCH).total_seconds())
    return _EPOCH + timedelta(seconds=elapsed - elapsed % interval_s)


def percentile(values: Sequence[int], pct: int) -> int | None:
    if not values:
        return None
    if len(values) == 1:
        return values[0]
    cuts = statistics.quantiles(values, n=100, method="inclusive")
    return round(cuts[pct - 1])


def lookback_start(start: datetime, interval_s: int) -> datetime:
    """The earliest result that may still speak for a node in the round at ``start``."""
    return start + timedelta(seconds=interval_s * (1 - LOOKBACK))


def judge(samples: Iterable[Sample], start: datetime, interval_s: int) -> Round:
    """The verdict of one round from the samples of its lookback window."""
    end = start + timedelta(seconds=interval_s)
    earliest = lookback_start(start, interval_s)
    counted = [s for s in samples if s.counts and earliest <= s.observed_at < end]
    latest: dict[str, Sample] = {}
    for sample in sorted(counted, key=lambda s: s.observed_at):
        latest[sample.node_id] = sample
    failing = [s for s in latest.values() if not s.ok]
    ok_nodes = len(latest) - len(failing)
    if len(failing) >= 2:
        verdict = Verdict.FAIL
    elif failing:
        verdict = Verdict.SUSPECT
    elif ok_nodes:
        verdict = Verdict.OK
    else:
        verdict = Verdict.UNKNOWN
    latencies = sorted(
        s.latency_ms
        for s in counted
        if s.ok and s.latency_ms is not None and s.observed_at >= start
    )
    causes = Counter(s.error_class for s in failing if s.error_class)
    cause = min(causes, key=lambda c: (-causes[c], c)) if causes else None
    return Round(
        start=start,
        interval_s=interval_s,
        verdict=verdict,
        ok_nodes=ok_nodes,
        fail_nodes=len(failing),
        p50_ms=percentile(latencies, 50),
        p95_ms=percentile(latencies, 95),
        error_class=cause,
    )


@dataclass(frozen=True)
class Transition:
    state: State
    at: datetime
    cause: str | None = None


@dataclass(frozen=True)
class Rules:
    confirm_rounds: int = CONFIRM_ROUNDS
    recover_rounds: int = RECOVER_ROUNDS
    unknown_rounds: int = UNKNOWN_ROUNDS
    degraded_after: timedelta = DEGRADED_AFTER


@dataclass(frozen=True)
class Machine:
    """Per-source state between rounds (persisted by the engine)."""

    state: State = State.UNKNOWN
    fail_streak: int = 0
    fail_since: datetime | None = None
    ok_streak: int = 0
    ok_since: datetime | None = None
    unknown_streak: int = 0
    unknown_since: datetime | None = None
    slow_since: datetime | None = None
    causes: dict[str, int] = field(default_factory=dict)

    @property
    def cause(self) -> str | None:
        if not self.causes:
            return None
        return min(self.causes, key=lambda c: (-self.causes[c], c))

    def to_json(self) -> dict[str, Any]:
        data: dict[str, Any] = {}
        for name, value in asdict(self).items():
            data[name] = value.isoformat() if isinstance(value, datetime) else value
        data["state"] = self.state.value
        return data

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> Machine:
        if not data:
            return cls()
        values = dict(data)
        for name in ("fail_since", "ok_since", "unknown_since", "slow_since"):
            if values.get(name):
                values[name] = datetime.fromisoformat(values[name])
        values["state"] = State(values.get("state", State.UNKNOWN))
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in values.items() if k in known})


def step(
    machine: Machine, rnd: Round, *, threshold_ms: int | None = None, rules: Rules | None = None
) -> tuple[Machine, Transition | None]:
    """Apply one round; returns the new machine and the state change, if any."""
    rules = rules or Rules()
    m = machine
    if rnd.verdict is Verdict.UNKNOWN:
        since = (m.unknown_since or rnd.start) if m.unknown_streak else rnd.start
        m = replace(m, unknown_streak=m.unknown_streak + 1, unknown_since=since)
        if m.unknown_streak >= rules.unknown_rounds and m.state not in (State.DOWN, State.UNKNOWN):
            return _to(m, State.UNKNOWN, since)
        return m, None

    m = replace(m, unknown_streak=0, unknown_since=None)
    if rnd.verdict is Verdict.FAIL:
        since = (m.fail_since or rnd.start) if m.fail_streak else rnd.start
        causes = dict(m.causes) if m.fail_streak else {}
        if rnd.error_class:
            causes[rnd.error_class] = causes.get(rnd.error_class, 0) + 1
        m = replace(
            m,
            fail_streak=m.fail_streak + 1,
            fail_since=since,
            causes=causes,
            ok_streak=0,
            ok_since=None,
            slow_since=None,
        )
        if m.state is not State.DOWN and m.fail_streak >= rules.confirm_rounds:
            return _to(m, State.DOWN, since, m.cause)
        if m.state not in (State.DOWN, State.UNVERIFIED):
            return _to(m, State.UNVERIFIED, rnd.start, rnd.error_class)
        return m, None

    if rnd.verdict is Verdict.SUSPECT:
        m = replace(m, fail_streak=0, fail_since=None, ok_streak=0, ok_since=None, slow_since=None)
        if m.state not in (State.DOWN, State.UNVERIFIED):
            return _to(m, State.UNVERIFIED, rnd.start, rnd.error_class)
        return m, None

    # Verdict.OK
    slow = threshold_ms is not None and rnd.p95_ms is not None and rnd.p95_ms > threshold_ms
    slow_since = (m.slow_since or rnd.start) if slow else None
    ok_since = (m.ok_since or rnd.start) if m.ok_streak else rnd.start
    m = replace(
        m,
        fail_streak=0,
        fail_since=None,
        ok_streak=m.ok_streak + 1,
        ok_since=ok_since,
        slow_since=slow_since,
    )
    degraded = slow_since is not None and rnd.end - slow_since >= rules.degraded_after
    target = State.DEGRADED if degraded else State.OPERATIONAL
    if m.state is State.DOWN:
        if m.ok_streak < rules.recover_rounds:
            return m, None
        return _to(m, target, ok_since)
    if m.state is target:
        return m, None
    at = slow_since if target is State.DEGRADED and slow_since is not None else rnd.start
    return _to(m, target, at)


def _to(
    m: Machine, state: State, at: datetime, cause: str | None = None
) -> tuple[Machine, Transition]:
    return replace(m, state=state), Transition(state, at, cause)


def combine(states: Iterable[State]) -> State:
    """A component is as bad as its worst availability source; unknown ones are ignored."""
    return max(states, key=lambda s: SEVERITY[s], default=State.UNKNOWN)
