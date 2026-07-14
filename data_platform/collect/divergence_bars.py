"""Cross-source divergence between INDEPENDENT providers at BAR granularity.

Complements :mod:`data_platform.collect.divergence` (tick/quote alignment) for
sources that publish candles. Bars are joined on their exact ``open_time`` —
grid data needs no nearest-neighbour tolerance — and disagreement is measured
at the boundary instants where both sides of the book are known:

- ``mid_diff_pips``    |mid_close(A) - mid_close(B)| per matched bar
- ``spread_diff_pips`` |spread_close(A) - spread_close(B)| per matched bar
- ``receive_time_skew_ms`` |received_at(A) - received_at(B)| where the caller
  supplies per-bar receive times (for historical downloads this is honest
  *download* skew — it measures the pipeline, not market latency, and the
  report says so via ``collection_context``)

On breach the pair of sources transitions to ``degraded``/``quarantined`` —
values are NEVER averaged into a synthetic consensus, and the report carries
explicit reasons so downstream research can exclude the window.

:func:`compare_bars_to_close_series` additionally supports a close-only
external series (e.g. HistData 1h closes): only the mid/close disagreement is
measurable there; spread metrics are honestly absent, never imputed.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from data_platform.collect.divergence import (
    DivergenceInputError,
    DivergenceThresholds,
    _percentiles,
    _severity,
)
from data_platform.materialize.candle_bars import CandleBar
from data_platform.quality.state import QualityState


@dataclass(frozen=True)
class BarComparisonResult:
    """The joined-and-measured comparison, before report serialization."""

    instrument: str
    providers: tuple[str, str]
    matched: int
    unmatched_primary: int
    unmatched_secondary: int
    mid_diffs_pips: tuple[float, ...]
    spread_diffs_pips: tuple[float, ...]
    receive_skews_ms: tuple[float, ...]
    state: QualityState
    reasons: tuple[str, ...]


def _classify(
    mid_diffs: Sequence[float],
    spread_diffs: Sequence[float],
    limits: DivergenceThresholds,
) -> tuple[QualityState, list[str]]:
    if not mid_diffs:
        return QualityState.UNAVAILABLE, ["no_aligned_bars"]
    state = QualityState.USABLE
    reasons: list[str] = []
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
    return state, reasons


def compare_candle_bars(
    primary: Sequence[CandleBar],
    secondary: Sequence[CandleBar],
    *,
    pip_size: float,
    thresholds: DivergenceThresholds | None = None,
    primary_received_at: Mapping[datetime, datetime] | None = None,
    secondary_received_at: Mapping[datetime, datetime] | None = None,
) -> dict[str, Any]:
    """Compare two independent providers' bars for ONE instrument."""

    limits = thresholds or DivergenceThresholds()
    if not primary or not secondary:
        raise DivergenceInputError("both sources must contribute at least one bar")
    instruments = {bar.instrument for bar in (*primary, *secondary)}
    if len(instruments) != 1:
        raise DivergenceInputError(f"mixed instruments in comparison: {sorted(instruments)}")
    providers = {bar.provider for bar in primary} | {bar.provider for bar in secondary}
    if len(providers) < 2:
        raise DivergenceInputError(
            "primary and secondary must be different providers; a second endpoint of "
            "the same provider is not an independent source"
        )
    intervals = {bar.interval for bar in (*primary, *secondary)}
    if len(intervals) != 1:
        raise DivergenceInputError(f"mixed bar intervals in comparison: {sorted(intervals)}")
    if pip_size <= 0:
        raise DivergenceInputError("pip_size must be positive")

    secondary_by_time = {bar.open_time: bar for bar in secondary}
    mid_diffs: list[float] = []
    spread_diffs: list[float] = []
    receive_skews: list[float] = []
    matched = 0
    for bar in sorted(primary, key=lambda b: b.open_time):
        other = secondary_by_time.get(bar.open_time)
        if other is None:
            continue
        matched += 1
        mid_diffs.append(abs(bar.mid_close - other.mid_close) / pip_size)
        spread_diffs.append(abs(bar.spread_close - other.spread_close) / pip_size)
        if primary_received_at is not None and secondary_received_at is not None:
            mine = primary_received_at.get(bar.open_time)
            theirs = secondary_received_at.get(bar.open_time)
            if mine is not None and theirs is not None:
                receive_skews.append(abs((theirs - mine).total_seconds()) * 1000.0)

    state, reasons = _classify(mid_diffs, spread_diffs, limits)
    primary_name = primary[0].provider
    secondary_name = next(iter(providers - {primary_name}))
    return {
        "instrument": next(iter(instruments)),
        "providers": sorted(providers),
        "interval": next(iter(intervals)),
        "comparison_basis": "boundary_close_mid_and_spread",
        "breach_counts": {
            "mid_over_threshold": sum(1 for diff in mid_diffs if diff > limits.max_mid_diff_pips),
            "mid_over_quarantine": sum(
                1 for diff in mid_diffs if diff > limits.quarantine_mid_diff_pips
            ),
            "spread_over_threshold": sum(
                1 for diff in spread_diffs if diff > limits.max_spread_diff_pips
            ),
        },
        "collection_context": (
            "bar sources are downloads/snapshots; receive_time_skew_ms measures the "
            "pipeline's receive alignment, not market latency"
        ),
        "matched_bars": matched,
        "unmatched_primary": len(primary) - matched,
        "unmatched_secondary": len(secondary) - matched,
        "metrics": {
            "mid_diff_pips": _percentiles(mid_diffs),
            "spread_diff_pips": _percentiles(spread_diffs),
            "receive_time_skew_ms": _percentiles(receive_skews),
        },
        "divergence_state": str(state),
        "divergence_reason": list(reasons),
        "thresholds": {
            "max_mid_diff_pips": limits.max_mid_diff_pips,
            "max_spread_diff_pips": limits.max_spread_diff_pips,
            "max_receive_skew_ms": limits.max_receive_skew_ms,
            "quarantine_mid_diff_pips": limits.quarantine_mid_diff_pips,
        },
        "policy": "values are never averaged on divergence; state degrades instead",
        "primary_provider": primary_name,
        "secondary_provider": secondary_name,
    }


