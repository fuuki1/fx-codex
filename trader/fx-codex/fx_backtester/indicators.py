"""指標計算（純粋関数・先読みなし）。

すべて「そのバーの終値までの情報」だけで計算する（未来を参照しない）。
"""
from __future__ import annotations

import pandas as pd


def sma(series: pd.Series, window: int) -> pd.Series:
    return series.rolling(window, min_periods=window).mean()


def atr(data: pd.DataFrame, window: int) -> pd.Series:
    """Average True Range。high/low/close があれば true range、無ければ close 差分。

    True Range(t) = max(high-low, |high-prev_close|, |low-prev_close|)
    prev_close は shift(1) なので未来を見ない。
    """
    if {"high", "low", "close"}.issubset(data.columns):
        prev_close = data["close"].shift(1)
        tr = pd.concat(
            [
                data["high"] - data["low"],
                (data["high"] - prev_close).abs(),
                (data["low"] - prev_close).abs(),
            ],
            axis=1,
        ).max(axis=1)
    else:
        tr = data["close"].diff().abs()
    return tr.rolling(window, min_periods=window).mean()
