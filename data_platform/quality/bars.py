"""Bar-stream quality checks: gaps, staleness, and primary/secondary divergence.

These operate on materialized :class:`~data_platform.materialize.bid_ask_bars.BidAskBar`
sequences. They *detect and report*; they never patch a stream to look healthy.
A gap is reported as a gap, not back-filled with an interpolated bar; a divergent
secondary is flagged, not silently averaged away.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import timedelta

from data_platform.materialize.bid_ask_bars import BAR_INTERVALS, BidAskBar


@dataclass(frozen=True)
class GapAudit:
    """Missing interval buckets between the first and last observed bar."""

    interval: str
    expected_bars: int
    observed_bars: int
    missing_open_times: tuple[str, ...]

    @property
    def completeness(self) -> float:
        if self.expected_bars == 0:
            return 1.0
        return self.observed_bars / self.expected_bars


def gap_audit(bars: Sequence[BidAskBar], interval: str) -> GapAudit:
    """Report which interval buckets are missing between first and last bar.

    Completeness is measured against the contiguous grid from the first to the
    last observed bar — not against an assumed 24/7 calendar — so a legitimately
    closed market does not read as thousands of gaps. Weekends/holidays are a
    calendar concern handled elsewhere.
    """

    if interval not in BAR_INTERVALS:
        raise ValueError(f"unknown bar interval {interval!r}")
    span = BAR_INTERVALS[interval]
    ordered = sorted(bars, key=lambda b: b.open_time)
    if len(ordered) < 2:
        return GapAudit(
            interval=interval,
            expected_bars=len(ordered),
            observed_bars=len(ordered),
            missing_open_times=(),
        )

    present = {bar.open_time for bar in ordered}
    start = ordered[0].open_time
    end = ordered[-1].open_time
    expected_times: list = []
    cursor = start
    while cursor <= end:
        expected_times.append(cursor)
        cursor = cursor + span
    missing = tuple(t.isoformat() for t in expected_times if t not in present)
    return GapAudit(
        interval=interval,
        expected_bars=len(expected_times),
        observed_bars=len(present),
        missing_open_times=missing,
    )


@dataclass(frozen=True)
class DivergenceReport:
    """Primary/secondary mid disagreement at co-timed bars."""

    compared_bars: int
    max_abs_mid_divergence: float
    mean_abs_mid_divergence: float
    breaches: tuple[str, ...] = field(default_factory=tuple)

    @property
    def has_breach(self) -> bool:
        return bool(self.breaches)


def source_divergence(
    primary: Sequence[BidAskBar],
    secondary: Sequence[BidAskBar],
    *,
    max_abs_mid: float,
) -> DivergenceReport:
    """Compare two sources' mid at bars sharing a ``close_time``.

    Only co-timed bars are compared; bars unique to one source are not a
    divergence (they are a coverage difference). A mid gap above ``max_abs_mid``
    is a breach — reported, never reconciled by averaging.
    """

    if max_abs_mid <= 0:
        raise ValueError("max_abs_mid must be positive")
    secondary_by_time = {bar.close_time: bar for bar in secondary}
    diffs: list[float] = []
    breaches: list[str] = []
    for bar in primary:
        other = secondary_by_time.get(bar.close_time)
        if other is None:
            continue
        diff = abs(bar.mid_close - other.mid_close)
        diffs.append(diff)
        if diff > max_abs_mid:
            breaches.append(bar.close_time.isoformat())
    if not diffs:
        return DivergenceReport(
            compared_bars=0,
            max_abs_mid_divergence=0.0,
            mean_abs_mid_divergence=0.0,
            breaches=(),
        )
    return DivergenceReport(
        compared_bars=len(diffs),
        max_abs_mid_divergence=max(diffs),
        mean_abs_mid_divergence=sum(diffs) / len(diffs),
        breaches=tuple(breaches),
    )


def max_staleness_seconds(bars: Sequence[BidAskBar]) -> float:
    """Largest intra-bar staleness across a bar stream (0.0 if empty)."""

    return max((bar.stale_seconds for bar in bars), default=0.0)


def stale_bars(bars: Sequence[BidAskBar], *, max_stale_seconds: float) -> list[BidAskBar]:
    """Bars whose intra-bar staleness exceeds the limit (candidates to flag)."""

    if max_stale_seconds < 0:
        raise ValueError("max_stale_seconds must be >= 0")
    limit = timedelta(seconds=max_stale_seconds).total_seconds()
    return [bar for bar in bars if bar.stale_seconds > limit]