def compare_bars_to_close_series(
    bars: Sequence[CandleBar],
    closes: Mapping[datetime, float],
    *,
    secondary_provider: str,
    pip_size: float,
    thresholds: DivergenceThresholds | None = None,
    close_basis_note: str,
) -> dict[str, Any]:
    """Compare bars against a close-only series (spread honestly unmeasurable).

    ``close_basis_note`` must state what the external closes are (e.g.
    "HistData M1-derived 1h closes, bid basis per provider docs") so the
    comparison's price-basis mismatch is part of the record, not a surprise.
    """

    limits = thresholds or DivergenceThresholds()
    if not bars or not closes:
        raise DivergenceInputError("both sources must contribute at least one row")
    if not close_basis_note.strip():
        raise DivergenceInputError("close_basis_note must describe the close-only series")
    providers = {bar.provider for bar in bars}
    if len(providers) != 1:
        raise DivergenceInputError(f"bars must come from one provider, got {sorted(providers)}")
    if secondary_provider in providers:
        raise DivergenceInputError("secondary_provider must differ from the bar provider")
    if pip_size <= 0:
        raise DivergenceInputError("pip_size must be positive")

    mid_diffs: list[float] = []
    matched = 0
    for bar in sorted(bars, key=lambda b: b.open_time):
        close = closes.get(bar.open_time)
        if close is None:
            continue
        matched += 1
        mid_diffs.append(abs(bar.mid_close - float(close)) / pip_size)

    state, reasons = _classify(mid_diffs, [], limits)
    return {
        "instrument": bars[0].instrument,
        "providers": sorted({*providers, secondary_provider}),
        "interval": bars[0].interval,
        "comparison_basis": f"mid_close_vs_close_only ({close_basis_note})",
        "matched_bars": matched,
        "unmatched_primary": len(bars) - matched,
        "unmatched_secondary": len(closes) - matched,
        "metrics": {
            "mid_diff_pips": _percentiles(mid_diffs),
            # spread cannot be measured against a close-only series; absence is
            # honest ("could not measure"), never imputed.
            "spread_diff_pips": {},
            "receive_time_skew_ms": {},
        },
        "divergence_state": str(state),
        "divergence_reason": list(reasons),
        "thresholds": {
            "max_mid_diff_pips": limits.max_mid_diff_pips,
            "max_spread_diff_pips": limits.max_spread_diff_pips,
            "max_receive_skew_ms": limits.max_receive_skew_ms,
            "quarantine_mid_diff_pips": limits.quarantine_mid_diff_pips,
        },
        "policy": "values are never averaged on divergence; state degrades instead",
        "primary_provider": bars[0].provider,
        "secondary_provider": secondary_provider,
    }
