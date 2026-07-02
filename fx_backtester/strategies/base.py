from __future__ import annotations

from abc import ABC, abstractmethod

import pandas as pd


class Strategy(ABC):
    """Strategy layer: convert market data into desired direction and stop distance."""

    @property
    @abstractmethod
    def name(self) -> str:
        raise NotImplementedError

    @abstractmethod
    def generate(self, symbol: str, data: pd.DataFrame) -> pd.DataFrame:
        """Return target_position (-1/0/1) and stop_distance indexed like data."""
        raise NotImplementedError

    def _validated_output(self, data: pd.DataFrame, output: pd.DataFrame) -> pd.DataFrame:
        required = {"target_position", "stop_distance"}
        missing = required - set(output.columns)
        if missing:
            raise ValueError(f"{self.name} signal output missing columns: {sorted(missing)}")
        aligned = output.reindex(data.index).copy()
        aligned["target_position"] = aligned["target_position"].fillna(0).astype(int).clip(-1, 1)
        aligned["stop_distance"] = aligned["stop_distance"].astype(float)
        fallback_stop = (data["close"].abs() * 0.002).replace(0, pd.NA)
        aligned["stop_distance"] = aligned["stop_distance"].where(
            aligned["stop_distance"] > 0,
            fallback_stop,
        )
        return aligned
