from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from fx_backtester.indicators import average_true_range, sma
from fx_backtester.strategies.base import Strategy


@dataclass
class MovingAverageCross(Strategy):
    fast_window: int = 20
    slow_window: int = 50
    atr_window: int = 14
    stop_atr_multiple: float = 2.0

    @property
    def name(self) -> str:
        return "ma_cross"

    def generate(self, symbol: str, data: pd.DataFrame) -> pd.DataFrame:
        if self.fast_window >= self.slow_window:
            raise ValueError("fast_window must be smaller than slow_window")

        fast = sma(data["close"], self.fast_window)
        slow = sma(data["close"], self.slow_window)
        target = pd.Series(0, index=data.index)
        target = target.mask(fast > slow, 1)
        target = target.mask(fast < slow, -1)
        target = target.where(slow.notna(), 0)

        stop_distance = average_true_range(data, self.atr_window) * self.stop_atr_multiple
        return self._validated_output(
            data,
            pd.DataFrame({"target_position": target, "stop_distance": stop_distance}),
        )
