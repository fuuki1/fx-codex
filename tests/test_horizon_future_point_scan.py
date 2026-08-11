"""The bisected future-point scan must return exactly what the linear scan did.

Horizon scoring spent 91% of its runtime in ``_future_point_and_path``, calling
``market.age`` 58 million times per run. The rewrite relies on one property:
market-open age is non-decreasing in ``point.ts``, because closed weekend spans
only ever subtract time. That makes the ages inside the tolerance window a
single contiguous run, so its edges can be bisected.

If that property ever broke, the bisection would silently pick a different point
rather than fail loudly — so the differential test below is the real guard.
"""

from __future__ import annotations

from bisect import bisect_left
from datetime import datetime, timedelta, UTC
import random

from fx_intel.horizon_learning import (
    PricePoint,
    WEEKEND_CLOSURE,
    _future_point_and_path,
)
from fx_intel.horizons import HORIZON_SPECS, HorizonSpec
from fx_intel.market import WeekendOpenHours


def _linear_scan(
    points: list[PricePoint], ts: datetime, spec: HorizonSpec
) -> tuple[PricePoint | None, list[PricePoint]]:
    """The pre-optimisation implementation, kept as the reference oracle."""

    stamps = [point.ts for point in points]
    lower = ts + timedelta(hours=max(0.0, spec.hours - spec.tolerance_hours))
    upper = ts + timedelta(hours=spec.hours + spec.tolerance_hours) + WEEKEND_CLOSURE
    best: tuple[float, PricePoint] | None = None
    start_index = bisect_left(stamps, ts)
    open_hours = WeekendOpenHours(ts, upper)
    for point in points[bisect_left(stamps, lower) :]:
        if point.ts > upper:
            break
        age = open_hours.age(point.ts)
        if spec.hours - spec.tolerance_hours <= age <= spec.hours + spec.tolerance_hours:
            gap = abs(age - spec.hours)
            if best is None or gap < best[0]:
                best = (gap, point)
    if best is None:
        return None, []
    path = [point for point in points[start_index:] if ts < point.ts <= best[1].ts]
    return best[1], path


def _series(base: datetime, count: int, step_minutes: int, rng: random.Random):
    points: list[PricePoint] = []
    moment = base
    for _ in range(count):
        moment = moment + timedelta(minutes=step_minutes * rng.choice([1, 1, 1, 2, 6]))
        close = 150 + rng.uniform(-2, 2)
        points.append(PricePoint(moment, close, close + 0.1, close - 0.1))
    return points


def test_matches_the_linear_scan_across_random_series() -> None:
    rng = random.Random(11)
    checked = 0
    for _ in range(120):
        base = datetime(2026, 7, 20, tzinfo=UTC) + timedelta(hours=rng.uniform(0, 500))
        points = _series(base, rng.randint(5, 300), rng.choice([5, 15, 60]), rng)
        stamps = [point.ts for point in points]
        for spec in HORIZON_SPECS:
            ts = base + timedelta(hours=rng.uniform(0, 40))
            assert _future_point_and_path(points, ts, spec, stamps) == _linear_scan(
                points, ts, spec
            )
            checked += 1
    assert checked > 0


def test_cached_stamps_do_not_change_the_answer() -> None:
    """Passing the caller's cached stamp list must be a pure optimisation."""

    rng = random.Random(5)
    base = datetime(2026, 8, 1, tzinfo=UTC)
    points = _series(base, 200, 15, rng)
    stamps = [point.ts for point in points]
    for spec in HORIZON_SPECS:
        ts = base + timedelta(hours=3)
        assert _future_point_and_path(points, ts, spec, stamps) == _future_point_and_path(
            points, ts, spec
        )


def test_empty_series_resolves_to_nothing() -> None:
    for spec in HORIZON_SPECS:
        assert _future_point_and_path([], datetime(2026, 8, 1, tzinfo=UTC), spec) == (None, [])


def test_series_ending_before_the_window_resolves_to_nothing() -> None:
    base = datetime(2026, 8, 3, tzinfo=UTC)
    points = _series(base, 5, 5, random.Random(3))
    spec = max(HORIZON_SPECS, key=lambda item: item.hours)
    ts = base + timedelta(hours=spec.hours * 4)
    assert _future_point_and_path(points, ts, spec) == _linear_scan(points, ts, spec)
