from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from fx_backtester.indicators import average_true_range, rsi
from fx_backtester.strategies.base import Strategy


@dataclass
class RSIMeanReversion(Strategy):
    rsi_window: int = 14
    low_threshold: float = 30.0
    high_threshold: float = 70.0
    exit_level: float = 50.0
    atr_window: int = 14
    stop_atr_multiple: float = 1.5

    @property
    def name(self) -> str:
        return "rsi_mean_reversion"

    def generate(self, symbol: str, data: pd.DataFrame) -> pd.DataFrame:
        if not self.low_threshold < self.exit_level < self.high_threshold:
            raise ValueError("low_threshold < exit_level < high_threshold is required")

        values = rsi(data["close"], self.rsi_window)
        target_values: list[int] = []
        current = 0
        for timestamp, value in values.items():
            if pd.isna(value):
                current = 0
            elif current == 1:
                if value >= self.exit_level:
                    current = 0
            elif current == -1:
                if value <= self.exit_level:
                    current = 0
            else:
                if value <= self.low_threshold:
                    current = 1
                elif value >= self.high_threshold:
                    current = -1
            target_values.append(current)

        stop_distance = average_true_range(data, self.atr_window) * self.stop_atr_multiple
        return self._validated_output(
            data,
            pd.DataFrame(
                {
                    "target_position": pd.Series(target_values, index=data.index),
                    "stop_distance": stop_distance,
                }
            ),
        )
