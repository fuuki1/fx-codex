from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from fx_backtester.indicators import average_true_range, sma
from fx_backtester.strategies.base import Strategy


@dataclass(frozen=True)
class RegimeFilterConfig:
    enabled: bool = True
    window: int = 200
    slope_window: int = 24
    min_atr_percentile: float = 0.10
    max_atr_percentile: float = 0.95


@dataclass(frozen=True)
class NoTradeFilterConfig:
    enabled: bool = True
    blocked_entry_hours: tuple[int, ...] = (21, 22)
    max_spread_multiple: float = 2.5
    spread_lookback: int = 48


class FilteredStrategy(Strategy):
    """Apply regime and entry-only no-trade filters to a base strategy."""

    def __init__(
        self,
        base: Strategy,
        regime: RegimeFilterConfig | None = None,
        no_trade: NoTradeFilterConfig | None = None,
    ) -> None:
        self.base = base
        self.regime = regime or RegimeFilterConfig()
        self.no_trade = no_trade or NoTradeFilterConfig()

    @property
    def name(self) -> str:
        return f"{self.base.name}_filtered"

    def generate(self, symbol: str, data: pd.DataFrame) -> pd.DataFrame:
        raw = self.base.generate(symbol, data).copy()
        target = raw["target_position"].astype(int)
        regime_allowed = self._regime_allowed(data, target)
        entry_allowed = self._entry_allowed(data)

        output = raw.copy()
        output["raw_target_position"] = target
        output["target_position"] = target.where(regime_allowed, 0)
        output["entry_allowed"] = entry_allowed
        output["regime_allowed"] = regime_allowed
        output["filter_reason"] = ""
        output.loc[~regime_allowed, "filter_reason"] = "regime_block"
        output.loc[~entry_allowed, "filter_reason"] = output.loc[
            ~entry_allowed,
            "filter_reason",
        ].where(output.loc[~entry_allowed, "filter_reason"] == "", "regime_block,no_trade")
        output.loc[
            (~entry_allowed) & (output["filter_reason"] == ""),
            "filter_reason",
        ] = "no_trade"
        return self._validated_output(data, output)

    def _regime_allowed(self, data: pd.DataFrame, target: pd.Series) -> pd.Series:
        if not self.regime.enabled:
            return pd.Series(True, index=data.index)
        if self.regime.window <= 1:
            raise ValueError("regime window must be > 1")
        if self.regime.slope_window <= 0:
            raise ValueError("regime slope_window must be > 0")

        close = data["close"].astype(float)
        trend = sma(close, self.regime.window)
        slope = trend - trend.shift(self.regime.slope_window)
        atr = average_true_range(data, min(14, max(2, self.regime.window // 10)))
        atr_percentile = _rolling_percentile(atr, self.regime.window)

        long_allowed = (target != 1) | ((close >= trend) & (slope >= 0))
        short_allowed = (target != -1) | ((close <= trend) & (slope <= 0))
        volatility_allowed = atr_percentile.between(
            self.regime.min_atr_percentile,
            self.regime.max_atr_percentile,
            inclusive="both",
        )
        neutral = target == 0
        allowed = neutral | (long_allowed & short_allowed & volatility_allowed)
        return allowed.fillna(False)

    def _entry_allowed(self, data: pd.DataFrame) -> pd.Series:
        if not self.no_trade.enabled:
            return pd.Series(True, index=data.index)
        allowed = pd.Series(True, index=data.index)
        if self.no_trade.blocked_entry_hours:
            hours = pd.Series(data.index.hour, index=data.index)
            allowed &= ~hours.isin(set(self.no_trade.blocked_entry_hours))

        if "spread_pips" in data.columns:
            spread_column = "spread_pips"
        elif "spread_price" in data.columns:
            spread_column = "spread_price"
        else:
            spread_column = "spread"
        if spread_column in data.columns:
            spread = data[spread_column].astype(float)
            median = spread.rolling(
                self.no_trade.spread_lookback,
                min_periods=max(3, min(self.no_trade.spread_lookback, 12)),
            ).median()
            allowed &= median.isna() | (spread <= median * self.no_trade.max_spread_multiple)
        return allowed.fillna(False)


def _rolling_percentile(series: pd.Series, window: int) -> pd.Series:
    return series.rolling(window, min_periods=window).apply(
        lambda values: pd.Series(values).rank(pct=True).iloc[-1],
        raw=False,
    )
