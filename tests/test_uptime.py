"""Uptime arithmetic on hand-computed timelines (UTC days, rendered in Europe/Istanbul)."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from sentinel.engine.uptime import Span, Tally, day_bounds, merge, tally
from sentinel.web.templating import local_date, local_time, percent

DAY = date(2026, 10, 19)
START, END = day_bounds(DAY)
NEXT = END + timedelta(hours=6)  # "now", after the day


def at(hour: float) -> datetime:
    return START + timedelta(hours=hour)


def minutes(t: Tally, state: str) -> float:
    return t.seconds[state] / 60


def test_a_day_with_a_half_hour_outage() -> None:
    spans = [
        Span("operational", at(0), at(12)),
        Span("down", at(12), at(12.5)),
        Span("operational", at(12.5), None),
    ]
    t = tally(spans, [], START, END, NEXT)
    assert minutes(t, "operational") == 23.5 * 60
    assert minutes(t, "down") == 30
    assert t.uptime == pytest.approx(23.5 / 24)
    assert t.worst == "down"


def test_maintenance_is_left_out_even_when_it_overlaps_an_outage() -> None:
    spans = [
        Span("operational", at(0), at(12)),
        Span("down", at(12), at(12.5)),
        Span("operational", at(12.5), None),
    ]
    windows = [(at(11.9), at(12.2)), (at(12.1), at(12.25))]  # overlapping: merged
    t = tally(spans, windows, START, END, NEXT)
    assert minutes(t, "maintenance") == pytest.approx(21)  # 11:54 → 12:15
    assert minutes(t, "down") == pytest.approx(15)  # 12:15 → 12:30
    assert minutes(t, "operational") == pytest.approx(23.5 * 60 - 6)
    assert t.uptime == pytest.approx((23.5 * 60 - 6) / (23.5 * 60 - 6 + 15))


def test_unknown_time_and_states_count_as_neither_up_nor_down() -> None:
    spans = [
        Span("unverified", at(6), at(8)),  # one node saw failures: still up
        Span("degraded", at(8), at(9)),
        Span("unknown", at(9), at(10)),
        Span("down", at(10), at(11)),
    ]
    t = tally(spans, [(at(20), at(21))], START, END, NEXT)
    # unknown: hours 0 to 6, 9 to 10 and 11 to 24, minus the maintenance hour
    assert minutes(t, "unknown") == pytest.approx((6 + 1 + 13 - 1) * 60)
    assert minutes(t, "maintenance") == 60  # maintenance outside the timeline is still maintenance
    assert t.up == pytest.approx(3 * 3600)
    assert t.uptime == pytest.approx(3 / 4)
    assert sum(t.seconds.values()) == pytest.approx(24 * 3600)


def test_the_current_day_stops_at_now() -> None:
    t = tally([Span("operational", at(0), None)], [], START, END, at(6))
    assert minutes(t, "operational") == 6 * 60
    assert minutes(t, "unknown") == 0
    assert tally([], [], START, END, at(-1)).uptime is None  # the day has not started


@pytest.mark.parametrize(
    ("seconds", "worst"),
    [
        ({"operational": 10, "down": 1}, "down"),
        ({"operational": 10, "unverified": 1}, "degraded"),
        ({"operational": 10, "maintenance": 5}, "operational"),
        ({"maintenance": 5}, "maintenance"),
        ({}, "unknown"),
    ],
)
def test_bar_colour(seconds: dict[str, float], worst: str) -> None:
    t = Tally()
    t.seconds.update(seconds)
    assert t.worst == worst


def test_utc_days_rendered_in_istanbul() -> None:
    # 22:30 UTC on 19 October is 01:30 on 20 October in Istanbul (UTC+3): the outage
    # belongs to the UTC day of the 19th, and its time is shown in Istanbul time.
    outage = Span("down", at(22.5), at(23))
    t = tally([outage], [], START, END, NEXT)
    assert minutes(t, "down") == 30
    assert local_time(outage.start) == "20.10.2026 01:30"
    assert local_date(DAY) == "19.10.2026"
    assert day_bounds(DAY) == (
        datetime(2026, 10, 19, tzinfo=UTC),
        datetime(2026, 10, 20, tzinfo=UTC),
    )


def test_helpers() -> None:
    assert merge([(at(3), at(4)), (at(1), at(2)), (at(1.5), at(2.5))]) == [
        (at(1), at(2.5)),
        (at(3), at(4)),
    ]
    assert percent(0.999512) == "%99,95"
    assert percent(1.0) == "%100,00"
    assert percent(None) == "—"
