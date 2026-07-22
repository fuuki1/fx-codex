from __future__ import annotations

import numpy as np
import pandas as pd

from fx_intel import historical_chart


def _daily_bid_ask() -> pd.DataFrame:
    timestamps = pd.date_range("2019-11-01", "2026-01-10", freq="1D", tz="UTC")
    index = np.arange(len(timestamps), dtype=float)
    mid = 1.15 + 0.02 * np.sin(index / 7.0) + 0.01 * np.sin(index / 31.0)
    spread = 0.0002
    return pd.DataFrame(
        {
            "timestamp": timestamps,
            "bid_open": mid - spread / 2,
            "bid_high": mid + 0.003 - spread / 2,
            "bid_low": mid - 0.003 - spread / 2,
            "bid_close": mid + 0.001 * np.sin(index / 3.0) - spread / 2,
            "ask_open": mid + spread / 2,
            "ask_high": mid + 0.003 + spread / 2,
            "ask_low": mid - 0.003 + spread / 2,
            "ask_close": mid + 0.001 * np.sin(index / 3.0) + spread / 2,
        }
    )


def test_features_do_not_change_when_only_future_quote_changes() -> None:
    bars = historical_chart.resample_bid_ask(_daily_bid_ask(), "1d")
    original = historical_chart.build_labeled_frame(bars)
    changed_bars = bars.copy()
    position = 100
    stamp = bars.index[position]
    changed_bars.loc[bars.index[position + 1], "bid_close"] += 0.05
    changed_bars.loc[bars.index[position + 1], "ask_close"] += 0.05
    changed = historical_chart.build_labeled_frame(changed_bars)

    assert original.loc[stamp, list(historical_chart.FEATURE_NAMES)].equals(
        changed.loc[stamp, list(historical_chart.FEATURE_NAMES)]
    )
    assert original.loc[stamp, "long_quote_r"] != changed.loc[stamp, "long_quote_r"]


def test_fixed_window_training_stays_shadow_and_reports_quote_r() -> None:
    cell = historical_chart.train_cell("EURUSD", "1d", _daily_bid_ask())

    assert cell["stage"] == "shadow"
    assert cell["promotion_admissible"] is False
    assert cell["samples"]["train"] > cell["samples"]["valid"]
    assert cell["samples"]["test"] >= 300
    assert cell["metrics"]["canonical_pure_r"] is False
    assert cell["metrics"]["cost_status"] == "spread_measured_commission_slippage_missing"


def test_partition_keeps_label_end_inside_fixed_window() -> None:
    index = pd.to_datetime(["2023-12-31T23:45:00Z", "2023-12-31T23:55:00Z"], utc=True)
    frame = pd.DataFrame(
        {
            "label_end_time": pd.to_datetime(
                ["2023-12-31T23:55:00Z", "2024-01-01T00:05:00Z"], utc=True
            )
        },
        index=index,
    )

    partition = historical_chart._partition(
        frame, historical_chart.TRAIN_START, historical_chart.TRAIN_END
    )

    assert list(partition.index) == [index[0]]
