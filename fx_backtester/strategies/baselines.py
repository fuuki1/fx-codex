from __future__ import annotations

from dataclasses import dataclass
from zlib import crc32

import numpy as np
import pandas as pd

from fx_backtester.indicators import average_true_range
from fx_backtester.strategies.base import Strategy


@dataclass
class FlatStrategy(Strategy):
    stop_atr_multiple: float = 2.0
    atr_window: int = 14

    @property
    def name(self) -> str:
        return "flat_baseline"

    def generate(self, symbol: str, data: pd.DataFrame) -> pd.DataFrame:
        stop_distance = average_true_range(data, self.atr_window) * self.stop_atr_multiple
        return self._validated_output(
            data,
            pd.DataFrame({"target_position": 0, "stop_distance": stop_distance}, index=data.index),
        )


@dataclass
class BuyAndHoldLongStrategy(Strategy):
    stop_atr_multiple: float = 2.0
    atr_window: int = 14

    @property
    def name(self) -> str:
        return "buy_and_hold_long_baseline"

    def generate(self, symbol: str, data: pd.DataFrame) -> pd.DataFrame:
        stop_distance = average_true_range(data, self.atr_window) * self.stop_atr_multiple
        return self._validated_output(
            data,
            pd.DataFrame({"target_position": 1, "stop_distance": stop_distance}, index=data.index),
        )


@dataclass
class RandomDirectionStrategy(Strategy):
    seed: int = 7
    flat_probability: float = 0.34
    stop_atr_multiple: float = 2.0
    atr_window: int = 14

    @property
    def name(self) -> str:
        return "random_direction_baseline"

    def generate(self, symbol: str, data: pd.DataFrame) -> pd.DataFrame:
        if not 0 <= self.flat_probability < 1:
            raise ValueError("flat_probability must be >= 0 and < 1")
        symbol_seed = self.seed + crc32(symbol.encode("utf-8"))
        rng = np.random.default_rng(symbol_seed)
        side_probability = (1 - self.flat_probability) / 2
        target = rng.choice(
            [-1, 0, 1],
            size=len(data),
            p=[side_probability, self.flat_probability, side_probability],
        )
        stop_distance = average_true_range(data, self.atr_window) * self.stop_atr_multiple
        return self._validated_output(
            data,
            pd.DataFrame(
                {
                    "target_position": target,
                    "stop_distance": stop_distance,
                },
                index=data.index,
            ),
        )
