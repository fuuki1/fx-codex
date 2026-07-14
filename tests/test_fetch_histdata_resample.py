"""HistData resample labeling: bars must be labelled by their OPEN time (UTC).

Regression for INC-20260714-M1: the v1 export used ``label="right"`` which
shifted every 1h label +1h (measured as p50 6.5 pips vs Dukascopy at lag 0).
"""

from __future__ import annotations

import pandas as pd

from scripts.fetch_histdata import resample


def _m1_frame() -> pd.DataFrame:
    # 2024-01-10 (winter: EST = UTC-5), minutes 10:00 .. 11:59 US/Eastern,
    # stamped by bar OPEN time exactly like HistData ASCII M1 rows.
    index = pd.date_range("2024-01-10 10:00", periods=120, freq="1min", tz="US/Eastern")
    prices = [100.0 + i * 0.01 for i in range(120)]
    return pd.DataFrame(
        {
            "open": prices,
            "high": [p + 0.005 for p in prices],
            "low": [p - 0.005 for p in prices],
            "close": [p + 0.002 for p in prices],
        },
        index=index,
    )


class TestResampleLabeling:
    def test_hourly_bars_are_labelled_by_open_time_utc(self) -> None:
        out = resample(_m1_frame(), "1h")
        assert list(out.index) == [
            pd.Timestamp("2024-01-10 15:00", tz="UTC"),  # 10:00 EST
            pd.Timestamp("2024-01-10 16:00", tz="UTC"),  # 11:00 EST
        ]

    def test_hourly_aggregation_uses_left_closed_buckets(self) -> None:
        out = resample(_m1_frame(), "1h")
        first = out.iloc[0]
        # bucket [10:00, 11:00): open of minute 10:00, close of minute 10:59
        assert first["open"] == 100.0
        assert first["close"] == 100.0 + 59 * 0.01 + 0.002
        assert first["high"] == 100.0 + 59 * 0.01 + 0.005
        assert first["low"] == 100.0 - 0.005

    def test_m1_passthrough_converts_to_utc(self) -> None:
        out = resample(_m1_frame(), "M1")
        assert str(out.index.tz) == "UTC"
        assert out.index[0] == pd.Timestamp("2024-01-10 15:00", tz="UTC")
