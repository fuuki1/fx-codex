"""As-of FX liquidity proxy tests."""

from __future__ import annotations

from datetime import datetime, timedelta, UTC

import pytest

from fx_intel import liquidity

NOW = datetime(2026, 6, 29, 10, 0, tzinfo=UTC)  # London session (Tokyo closed)


def _row(minutes_back: int, spread: float, timeframe: str) -> dict[str, object]:
    stamp = NOW - timedelta(minutes=minutes_back)
    start = stamp - timedelta(minutes=5)
    return {
        "symbol": "USDJPY",
        "timeframe": timeframe,
        "source": "oanda_v20",
        "bar_granularity": "M5",
        "bar_start": start.isoformat(),
        "available_time": stamp.isoformat(),
        "spread": spread,
    }


def test_baseline_deduplicates_timeframe_copies_and_marks_stress() -> None:
    rows = [
        _row(20, 0.01, "15m"),
        _row(20, 0.01, "1h"),
        _row(10, 0.02, "15m"),
        _row(10, 0.02, "4h"),
    ]
    quote = liquidity.scanner_quote("USDJPY", bid=156.00, ask=156.03, observed_at=NOW)
    snapshot = liquidity.build_liquidity_snapshot(
        "USDJPY",
        decision_time=NOW,
        quote=quote,
        price_rows=rows,
        session_bucket="london",
        policy=liquidity.LiquidityPolicy(
            min_baseline_samples=2, thin_percentile=0.5, stressed_percentile=0.99
        ),
    )

    assert snapshot.features["baseline_n"] == 2.0
    assert snapshot.features["spread_percentile"] == 1.0
    assert snapshot.status == "stressed"
    assert snapshot.baseline_scope == "session"


def test_insufficient_baseline_is_unknown_not_normal() -> None:
    quote = liquidity.scanner_quote("EURUSD", bid=1.10, ask=1.1001, observed_at=NOW)
    snapshot = liquidity.build_liquidity_snapshot(
        "EURUSD",
        decision_time=NOW,
        quote=quote,
        price_rows=[],
        session_bucket="london",
        policy=liquidity.LiquidityPolicy(min_baseline_samples=2),
    )

    assert snapshot.status == "unknown"
    assert "baseline_insufficient" in snapshot.reason_codes
    assert snapshot.features["spread_pips"] == pytest.approx(1.0)


def test_invalid_and_future_quotes_fail_closed() -> None:
    invalid = liquidity.scanner_quote("USDJPY", bid=156.02, ask=156.01, observed_at=NOW)
    invalid_snapshot = liquidity.build_liquidity_snapshot(
        "USDJPY",
        decision_time=NOW,
        quote=invalid,
        price_rows=[],
        session_bucket="london",
    )
    assert invalid_snapshot.status == "invalid"
    assert "ask_below_bid" in invalid_snapshot.reason_codes

    future = liquidity.scanner_quote(
        "USDJPY", bid=156.00, ask=156.01, observed_at=NOW + timedelta(seconds=1)
    )
    future_snapshot = liquidity.build_liquidity_snapshot(
        "USDJPY",
        decision_time=NOW,
        quote=future,
        price_rows=[_row(10, 0.01, "15m")],
        session_bucket="london",
        policy=liquidity.LiquidityPolicy(min_baseline_samples=1),
    )
    assert future_snapshot.status == "invalid"
    assert "future_quote" in future_snapshot.reason_codes


def test_rollover_window_uses_new_york_local_time() -> None:
    # June is EDT, so 17:00 New York is 21:00 UTC.
    assert liquidity.is_rollover_window(datetime(2026, 6, 29, 21, 5, tzinfo=UTC))
    assert not liquidity.is_rollover_window(datetime(2026, 6, 29, 20, 30, tzinfo=UTC))
