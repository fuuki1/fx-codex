from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from fx_backtester.indicators import average_true_range
from fx_backtester.strategies.base import Strategy


@dataclass
class DonchianBreakout(Strategy):
    entry_window: int = 20
    exit_window: int = 10
    atr_window: int = 14
    stop_atr_multiple: float = 2.0

    @property
    def name(self) -> str:
        return "donchian"

    def generate(self, symbol: str, data: pd.DataFrame) -> pd.DataFrame:
        if self.exit_window >= self.entry_window:
            raise ValueError("exit_window should be smaller than entry_window")

        upper_entry = data["high"].shift(1).rolling(self.entry_window, min_periods=self.entry_window).max()
        lower_entry = data["low"].shift(1).rolling(self.entry_window, min_periods=self.entry_window).min()
        upper_exit = data["high"].shift(1).rolling(self.exit_window, min_periods=self.exit_window).max()
        lower_exit = data["low"].shift(1).rolling(self.exit_window, min_periods=self.exit_window).min()

        target_values: list[int] = []
        current = 0
        for timestamp in data.index:
            close = data.at[timestamp, "close"]
            if pd.isna(upper_entry.at[timestamp]) or pd.isna(lower_entry.at[timestamp]):
                current = 0
            elif current == 1:
                if close < lower_entry.at[timestamp]:
                    current = -1
                elif close < lower_exit.at[timestamp]:
                    current = 0
            elif current == -1:
                if close > upper_entry.at[timestamp]:
                    current = 1
                elif close > upper_exit.at[timestamp]:
                    current = 0
            else:
                if close > upper_entry.at[timestamp]:
                    current = 1
                elif close < lower_entry.at[timestamp]:
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
