from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pandas as pd


@dataclass(frozen=True)
class DataQualityConfig:
    expected_frequency: str | None = None
    max_missing_pct: float = 0.0005


def validate_price_data(
    data: dict[str, pd.DataFrame],
    config: DataQualityConfig | None = None,
) -> pd.DataFrame:
    settings = config or DataQualityConfig()
    rows: list[dict[str, Any]] = []
    for symbol, frame in sorted(data.items()):
        prepared = frame.sort_index()
        duplicate_count = int(prepared.index.duplicated().sum())
        monotonic = bool(prepared.index.is_monotonic_increasing)
        null_ohlc_count = int(prepared[["open", "high", "low", "close"]].isna().sum().sum())
        invalid_ohlc_count = int(
            (
                (prepared["high"] < prepared["low"])
                | (prepared["open"] > prepared["high"])
                | (prepared["open"] < prepared["low"])
                | (prepared["close"] > prepared["high"])
                | (prepared["close"] < prepared["low"])
            ).sum()
        )

        expected_bars = len(prepared)
        missing_bars = 0
        missing_pct = 0.0
        inferred_frequency = pd.infer_freq(prepared.index) if len(prepared.index) >= 3 else None
        frequency = settings.expected_frequency or inferred_frequency
        if frequency and not prepared.empty:
            expected_index = pd.date_range(
                prepared.index.min(), prepared.index.max(), freq=frequency
            )
            expected_bars = len(expected_index)
            missing_bars = int(len(expected_index.difference(prepared.index)))
            missing_pct = missing_bars / expected_bars if expected_bars else 0.0

        passed = (
            monotonic
            and duplicate_count == 0
            and null_ohlc_count == 0
            and invalid_ohlc_count == 0
            and missing_pct <= settings.max_missing_pct
        )
        rows.append(
            {
                "symbol": symbol,
                "rows": int(len(prepared)),
                "start": prepared.index.min() if not prepared.empty else pd.NaT,
                "end": prepared.index.max() if not prepared.empty else pd.NaT,
                "frequency": frequency or "",
                "expected_bars": int(expected_bars),
                "missing_bars": missing_bars,
                "missing_pct": float(missing_pct),
                "duplicate_count": duplicate_count,
                "monotonic": monotonic,
                "null_ohlc_count": null_ohlc_count,
                "invalid_ohlc_count": invalid_ohlc_count,
                "passed": bool(passed),
            }
        )

    return pd.DataFrame(rows)
