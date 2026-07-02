from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pandas as pd

from fx_backtester.models import instrument_for, notional_usd, price_distance_to_usd_per_unit


@dataclass
class RiskConfig:
    risk_per_trade_pct: float = 0.01
    risk_cap_pct: float = 0.01
    max_daily_loss_pct: float = 0.02
    max_weekly_loss_pct: float | None = None
    max_monthly_drawdown_pct: float | None = None
    monthly_profit_target_pct: float | None = None
    hard_drawdown_pct: float | None = None
    min_stop_pips: float = 5.0
    max_leverage: float = 10.0
    max_currency_exposure_pct: float | None = None
    max_position_units: float | None = None
    allow_fractional_units: bool = False


class RiskManager:
    """Risk layer: position sizing and daily stop state."""

    def __init__(self, config: RiskConfig | None = None) -> None:
        self.config = config or RiskConfig()
        self._current_day: Any = None
        self._current_week: Any = None
        self._current_month: Any = None
        self._day_start_equity: float | None = None
        self._week_start_equity: float | None = None
        self._month_start_equity: float | None = None
        self._month_peak_equity: float | None = None
        self._high_watermark_equity: float | None = None
        self._daily_locked = False
        self._weekly_locked = False
        self._monthly_locked = False
        self._monthly_profit_locked = False
        self._hard_locked = False

    @property
    def daily_locked(self) -> bool:
        return self._daily_locked

    @property
    def weekly_locked(self) -> bool:
        return self._weekly_locked

    @property
    def monthly_locked(self) -> bool:
        return self._monthly_locked

    @property
    def monthly_profit_locked(self) -> bool:
        return self._monthly_profit_locked

    @property
    def hard_locked(self) -> bool:
        return self._hard_locked

    @property
    def risk_locked(self) -> bool:
        return (
            self._daily_locked
            or self._weekly_locked
            or self._monthly_locked
            or self._monthly_profit_locked
            or self._hard_locked
        )

    def reset(self) -> None:
        self._current_day = None
        self._current_week = None
        self._current_month = None
        self._day_start_equity = None
        self._week_start_equity = None
        self._month_start_equity = None
        self._month_peak_equity = None
        self._high_watermark_equity = None
        self._daily_locked = False
        self._weekly_locked = False
        self._monthly_locked = False
        self._monthly_profit_locked = False
        self._hard_locked = False

    def on_bar(self, timestamp: Any, equity: float) -> None:
        normalized = pd.Timestamp(timestamp)
        day = normalized.date()
        if day != self._current_day:
            self._current_day = day
            self._day_start_equity = equity
            self._daily_locked = False

        iso = normalized.isocalendar()
        week = (int(iso.year), int(iso.week))
        if week != self._current_week:
            self._current_week = week
            self._week_start_equity = equity
            self._weekly_locked = False

        month = (normalized.year, normalized.month)
        if month != self._current_month:
            self._current_month = month
            self._month_start_equity = equity
            self._month_peak_equity = equity
            self._monthly_locked = False
            self._monthly_profit_locked = False

        if self._month_peak_equity is None or equity > self._month_peak_equity:
            self._month_peak_equity = equity
        if self._high_watermark_equity is None or equity > self._high_watermark_equity:
            self._high_watermark_equity = equity

    def check_daily_loss(self, timestamp: Any, equity: float) -> bool:
        self.on_bar(timestamp, equity)
        if self._day_start_equity is None:
            return False
        threshold = self._day_start_equity * (1 - self.config.max_daily_loss_pct)
        if equity <= threshold:
            self._daily_locked = True
        return self._daily_locked

    def check_portfolio_stops(self, timestamp: Any, equity: float) -> list[str]:
        self.on_bar(timestamp, equity)
        reasons: list[str] = []

        if (
            self.config.max_weekly_loss_pct is not None
            and self._week_start_equity is not None
            and equity <= self._week_start_equity * (1 - self.config.max_weekly_loss_pct)
        ):
            self._weekly_locked = True
            reasons.append("weekly_loss_stop")

        if (
            self.config.max_monthly_drawdown_pct is not None
            and self._month_peak_equity is not None
            and equity <= self._month_peak_equity * (1 - self.config.max_monthly_drawdown_pct)
        ):
            self._monthly_locked = True
            reasons.append("monthly_drawdown_stop")

        if (
            self.config.monthly_profit_target_pct is not None
            and self._month_start_equity is not None
            and equity >= self._month_start_equity * (1 + self.config.monthly_profit_target_pct)
        ):
            self._monthly_profit_locked = True
            reasons.append("monthly_profit_target")

        if (
            self.config.hard_drawdown_pct is not None
            and self._high_watermark_equity is not None
            and equity <= self._high_watermark_equity * (1 - self.config.hard_drawdown_pct)
        ):
            self._hard_locked = True
            reasons.append("hard_drawdown_stop")

        return reasons

    def can_open(self, timestamp: Any, equity: float, no_trade_window: bool) -> bool:
        self.on_bar(timestamp, equity)
        if no_trade_window:
            return False
        if self.risk_locked:
            return False
        return equity > 0

    def position_size(
        self,
        symbol: str,
        equity: float,
        entry_price: float,
        stop_distance: float,
        extra_risk_per_unit_usd: float = 0.0,
        extra_risk_usd: float = 0.0,
        conversion_rates: dict[str, float] | None = None,
    ) -> tuple[float, float, float]:
        """Return units, adjusted stop distance, and initial risk in USD.

        The per-trade risk is capped by RiskConfig.risk_cap_pct.
        extra_risk_per_unit_usd is used for estimated round-trip spread/slippage/fees.
        extra_risk_usd is used for fixed or minimum fees that do not scale with size.
        """
        inst = instrument_for(symbol)
        min_stop_distance = inst.pip_size * self.config.min_stop_pips
        adjusted_stop_distance = max(float(stop_distance), min_stop_distance)
        price_risk_per_unit = price_distance_to_usd_per_unit(
            symbol,
            adjusted_stop_distance,
            entry_price,
            conversion_rates,
        )
        total_risk_per_unit = price_risk_per_unit + max(extra_risk_per_unit_usd, 0.0)
        if total_risk_per_unit <= 0:
            return 0.0, adjusted_stop_distance, 0.0

        fixed_risk = max(float(extra_risk_usd), 0.0)
        risk_budget = equity * min(self.config.risk_per_trade_pct, self.config.risk_cap_pct)
        if risk_budget <= fixed_risk:
            return 0.0, adjusted_stop_distance, 0.0
        units = (risk_budget - fixed_risk) / total_risk_per_unit

        notional_per_unit = notional_usd(symbol, 1.0, entry_price, conversion_rates)
        max_units_by_leverage = equity * self.config.max_leverage / notional_per_unit
        units = min(units, max_units_by_leverage)
        if self.config.max_position_units is not None:
            units = min(units, self.config.max_position_units)
        if not self.config.allow_fractional_units:
            units = float(int(units))
        if units <= 0:
            return 0.0, adjusted_stop_distance, 0.0

        initial_risk = units * total_risk_per_unit + fixed_risk
        return max(units, 0.0), adjusted_stop_distance, initial_risk
