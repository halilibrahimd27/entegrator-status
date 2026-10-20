"""Consensus rules, table-driven (CLAUDE.md "Status semantics", PLAN Phase 4 acceptance)."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from sentinel.engine.consensus import (
    Machine,
    Round,
    Sample,
    State,
    Verdict,
    combine,
    judge,
    percentile,
    round_start,
    step,
)

T0 = datetime(2026, 10, 20, 9, 0, tzinfo=UTC)
CODES = {"F": Verdict.FAIL, "S": Verdict.SUSPECT, "O": Verdict.OK, "U": Verdict.UNKNOWN}
NAMES = {
    State.OPERATIONAL: "operational",
    State.DEGRADED: "degraded",
    State.UNVERIFIED: "unverified",
    State.DOWN: "down",
    State.UNKNOWN: "unknown",
}


def at(minute: float) -> datetime:
    return T0 + timedelta(minutes=minute)


def play(
    code: str, *, p95: Sequence[int | None] | None = None, threshold_ms: int | None = None
) -> list[tuple[int, str]]:
    """Feed one round per character (60 s each); returns (minute of change, new state)."""
    machine = Machine()
    changes: list[tuple[int, str]] = []
    for i, char in enumerate(code):
        rnd = Round(
            start=at(i),
            interval_s=60,
            verdict=CODES[char],
            p95_ms=p95[i] if p95 else None,
            error_class="http_5xx" if char in "FS" else None,
        )
        machine, change = step(machine, rnd, threshold_ms=threshold_ms)
        if change:
            changes.append((int((change.at - T0).total_seconds() // 60), NAMES[change.state]))
    return changes


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("O", [(0, "operational")]),
        # one node failing is never "down", however long it lasts
        ("SSSSSSSS", [(0, "unverified")]),
        ("FF", [(0, "unverified")]),
        # two nodes failing three rounds: down, dated back to the first failing round
        ("OFFF", [(0, "operational"), (1, "unverified"), (1, "down")]),
        # a gap in the data does not break the streak
        ("FFUF", [(0, "unverified"), (0, "down")]),
        # a round where only one node fails does
        ("FFSFF", [(0, "unverified")]),
        # recovery needs three successes, dated back to the first one
        ("FFFOO", [(0, "unverified"), (0, "down")]),
        ("FFFOOO", [(0, "unverified"), (0, "down"), (3, "operational")]),
        ("FFFOOSOOO", [(0, "unverified"), (0, "down"), (6, "operational")]),
        # alternating results: no "down", so no incident spam
        ("FOFOFOFO", [(i, "operational" if i % 2 else "unverified") for i in range(8)]),
        ("FFFOFOFO", [(0, "unverified"), (0, "down")]),
        # no data for three rounds: unknown, but never out of "down"
        ("OUUU", [(0, "operational"), (1, "unknown")]),
        ("OUUO", [(0, "operational")]),
        ("FFFUUUUUU", [(0, "unverified"), (0, "down")]),
        ("UUUO", [(3, "operational")]),
    ],
)
def test_state_machine(code: str, expected: list[tuple[int, str]]) -> None:
    assert play(code) == expected


def test_alternating_results_never_go_down() -> None:
    assert all(state != "down" for _, state in play("FO" * 50))
    assert all(state != "down" for _, state in play("FFO" * 30))


def test_degraded_after_fifteen_slow_minutes() -> None:
    slow, fast = 900, 100
    assert play("O" * 14, p95=[slow] * 14, threshold_ms=500) == [(0, "operational")]
    changes = play("O" * 16, p95=[slow] * 15 + [fast], threshold_ms=500)
    assert changes == [(0, "operational"), (0, "degraded"), (15, "operational")]
    # a fast round in between restarts the clock
    blip = [slow] * 10 + [fast] + [slow] * 10
    assert play("O" * 21, p95=blip, threshold_ms=500) == [(0, "operational")]
    # no threshold, no degraded
    assert play("O" * 20, p95=[slow] * 20) == [(0, "operational")]


def test_recovery_into_degraded_and_causes() -> None:
    code = "FFF" + "O" * 20
    p95: list[int | None] = [None] * 3 + [900] * 20
    assert play(code, p95=p95, threshold_ms=500) == [
        (0, "unverified"),
        (0, "down"),
        (3, "operational"),
        (3, "degraded"),
    ]
    machine = Machine()
    for i, cause in enumerate(["tls", "read_timeout", "read_timeout"]):
        rnd = Round(start=at(i), interval_s=60, verdict=Verdict.FAIL, error_class=cause)
        machine, change = step(machine, rnd)
    assert change is not None
    assert change.state is State.DOWN
    assert change.cause == "read_timeout"


# --- judging one round -----------------------------------------------------------------


def sample(node: str, second: float, ok: bool = True, **kwargs: Any) -> Sample:
    if not ok:
        kwargs.setdefault("error_class", "http_5xx")
    return Sample(node_id=node, observed_at=at(second / 60), ok=ok, **kwargs)


@pytest.mark.parametrize(
    ("samples", "verdict"),
    [
        ([sample("a", 10, False), sample("b", 20, False)], Verdict.FAIL),
        ([sample("a", 10, False), sample("b", 20)], Verdict.SUSPECT),
        ([sample("a", 10, False)], Verdict.SUSPECT),
        ([sample("a", 10), sample("b", 20)], Verdict.OK),
        ([], Verdict.UNKNOWN),
        # an unhealthy node is left out: our network, not the provider
        ([sample("a", 10, False, node_healthy=False), sample("b", 20)], Verdict.OK),
        ([sample("a", 10, False, node_healthy=False)], Verdict.UNKNOWN),
        # results that say nothing about the provider are left out
        ([sample("a", 10, False, error_class="blocked_by_robots")], Verdict.UNKNOWN),
        ([sample("a", 10, False, error_class="rate_limited_by_us")], Verdict.UNKNOWN),
        # each node counts once, by its latest result
        ([sample("a", 10, False), sample("a", 40), sample("b", 20, False)], Verdict.SUSPECT),
        # a node with no result inside the round still counts by a recent one
        ([sample("a", -20, False), sample("b", 30, False)], Verdict.FAIL),
        # …but not by a stale one
        ([sample("a", -40, False), sample("b", 30, False)], Verdict.SUSPECT),
        ([sample("a", 60, False), sample("b", 30, False)], Verdict.SUSPECT),
    ],
)
def test_judge(samples: list[Sample], verdict: Verdict) -> None:
    assert judge(samples, T0, 60).verdict is verdict


def test_judge_latency_and_cause() -> None:
    samples = [
        sample("a", -20, latency_ms=5000),  # lookback only: not this round's latency
        *[sample("a", s, latency_ms=ms) for s, ms in ((1, 100), (2, 200), (3, 300), (4, 400))],
        sample("b", 5, False, error_class="tls"),
        sample("c", 6, False, error_class="read_timeout"),
        sample("d", 7, False, error_class="read_timeout"),
    ]
    rnd = judge(samples, T0, 60)
    assert (rnd.verdict, rnd.ok_nodes, rnd.fail_nodes) == (Verdict.FAIL, 1, 3)
    assert (rnd.p50_ms, rnd.p95_ms) == (250, 385)
    assert rnd.error_class == "read_timeout"
    assert rnd.end == at(1)


def test_helpers() -> None:
    assert round_start(datetime(2026, 10, 20, 9, 7, 31, tzinfo=UTC), 300) == at(5)
    assert round_start(at(0), 60) == at(0)
    assert percentile([], 95) is None
    assert percentile([7], 95) == 7
    assert combine([]) is State.UNKNOWN
    assert combine([State.UNKNOWN, State.OPERATIONAL]) is State.OPERATIONAL
    assert combine([State.DEGRADED, State.DOWN, State.UNVERIFIED]) is State.DOWN
    assert combine([State.DEGRADED, State.UNVERIFIED]) is State.UNVERIFIED
