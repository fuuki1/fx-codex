from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Iterable

import pandas as pd

from fx_backtester.engine import BacktestConfig
from fx_backtester.models import ACCOUNT_CURRENCY, TRADE_LOG_COLUMNS, instrument_for

REQUIRED_TRADE_LOG_COLUMNS = TRADE_LOG_COLUMNS


class ProductValidationError(ValueError):
    """Raised when a run is unsafe or not auditable enough for production use."""


@dataclass(frozen=True)
class ValidationReport:
    errors: tuple[str, ...]
    warnings: tuple[str, ...] = ()

    @property
    def passed(self) -> bool:
        return not self.errors

    def raise_for_errors(self) -> None:
        if self.errors:
            raise ProductValidationError("; ".join(self.errors))


def validate_backtest_inputs(
    data: dict[str, pd.DataFrame],
    config: BacktestConfig,
) -> ValidationReport:
    errors: list[str] = []
    warnings: list[str] = []

    if config.initial_cash <= 0:
        errors.append("initial_cash must be positive")
    _validate_pct("risk_per_trade_pct", config.risk.risk_per_trade_pct, errors)
    _validate_pct("risk_cap_pct", config.risk.risk_cap_pct, errors)
    _validate_pct("max_daily_loss_pct", config.risk.max_daily_loss_pct, errors)
    for name, value in (
        ("max_weekly_loss_pct", config.risk.max_weekly_loss_pct),
        ("max_monthly_drawdown_pct", config.risk.max_monthly_drawdown_pct),
        ("monthly_profit_target_pct", config.risk.monthly_profit_target_pct),
        ("hard_drawdown_pct", config.risk.hard_drawdown_pct),
    ):
        if value is not None:
            _validate_pct(name, value, errors)
    if config.risk.max_leverage <= 0:
        errors.append("max_leverage must be positive")
    if (
        config.risk.max_currency_exposure_pct is not None
        and config.risk.max_currency_exposure_pct <= 0
    ):
        errors.append("max_currency_exposure_pct must be positive when set")
    if config.risk.min_stop_pips <= 0:
        errors.append("min_stop_pips must be positive")
    if config.max_open_positions is not None and config.max_open_positions <= 0:
        errors.append("max_open_positions must be positive when set")
    if config.cooldown_bars_after_stop < 0:
        errors.append("cooldown_bars_after_stop must be >= 0")
    if config.execution.commission_per_million_usd < 0:
        errors.append("commission_per_million_usd must be >= 0")
    if config.execution.fixed_fee_usd < 0:
        errors.append("fixed_fee_usd must be >= 0")
    if config.execution.minimum_fee_usd < 0:
        errors.append("minimum_fee_usd must be >= 0")
    for name, values in (
        ("spread_time_multipliers", config.execution.spread_time_multipliers),
        ("slippage_time_multipliers", config.execution.slippage_time_multipliers),
    ):
        for hour, multiplier in values.items():
            if int(hour) < 0 or int(hour) > 23:
                errors.append(f"{name} hour must be 0-23, got {hour}")
            if multiplier <= 0:
                errors.append(f"{name} multiplier must be positive for hour {hour}")

    for symbol, frame in sorted(data.items()):
        inst = instrument_for(symbol)
        if len(frame) < 2:
            errors.append(f"{inst.symbol} requires at least two bars for next-open execution")
        missing_columns = {"open", "high", "low", "close"} - set(frame.columns)
        if missing_columns:
            errors.append(f"{inst.symbol} missing OHLC columns: {sorted(missing_columns)}")
            continue
        if frame.index.has_duplicates:
            errors.append(f"{inst.symbol} has duplicate timestamps")
        if not frame.index.is_monotonic_increasing:
            errors.append(f"{inst.symbol} timestamps must be sorted ascending")
        invalid_ohlc = (
            (frame["high"] < frame["low"])
            | (frame["open"] > frame["high"])
            | (frame["open"] < frame["low"])
            | (frame["close"] > frame["high"])
            | (frame["close"] < frame["low"])
        )
        if bool(invalid_ohlc.any()):
            errors.append(f"{inst.symbol} has invalid OHLC rows")

        spread_source = _spread_source(frame)
        if spread_source:
            if bool((frame[spread_source].astype(float) <= 0).any()):
                errors.append(f"{inst.symbol} {spread_source} must be positive on every row")
            if spread_source == "spread":
                warnings.append(
                    f"{inst.symbol} uses legacy spread column as pips; "
                    "prefer spread_pips or spread_price"
                )
        else:
            configured_spread = config.execution.spread_pips.get(inst.symbol)
            if configured_spread is None:
                errors.append(
                    f"{inst.symbol} requires spread_pips/spread_price column or spread_pips config"
                )
            elif configured_spread <= 0:
                errors.append(f"{inst.symbol} configured spread_pips must be positive")
            else:
                warnings.append(
                    f"{inst.symbol} uses configured spread_pips; no per-bar spread column"
                )

        configured_slippage = config.execution.slippage_pips.get(inst.symbol)
        if configured_slippage is None:
            errors.append(f"{inst.symbol} requires slippage_pips config")
        elif configured_slippage <= 0:
            errors.append(f"{inst.symbol} slippage_pips must be positive")

        if inst.base != ACCOUNT_CURRENCY and inst.quote != ACCOUNT_CURRENCY:
            usd_quote = f"{ACCOUNT_CURRENCY}{inst.quote}"
            quote_usd = f"{inst.quote}{ACCOUNT_CURRENCY}"
            available_symbols = set(data)
            available_conversions = set(config.conversion_rates)
            if (
                usd_quote not in available_symbols
                and quote_usd not in available_symbols
                and usd_quote not in available_conversions
                and quote_usd not in available_conversions
            ):
                errors.append(f"{inst.symbol} requires conversion rate {usd_quote} or {quote_usd}")

    return ValidationReport(tuple(errors), tuple(warnings))


def validate_trade_log_contract(trades: pd.DataFrame) -> ValidationReport:
    errors: list[str] = []
    if trades.empty:
        return ValidationReport(())

    missing = [column for column in REQUIRED_TRADE_LOG_COLUMNS if column not in trades.columns]
    if missing:
        errors.append(f"trade log missing required columns: {missing}")
    for column in ("spread_pips", "slippage_pips"):
        if column in trades.columns and bool((trades[column].astype(float) <= 0).any()):
            errors.append(f"trade log {column} must be positive")
    if {"signal_time", "fill_time"}.issubset(trades.columns):
        signal_time = pd.to_datetime(trades["signal_time"])
        fill_time = pd.to_datetime(trades["fill_time"])
        if bool((fill_time <= signal_time).any()):
            errors.append("trade fill_time must be after signal_time")
    return ValidationReport(tuple(errors))


def require_required_trade_columns() -> Iterable[str]:
    return REQUIRED_TRADE_LOG_COLUMNS


def _validate_pct(name: str, value: float, errors: list[str]) -> None:
    if value <= 0 or value >= 1:
        errors.append(f"{name} must be > 0 and < 1")


def _spread_source(frame: pd.DataFrame) -> str | None:
    if "spread_pips" in frame.columns:
        return "spread_pips"
    if "spread_price" in frame.columns:
        return "spread_price"
    if "spread" in frame.columns:
        return "spread"
    return None
