from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class DataQualityConfig:
    expected_frequency: str | None = None
    max_missing_pct: float = 0.0005
    max_future_skew: timedelta = timedelta(seconds=5)


def validate_price_data(
    data: dict[str, pd.DataFrame],
    config: DataQualityConfig | None = None,
    *,
    as_of: datetime | None = None,
) -> pd.DataFrame:
    settings = config or DataQualityConfig()
    evaluated_at = as_of or datetime.now(UTC)
    if evaluated_at.tzinfo is None or evaluated_at.utcoffset() is None:
        raise ValueError("price QA as_of must be timezone-aware")
    evaluated_at = evaluated_at.astimezone(UTC)
    if settings.max_future_skew < timedelta(0):
        raise ValueError("price QA max_future_skew must be non-negative")
    rows: list[dict[str, Any]] = []
    for symbol, frame in sorted(data.items()):
        prepared = frame.sort_index()
        duplicate_count = int(prepared.index.duplicated().sum())
        monotonic = bool(prepared.index.is_monotonic_increasing)
        null_ohlc_count = int(prepared[["open", "high", "low", "close"]].isna().sum().sum())
        try:
            numeric_ohlc = prepared[["open", "high", "low", "close"]].apply(
                pd.to_numeric,
                errors="raise",
            )
            nonfinite_ohlc_count = int((~np.isfinite(numeric_ohlc.to_numpy(dtype=float))).sum())
            nonpositive_ohlc_count = int((numeric_ohlc <= 0).sum().sum())
        except (TypeError, ValueError):
            numeric_ohlc = None
            nonfinite_ohlc_count = int(len(prepared) * 4)
            nonpositive_ohlc_count = 0
        invalid_ohlc_count = (
            int(
                (
                    (numeric_ohlc["high"] < numeric_ohlc["low"])
                    | (numeric_ohlc["open"] > numeric_ohlc["high"])
                    | (numeric_ohlc["open"] < numeric_ohlc["low"])
                    | (numeric_ohlc["close"] > numeric_ohlc["high"])
                    | (numeric_ohlc["close"] < numeric_ohlc["low"])
                ).sum()
            )
            if numeric_ohlc is not None
            else len(prepared)
        )

        aware_utc_index = (
            isinstance(prepared.index, pd.DatetimeIndex) and prepared.index.tz is not None
        )
        if aware_utc_index:
            aware_utc_index = all(
                timestamp.utcoffset() == timedelta(0) for timestamp in prepared.index
            )
        future_timestamp_count = (
            int((prepared.index > pd.Timestamp(evaluated_at + settings.max_future_skew)).sum())
            if aware_utc_index
            else len(prepared)
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
            and nonfinite_ohlc_count == 0
            and nonpositive_ohlc_count == 0
            and aware_utc_index
            and future_timestamp_count == 0
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
                "nonfinite_ohlc_count": nonfinite_ohlc_count,
                "nonpositive_ohlc_count": nonpositive_ohlc_count,
                "aware_utc_index": aware_utc_index,
                "future_timestamp_count": future_timestamp_count,
                "passed": bool(passed),
            }
        )

    return pd.DataFrame(rows)
