from __future__ import annotations

from dataclasses import dataclass
from math import isfinite
from numbers import Real
from typing import Any

import pandas as pd

from fx_backtester.models import instrument_for, notional_usd, price_distance_to_usd_per_unit

MAX_INPUT_MAGNITUDE = 1e15
MAX_LEVERAGE = 1e6
MAX_STOP_PIPS = 1e6


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

    def __post_init__(self) -> None:
        for name in (
            "risk_per_trade_pct",
            "risk_cap_pct",
            "max_daily_loss_pct",
        ):
            _bounded_real(getattr(self, name), name, lower=0.0, upper=1.0, lower_open=True)
        for name in (
            "max_weekly_loss_pct",
            "max_monthly_drawdown_pct",
            "monthly_profit_target_pct",
            "hard_drawdown_pct",
        ):
            value = getattr(self, name)
            if value is not None:
                _bounded_real(value, name, lower=0.0, upper=1.0, lower_open=True)
        if self.max_currency_exposure_pct is not None:
            _bounded_real(
                self.max_currency_exposure_pct,
                "max_currency_exposure_pct",
                lower=0.0,
                upper=100.0,
                lower_open=True,
            )
        _bounded_real(
            self.min_stop_pips,
            "min_stop_pips",
            lower=0.0,
            upper=MAX_STOP_PIPS,
            lower_open=True,
        )
        _bounded_real(
            self.max_leverage,
            "max_leverage",
            lower=0.0,
            upper=MAX_LEVERAGE,
            lower_open=True,
        )
        if self.max_position_units is not None:
            _bounded_real(
                self.max_position_units,
                "max_position_units",
                lower=0.0,
                upper=MAX_INPUT_MAGNITUDE,
                lower_open=True,
            )
        if not isinstance(self.allow_fractional_units, bool):
            raise ValueError("allow_fractional_units must be a boolean")


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
        self._leverage_locked = False

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
            or self._leverage_locked
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
        self._leverage_locked = False

    def on_bar(self, timestamp: Any, equity: float) -> None:
        equity = _bounded_real(
            equity,
            "equity",
            lower=-MAX_INPUT_MAGNITUDE,
            upper=MAX_INPUT_MAGNITUDE,
        )
        normalized = pd.Timestamp(timestamp)
        if pd.isna(normalized):
            raise ValueError("timestamp must be valid")
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
        equity = _bounded_real(
            equity,
            "equity",
            lower=-MAX_INPUT_MAGNITUDE,
            upper=MAX_INPUT_MAGNITUDE,
        )
        self.on_bar(timestamp, equity)
        if self._day_start_equity is None:
            return False
        threshold = self._day_start_equity * (1 - self.config.max_daily_loss_pct)
        if equity <= threshold:
            self._daily_locked = True
        return self._daily_locked

    def check_portfolio_stops(self, timestamp: Any, equity: float) -> list[str]:
        equity = _bounded_real(
            equity,
            "equity",
            lower=-MAX_INPUT_MAGNITUDE,
            upper=MAX_INPUT_MAGNITUDE,
        )
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
        if not isinstance(no_trade_window, bool):
            raise ValueError("no_trade_window must be a boolean")
        equity = _bounded_real(
            equity,
            "equity",
            lower=-MAX_INPUT_MAGNITUDE,
            upper=MAX_INPUT_MAGNITUDE,
        )
        self.on_bar(timestamp, equity)
        if no_trade_window:
            return False
        if self.risk_locked:
            return False
        return equity > 0

    def check_gross_leverage(self, gross_notional_usd: float, equity: float) -> bool:
        """Latch the run when marked-to-market gross leverage exceeds its cap."""

        gross_notional_usd = _bounded_real(
            gross_notional_usd,
            "gross_notional_usd",
            lower=0.0,
            upper=MAX_INPUT_MAGNITUDE,
        )
        equity = _bounded_real(
            equity,
            "equity",
            lower=-MAX_INPUT_MAGNITUDE,
            upper=MAX_INPUT_MAGNITUDE,
        )
        if equity <= 0 or gross_notional_usd > equity * self.config.max_leverage:
            self._leverage_locked = True
        return self._leverage_locked

    def position_size(
        self,
        symbol: str,
        equity: float,
        entry_price: float,
        stop_distance: float,
        extra_risk_per_unit_usd: float = 0.0,
        extra_risk_usd: float = 0.0,
        current_gross_notional_usd: float = 0.0,
        conversion_rates: dict[str, float] | None = None,
    ) -> tuple[float, float, float]:
        """Return units, adjusted stop distance, and initial risk in USD.

        The per-trade risk is capped by RiskConfig.risk_cap_pct.
        extra_risk_per_unit_usd is used for estimated round-trip spread/slippage/fees.
        extra_risk_usd is used for fixed or minimum fees that do not scale with size.
        """
        equity = _bounded_real(
            equity,
            "equity",
            lower=-MAX_INPUT_MAGNITUDE,
            upper=MAX_INPUT_MAGNITUDE,
        )
        entry_price = _bounded_real(
            entry_price,
            "entry_price",
            lower=0.0,
            upper=MAX_INPUT_MAGNITUDE,
            lower_open=True,
        )
        stop_distance = _bounded_real(
            stop_distance,
            "stop_distance",
            lower=0.0,
            upper=MAX_INPUT_MAGNITUDE,
            lower_open=True,
        )
        extra_risk_per_unit_usd = _bounded_real(
            extra_risk_per_unit_usd,
            "extra_risk_per_unit_usd",
            lower=0.0,
            upper=MAX_INPUT_MAGNITUDE,
        )
        extra_risk_usd = _bounded_real(
            extra_risk_usd,
            "extra_risk_usd",
            lower=0.0,
            upper=MAX_INPUT_MAGNITUDE,
        )
        current_gross_notional_usd = _bounded_real(
            current_gross_notional_usd,
            "current_gross_notional_usd",
            lower=0.0,
            upper=MAX_INPUT_MAGNITUDE,
        )
        _validate_conversion_rates(conversion_rates)
        inst = instrument_for(symbol)
        min_stop_distance = inst.pip_size * self.config.min_stop_pips
        adjusted_stop_distance = max(float(stop_distance), min_stop_distance)
        price_risk_per_unit = price_distance_to_usd_per_unit(
            symbol,
            adjusted_stop_distance,
            entry_price,
            conversion_rates,
        )
        price_risk_per_unit = _bounded_real(
            price_risk_per_unit,
            "price_risk_per_unit",
            lower=0.0,
            upper=MAX_INPUT_MAGNITUDE,
            lower_open=True,
        )
        total_risk_per_unit = _bounded_real(
            price_risk_per_unit + max(extra_risk_per_unit_usd, 0.0),
            "total_risk_per_unit",
            lower=0.0,
            upper=MAX_INPUT_MAGNITUDE,
            lower_open=True,
        )
        if total_risk_per_unit <= 0:
            return 0.0, adjusted_stop_distance, 0.0

        fixed_risk = max(float(extra_risk_usd), 0.0)
        risk_budget = equity * min(self.config.risk_per_trade_pct, self.config.risk_cap_pct)
        if risk_budget <= fixed_risk:
            return 0.0, adjusted_stop_distance, 0.0
        units = (risk_budget - fixed_risk) / total_risk_per_unit

        notional_per_unit = _bounded_real(
            notional_usd(symbol, 1.0, entry_price, conversion_rates),
            "notional_per_unit",
            lower=0.0,
            upper=MAX_INPUT_MAGNITUDE,
            lower_open=True,
        )
        remaining_notional = _bounded_real(
            max(
                0.0,
                equity * self.config.max_leverage - max(current_gross_notional_usd, 0.0),
            ),
            "remaining_notional",
            lower=0.0,
            upper=MAX_INPUT_MAGNITUDE,
        )
        max_units_by_leverage = remaining_notional / notional_per_unit
        units = min(units, max_units_by_leverage)
        if self.config.max_position_units is not None:
            units = min(units, self.config.max_position_units)
        units = _bounded_real(
            units,
            "units",
            lower=0.0,
            upper=MAX_INPUT_MAGNITUDE,
        )
        if not self.config.allow_fractional_units:
            units = float(int(units))
        if units <= 0:
            return 0.0, adjusted_stop_distance, 0.0

        initial_risk = units * total_risk_per_unit + fixed_risk
        if not isfinite(initial_risk) or initial_risk > MAX_INPUT_MAGNITUDE:
            raise ValueError("initial_risk must be finite and within the supported bound")
        return max(units, 0.0), adjusted_stop_distance, initial_risk


def _bounded_real(
    value: object,
    name: str,
    *,
    lower: float,
    upper: float,
    lower_open: bool = False,
) -> float:
    if not isinstance(value, Real) or isinstance(value, bool):
        raise ValueError(f"{name} must be a real number, not a boolean")
    numeric = float(value)
    if not isfinite(numeric):
        raise ValueError(f"{name} must be finite")
    below = numeric <= lower if lower_open else numeric < lower
    if below or numeric > upper:
        interval = f"({lower}, {upper}]" if lower_open else f"[{lower}, {upper}]"
        raise ValueError(f"{name} must be within {interval}")
    return numeric


def _validate_conversion_rates(conversion_rates: dict[str, float] | None) -> None:
    if conversion_rates is None:
        return
    if not isinstance(conversion_rates, dict):
        raise ValueError("conversion_rates must be a dictionary")
    for symbol, rate in conversion_rates.items():
        if not isinstance(symbol, str) or not symbol.strip():
            raise ValueError("conversion_rates keys must be non-empty symbols")
        instrument_for(symbol)
        _bounded_real(
            rate,
            f"conversion_rates[{symbol!r}]",
            lower=0.0,
            upper=MAX_INPUT_MAGNITUDE,
            lower_open=True,
        )
