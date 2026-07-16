"""Cross-source divergence between two INDEPENDENT quote providers.

Aligns quotes from a primary and a secondary provider on nearest event time
(within a declared tolerance) and measures disagreement. On breach the pair of
sources transitions to ``degraded``/``quarantined`` — values are NEVER
averaged into a synthetic "consensus" price, and the report carries an
explicit reason so downstream research can exclude the window.

Metrics: ``mid_diff_pips``, ``spread_diff_pips``, ``event_time_skew_ms``,
``receive_time_skew_ms``, per-source freshness, unmatched (missing) rates and
tradable-state mismatches.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
import statistics
from typing import Any

from data_platform.collect.contract import CollectedQuote
from data_platform.quality.state import QualityState


class DivergenceInputError(ValueError):
    """Inputs unusable for comparison (mixed instruments, same provider…)."""


@dataclass(frozen=True)
class DivergenceThresholds:
    max_mid_diff_pips: float = 3.0
    max_spread_diff_pips: float = 5.0
    max_receive_skew_ms: float = 5_000.0
    quarantine_mid_diff_pips: float = 10.0

    def __post_init__(self) -> None:
        if not 0 < self.max_mid_diff_pips < self.quarantine_mid_diff_pips:
            raise DivergenceInputError(
                "0 < max_mid_diff_pips < quarantine_mid_diff_pips is required"
            )


def _percentiles(values: Sequence[float]) -> dict[str, float]:
    if not values:
        return {}
    ordered = sorted(values)
    quantiles = statistics.quantiles(ordered, n=100, method="inclusive") if len(ordered) > 1 else []

    def pick(p: int) -> float:
        if not quantiles:
            return ordered[0]
        return quantiles[p - 1]

    return {
        "count": float(len(ordered)),
        "p50": pick(50),
        "p95": pick(95),
        "p99": pick(99),
        "max": ordered[-1],
    }


def _event_time(quote: CollectedQuote) -> datetime:
    return quote.provider_event_time or quote.received_at


def compare_sources(
    primary: Sequence[CollectedQuote],
    secondary: Sequence[CollectedQuote],
    *,
    pip_size: float,
    thresholds: DivergenceThresholds | None = None,
    align_tolerance_seconds: float = 2.0,
) -> dict[str, Any]:
    """Compare two independent providers for ONE instrument."""

    limits = thresholds or DivergenceThresholds()
    if not primary or not secondary:
        raise DivergenceInputError("both sources must contribute at least one quote")
    instruments = {q.instrument for q in (*primary, *secondary)}
    if len(instruments) != 1:
        raise DivergenceInputError(f"mixed instruments in comparison: {sorted(instruments)}")
    providers = {q.provider for q in primary} | {q.provider for q in secondary}
    if len(providers) < 2:
        raise DivergenceInputError(
            "primary and secondary must be different providers; a second endpoint of "
            "the same provider is not an independent source"
        )
    if pip_size <= 0:
        raise DivergenceInputError("pip_size must be positive")

    sorted_secondary = sorted(secondary, key=_event_time)
    times_b = [_event_time(q) for q in sorted_secondary]
    mid_diffs: list[float] = []
    spread_diffs: list[float] = []
    event_skews: list[float] = []
    receive_skews: list[float] = []
    tradable_mismatch = 0
    matched = 0
    import bisect

    for quote in sorted(primary, key=_event_time):
        stamp = _event_time(quote)
        index = bisect.bisect_left(times_b, stamp)
        candidates = [i for i in (index - 1, index) if 0 <= i < len(times_b)]
        if not candidates:
            continue
        best = min(candidates, key=lambda i: abs((times_b[i] - stamp).total_seconds()))
        gap = abs((times_b[best] - stamp).total_seconds())
        if gap > align_tolerance_seconds:
            continue
        other = sorted_secondary[best]
        matched += 1
        mid_diffs.append(abs(quote.mid - other.mid) / pip_size)
        spread_diffs.append(abs(quote.spread - other.spread) / pip_size)
        event_skews.append((times_b[best] - stamp).total_seconds() * 1000.0)
        receive_skews.append((other.received_at - quote.received_at).total_seconds() * 1000.0)
        if quote.tradable != other.tradable:
            tradable_mismatch += 1

    unmatched_primary = len(primary) - matched
    state = QualityState.USABLE
    reasons: list[str] = []
    if matched == 0:
        state = QualityState.UNAVAILABLE
        reasons.append("no_aligned_quotes")
    else:
        worst_mid = max(mid_diffs)
        if worst_mid > limits.quarantine_mid_diff_pips:
            state = QualityState.QUARANTINED
            reasons.append(f"mid_diff_{worst_mid:.2f}pips_exceeds_quarantine")
        elif worst_mid > limits.max_mid_diff_pips:
            state = QualityState.DEGRADED
            reasons.append(f"mid_diff_{worst_mid:.2f}pips_exceeds_threshold")
        if spread_diffs and max(spread_diffs) > limits.max_spread_diff_pips:
            state = max(state, QualityState.DEGRADED, key=_severity)
            reasons.append("spread_diff_exceeds_threshold")
        if receive_skews and max(abs(v) for v in receive_skews) > limits.max_receive_skew_ms:
            state = max(state, QualityState.DEGRADED, key=_severity)
            reasons.append("receive_time_skew_exceeds_threshold")

    return {
        "instrument": next(iter(instruments)),
        "providers": sorted(providers),
        "matched_quotes": matched,
        "unmatched_primary": unmatched_primary,
        "unmatched_secondary": len(secondary) - matched,
        "tradable_mismatch_count": tradable_mismatch,
        "metrics": {
            "mid_diff_pips": _percentiles(mid_diffs),
            "spread_diff_pips": _percentiles(spread_diffs),
            "event_time_skew_ms": _percentiles([abs(v) for v in event_skews]),
            "receive_time_skew_ms": _percentiles([abs(v) for v in receive_skews]),
        },
        "divergence_state": str(state),
        "divergence_reason": reasons,
        "thresholds": {
            "max_mid_diff_pips": limits.max_mid_diff_pips,
            "max_spread_diff_pips": limits.max_spread_diff_pips,
            "max_receive_skew_ms": limits.max_receive_skew_ms,
            "quarantine_mid_diff_pips": limits.quarantine_mid_diff_pips,
        },
        "policy": "values are never averaged on divergence; state degrades instead",
    }


_SEVERITY = {
    QualityState.USABLE: 0,
    QualityState.DEGRADED: 1,
    QualityState.QUARANTINED: 2,
    QualityState.UNAVAILABLE: 3,
}


def _severity(state: QualityState) -> int:
    return _SEVERITY[state]
