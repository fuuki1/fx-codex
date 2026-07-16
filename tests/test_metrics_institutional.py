from __future__ import annotations

import pandas as pd
import pytest

from fx_backtester.metrics import calculate_metrics


def test_institutional_tail_holding_cost_and_streak_metrics() -> None:
    index = pd.date_range("2025-01-01", periods=6, freq="D", tz="UTC")
    equity = pd.DataFrame(
        {"equity": [100.0, 102.0, 99.0, 98.0, 101.0, 100.0], "open_positions": [0, 1, 1, 1, 1, 0]},
        index=index,
    )
    trades = pd.DataFrame(
        {
            "net_pnl": [2.0, -3.0, -1.0, 3.0, -1.0],
            "r_multiple": [1.0, -1.5, -0.5, 1.5, -0.5],
            "fees": [0.1, 0.2, 0.2, 0.1, 0.4],
            "units": [10.0, 20.0, 30.0, 40.0, 50.0],
            "entry_time": index[:5],
            "exit_time": index[1:6],
        }
    )

    metrics = calculate_metrics(equity, trades, initial_cash=100.0)

    assert metrics["median_r"] == -0.5
    assert metrics["expected_shortfall_r_05"] == -1.5
    assert metrics["longest_loss_streak"] == 2
    assert metrics["average_holding_hours"] == 24.0
    assert metrics["median_holding_hours"] == 24.0
    assert metrics["total_fees_usd"] == pytest.approx(1.0)
    assert metrics["round_trip_turnover_units"] == 300.0
    assert metrics["sortino_ratio"] != 0.0


def test_institutional_metrics_are_zero_for_empty_trade_log() -> None:
    index = pd.date_range("2025-01-01", periods=2, freq="D", tz="UTC")
    equity = pd.DataFrame({"equity": [100.0, 100.0]}, index=index)

    metrics = calculate_metrics(equity, pd.DataFrame(), initial_cash=100.0)

    for key in (
        "median_r",
        "expected_shortfall_r_05",
        "longest_loss_streak",
        "average_holding_hours",
        "median_holding_hours",
        "total_fees_usd",
        "round_trip_turnover_units",
    ):
        assert metrics[key] == 0
