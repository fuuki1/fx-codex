"""MA クロス + ATR ストップ戦略。

target_position: fast SMA > slow SMA なら +1、< なら -1、確定前(NaN)は 0。
stop_distance:   ATR * stop_atr_multiple（価格距離）。
すべて「そのバーまで」の情報で計算（先読みなし）。実際の約定遅延は engine 側で
ポジションを 1 バー遅らせて適用することで担保する。
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from ..indicators import atr, sma
from .base import Strategy


class MaCrossStrategy(Strategy):
    def __init__(
        self,
        fast_window: int = 20,
        slow_window: int = 60,
        atr_window: int = 14,
        stop_atr_multiple: float = 2.0,
    ) -> None:
        if fast_window <= 0 or slow_window <= 0 or atr_window <= 0:
            raise ValueError("windows must be positive")
        if fast_window >= slow_window:
            raise ValueError(f"fast_window({fast_window}) must be < slow_window({slow_window})")
        self.fast_window = fast_window
        self.slow_window = slow_window
        self.atr_window = atr_window
        self.stop_atr_multiple = float(stop_atr_multiple)

    @property
    def name(self) -> str:
        return "ma_cross"

    def generate(self, symbol: str, data: pd.DataFrame) -> pd.DataFrame:
        close = data["close"]
        fast = sma(close, self.fast_window)
        slow = sma(close, self.slow_window)
        # NaN（確定前）は 0 のまま（_validated_output が fillna(0)）
        target = np.sign(fast - slow)
        stop_distance = atr(data, self.atr_window) * self.stop_atr_multiple
        out = pd.DataFrame(
            {"target_position": target, "stop_distance": stop_distance}, index=data.index
        )
        return self._validated_output(data, out)
