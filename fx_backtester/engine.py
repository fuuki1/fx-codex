from __future__ import annotations

from dataclasses import dataclass, field
from datetime import time
from math import isfinite
from typing import Any

import pandas as pd

from fx_backtester.data import build_no_trade_mask
from fx_backtester.execution import ExecutionConfig, SimulatedExecution
from fx_backtester.metrics import calculate_metrics
from fx_backtester.models import (
    TRADE_LOG_COLUMNS,
    Position,
    Trade,
    instrument_for,
    normalize_symbol,
    notional_usd,
    pnl_usd,
)
from fx_backtester.risk import RiskConfig, RiskManager
from fx_backtester.strategies.base import Strategy


@dataclass
class PendingOrder:
    symbol: str
    action: str
    side: int
    signal_time: Any
    order_time: Any
    reason: str
    order_type: str = "market"
    stop_distance: float | None = None
    take_profit_distance: float | None = None


@dataclass
class BacktestConfig:
    initial_cash: float = 100_000.0
    risk: RiskConfig = field(default_factory=RiskConfig)
    execution: ExecutionConfig = field(default_factory=ExecutionConfig)
    no_trade_minutes_before: int = 30
    no_trade_minutes_after: int = 30
    min_event_impact: str = "medium"
    max_open_positions: int | None = None
    cooldown_bars_after_stop: int = 0
    trading_start_time: str | None = None
    trading_end_time: str | None = None
    blocked_weekdays: tuple[int, ...] = ()
    conversion_rates: dict[str, float] = field(default_factory=dict)
    close_positions_on_daily_stop: bool = True
    close_positions_on_portfolio_stop: bool = True
    force_close_on_end: bool = True


@dataclass
class BacktestResult:
    equity_curve: pd.DataFrame
    trades: pd.DataFrame
    metrics: dict[str, float | int]


class BacktestEngine:
    """Event-based backtest engine with explicit strategy/risk/execution boundaries."""

    def __init__(
        self,
        strategy: Strategy,
        config: BacktestConfig | None = None,
        economic_events: pd.DataFrame | None = None,
    ) -> None:
        self.strategy = strategy
        self.config = config or BacktestConfig()
        self.risk = RiskManager(self.config.risk)
        self.execution = SimulatedExecution(self.config.execution)
        self.economic_events = economic_events if economic_events is not None else pd.DataFrame()

    def run(self, data: dict[str, pd.DataFrame]) -> BacktestResult:
        prepared = {
            normalize_symbol(symbol): frame.sort_index().copy() for symbol, frame in data.items()
        }
        signals = {
            symbol: self._validate_signal_output(
                symbol,
                frame,
                self.strategy.generate(symbol, frame),
            )
            for symbol, frame in prepared.items()
        }
        no_trade_masks = {
            symbol: build_no_trade_mask(
                frame.index,
                symbol,
                self.economic_events,
                self.config.no_trade_minutes_before,
                self.config.no_trade_minutes_after,
                self.config.min_event_impact,
            )
            for symbol, frame in prepared.items()
        }

        all_times = pd.DatetimeIndex(
            sorted(set().union(*(frame.index for frame in prepared.values())))
        )
        if all_times.empty:
            raise ValueError("No bars available")

        cash = float(self.config.initial_cash)
        positions: dict[str, Position] = {}
        last_prices: dict[str, float] = {}
        trades: list[Trade] = []
        equity_rows: list[dict[str, Any]] = []
        pending_orders: list[PendingOrder] = []
        entry_cooldowns: dict[str, int] = {}
        self.risk.reset()

        for timestamp in all_times:
            self._advance_cooldowns(entry_cooldowns)
            current_bars = self._bars_at(prepared, timestamp)
            open_conversion_rates = self._conversion_rates(current_bars, last_prices, "open")
            cash = self._fill_pending_orders(
                timestamp,
                current_bars,
                open_conversion_rates,
                pending_orders,
                positions,
                last_prices,
                no_trade_masks,
                trades,
                cash,
            )
            for symbol, row in current_bars.items():
                last_prices[symbol] = float(row["close"])

            equity = self._equity(cash, positions, last_prices)
            self.risk.on_bar(timestamp, equity)

            stopped_symbols, cash = self._apply_price_exits(
                timestamp,
                current_bars,
                self._conversion_rates(current_bars, last_prices, "open"),
                positions,
                trades,
                cash,
            )
            for symbol in stopped_symbols:
                if self.config.cooldown_bars_after_stop > 0:
                    entry_cooldowns[symbol] = max(
                        entry_cooldowns.get(symbol, 0),
                        self.config.cooldown_bars_after_stop + 1,
                    )

            equity = self._equity(cash, positions, last_prices)
            if self.risk.check_daily_loss(timestamp, equity):
                if self.config.close_positions_on_daily_stop:
                    self._schedule_close_all(
                        pending_orders,
                        timestamp,
                        "daily_loss_stop",
                        positions,
                    )
            self._apply_portfolio_stops(timestamp, positions, last_prices, pending_orders, cash)

            exit_symbols: list[str] = []
            desired_entries: dict[str, int] = {}
            for symbol in current_bars:
                target = int(signals[symbol].at[timestamp, "target_position"])
                position = positions.get(symbol)
                if position and target == 0:
                    exit_symbols.append(symbol)
                elif position and target == -position.direction:
                    exit_symbols.append(symbol)
                    desired_entries[symbol] = target
                elif not position and target != 0:
                    desired_entries[symbol] = target

            for symbol in exit_symbols:
                if symbol in positions:
                    self._schedule_close_order(
                        pending_orders,
                        symbol,
                        timestamp,
                        "signal_exit",
                    )

            equity = self._equity(cash, positions, last_prices)
            if self.risk.check_daily_loss(timestamp, equity):
                if self.config.close_positions_on_daily_stop:
                    self._schedule_close_all(
                        pending_orders,
                        timestamp,
                        "daily_loss_stop",
                        positions,
                    )
            self._apply_portfolio_stops(timestamp, positions, last_prices, pending_orders, cash)

            for symbol, target in desired_entries.items():
                pending_close = self._has_pending_close_order(pending_orders, symbol)
                if symbol in positions and not pending_close:
                    continue
                if symbol in stopped_symbols:
                    continue
                if symbol not in current_bars:
                    continue
                if self._has_pending_open_order(pending_orders, symbol):
                    continue
                if symbol in entry_cooldowns:
                    continue
                if self._max_positions_reached(positions):
                    continue
                if not self._entry_time_allowed(timestamp):
                    continue

                row = current_bars[symbol]
                equity = self._equity(cash, positions, last_prices)
                signal_entry_allowed = _optional_bool_signal_value(
                    signals[symbol],
                    timestamp,
                    "entry_allowed",
                    default=True,
                )
                no_trade_window = (
                    bool(no_trade_masks[symbol].at[timestamp]) or not signal_entry_allowed
                )
                if not self.risk.can_open(timestamp, equity, no_trade_window):
                    continue

                stop_distance = float(signals[symbol].at[timestamp, "stop_distance"])
                take_profit_distance = _optional_signal_value(
                    signals[symbol],
                    timestamp,
                    "take_profit_distance",
                )
                pending_orders.append(
                    PendingOrder(
                        symbol=symbol,
                        action="open",
                        side=target,
                        signal_time=timestamp,
                        order_time=timestamp,
                        reason="signal_entry",
                        stop_distance=stop_distance,
                        take_profit_distance=take_profit_distance,
                    )
                )

            equity_rows.append(
                {
                    "timestamp": timestamp,
                    "cash": cash,
                    "equity": self._equity(cash, positions, last_prices),
                    "open_positions": len(positions),
                    "daily_locked": self.risk.daily_locked,
                    "weekly_locked": self.risk.weekly_locked,
                    "monthly_locked": self.risk.monthly_locked,
                    "monthly_profit_locked": self.risk.monthly_profit_locked,
                    "hard_locked": self.risk.hard_locked,
                    "risk_locked": self.risk.risk_locked,
                }
            )

        if self.config.force_close_on_end and positions:
            final_time = all_times[-1]
            final_bars = self._bars_at(prepared, final_time)
            final_conversion_rates = self._conversion_rates(final_bars, last_prices, "close")
            for symbol in list(positions):
                if symbol in final_bars:
                    cash = self._close_position(
                        positions[symbol],
                        final_time,
                        float(final_bars[symbol]["close"]),
                        "end_of_backtest",
                        "market_on_close",
                        final_bars[symbol],
                        final_conversion_rates,
                        positions,
                        trades,
                        cash,
                    )
            equity_rows.append(
                {
                    "timestamp": final_time,
                    "cash": cash,
                    "equity": self._equity(cash, positions, last_prices),
                    "open_positions": len(positions),
                    "daily_locked": self.risk.daily_locked,
                    "weekly_locked": self.risk.weekly_locked,
                    "monthly_locked": self.risk.monthly_locked,
                    "monthly_profit_locked": self.risk.monthly_profit_locked,
                    "hard_locked": self.risk.hard_locked,
                    "risk_locked": self.risk.risk_locked,
                }
            )

        equity_curve = pd.DataFrame(equity_rows).set_index("timestamp")
        trades_frame = pd.DataFrame(
            [trade.to_dict() for trade in trades], columns=TRADE_LOG_COLUMNS
        )
        metrics = calculate_metrics(equity_curve, trades_frame, self.config.initial_cash)
        return BacktestResult(equity_curve=equity_curve, trades=trades_frame, metrics=metrics)

    def _bars_at(self, data: dict[str, pd.DataFrame], timestamp: Any) -> dict[str, pd.Series]:
        return {
            symbol: frame.loc[timestamp]
            for symbol, frame in data.items()
            if timestamp in frame.index
        }

    def _validate_signal_output(
        self,
        symbol: str,
        data: pd.DataFrame,
        signals: pd.DataFrame,
    ) -> pd.DataFrame:
        if not isinstance(signals, pd.DataFrame):
            raise ValueError(f"{self.strategy.name} signal output for {symbol} must be a DataFrame")
        required = {"target_position", "stop_distance"}
        missing = required - set(signals.columns)
        if missing:
            raise ValueError(
                f"{self.strategy.name} signal output for {symbol} "
                f"missing columns: {sorted(missing)}"
            )
        if signals.index.has_duplicates:
            raise ValueError(
                f"{self.strategy.name} signal output for {symbol} has duplicate timestamps"
            )
        if not signals.index.equals(data.index):
            raise ValueError(
                f"{self.strategy.name} signal output for {symbol} "
                "must be indexed exactly like input data"
            )

        output = signals.copy()
        target = pd.to_numeric(output["target_position"], errors="coerce")
        if bool(target.isna().any()) or not bool(target.map(isfinite).all()):
            raise ValueError(f"{self.strategy.name} target_position for {symbol} must be finite")
        if not bool(target.isin([-1, 0, 1]).all()):
            raise ValueError(
                f"{self.strategy.name} target_position for {symbol} must be -1, 0, or 1"
            )
        output["target_position"] = target.astype(int)

        stop_distance = pd.to_numeric(output["stop_distance"], errors="coerce")
        if (
            bool(stop_distance.isna().any())
            or not bool(stop_distance.map(isfinite).all())
            or bool((stop_distance <= 0).any())
        ):
            raise ValueError(f"{self.strategy.name} stop_distance for {symbol} must be positive")
        output["stop_distance"] = stop_distance.astype(float)

        if "take_profit_distance" in output.columns:
            raw_take_profit = output["take_profit_distance"]
            take_profit = pd.to_numeric(raw_take_profit, errors="coerce")
            provided = raw_take_profit.notna()
            invalid = provided & (
                take_profit.isna() | ~take_profit.map(isfinite) | (take_profit <= 0)
            )
            if bool(invalid.any()):
                raise ValueError(
                    f"{self.strategy.name} take_profit_distance for {symbol} "
                    "must be positive when set"
                )
            output["take_profit_distance"] = take_profit

        return output

    def _advance_cooldowns(self, entry_cooldowns: dict[str, int]) -> None:
        for symbol in list(entry_cooldowns):
            entry_cooldowns[symbol] -= 1
            if entry_cooldowns[symbol] <= 0:
                entry_cooldowns.pop(symbol, None)

    def _max_positions_reached(self, positions: dict[str, Position]) -> bool:
        if self.config.max_open_positions is None:
            return False
        return len(positions) >= self.config.max_open_positions

    def _has_pending_open_order(self, pending_orders: list[PendingOrder], symbol: str) -> bool:
        return any(order.symbol == symbol and order.action == "open" for order in pending_orders)

    def _has_pending_close_order(self, pending_orders: list[PendingOrder], symbol: str) -> bool:
        return any(order.symbol == symbol and order.action == "close" for order in pending_orders)

    def _entry_time_allowed(self, timestamp: Any) -> bool:
        normalized = pd.Timestamp(timestamp)
        if normalized.weekday() in set(self.config.blocked_weekdays):
            return False
        if self.config.trading_start_time is None and self.config.trading_end_time is None:
            return True

        current = normalized.time()
        start = _parse_time(self.config.trading_start_time, time.min)
        end = _parse_time(self.config.trading_end_time, time.max)
        if start <= end:
            return start <= current <= end
        return current >= start or current <= end

    def _equity(
        self,
        cash: float,
        positions: dict[str, Position],
        last_prices: dict[str, float],
    ) -> float:
        equity = cash
        conversion_rates = self.config.conversion_rates.copy()
        conversion_rates.update(last_prices)
        for symbol, position in positions.items():
            if symbol not in last_prices:
                continue
            equity += pnl_usd(
                symbol,
                position.direction,
                position.units,
                position.entry_price,
                last_prices[symbol],
                conversion_rates,
            )
        return float(equity)

    def _apply_portfolio_stops(
        self,
        timestamp: Any,
        positions: dict[str, Position],
        last_prices: dict[str, float],
        pending_orders: list[PendingOrder],
        cash: float,
    ) -> None:
        equity = self._equity(cash, positions, last_prices)
        reasons = self.risk.check_portfolio_stops(timestamp, equity)
        if reasons and self.config.close_positions_on_portfolio_stop:
            self._schedule_close_all(
                pending_orders,
                timestamp,
                reasons[-1],
                positions,
            )

    def _fill_pending_orders(
        self,
        timestamp: Any,
        current_bars: dict[str, pd.Series],
        conversion_rates: dict[str, float],
        pending_orders: list[PendingOrder],
        positions: dict[str, Position],
        last_prices: dict[str, float],
        no_trade_masks: dict[str, pd.Series],
        trades: list[Trade],
        cash: float,
    ) -> float:
        remaining: list[PendingOrder] = []
        for order in pending_orders:
            row = current_bars.get(order.symbol)
            if row is None:
                remaining.append(order)
                continue

            expected_price = float(row["open"])
            if order.action == "close":
                position = positions.get(order.symbol)
                if position is not None:
                    cash = self._close_position(
                        position,
                        timestamp,
                        expected_price,
                        order.reason,
                        order.order_type,
                        row,
                        conversion_rates,
                        positions,
                        trades,
                        cash,
                    )
                continue

            if order.action != "open" or order.symbol in positions:
                continue
            equity = self._equity(cash, positions, last_prices)
            no_trade_window = bool(no_trade_masks[order.symbol].at[timestamp])
            if not self.risk.can_open(timestamp, equity, no_trade_window):
                continue
            if self._max_positions_reached(positions):
                continue

            extra_risk = self.execution.round_trip_cost_per_unit_usd(
                order.symbol,
                expected_price,
                row,
                conversion_rates,
            )
            estimated_fill_price = self.execution.fill_price(
                order.symbol,
                expected_price,
                order.side,
                row,
            )
            units, adjusted_stop_distance, initial_risk = self.risk.position_size(
                order.symbol,
                equity,
                estimated_fill_price,
                float(order.stop_distance or 0.0),
                extra_risk_per_unit_usd=extra_risk,
                extra_risk_usd=self.execution.round_trip_fixed_fee_floor_usd(),
                current_gross_notional_usd=self._gross_notional_usd(
                    positions, last_prices, conversion_rates
                ),
                conversion_rates=conversion_rates,
            )
            if units <= 0 or initial_risk <= 0:
                continue
            if not self._currency_exposure_allowed(
                order.symbol,
                order.side,
                units,
                estimated_fill_price,
                equity,
                positions,
                last_prices,
                conversion_rates,
            ):
                continue

            fill = self.execution.execute_market(
                order.symbol,
                expected_price,
                order.side,
                units,
                row,
                order.order_type,
                conversion_rates,
            )
            stop_price = fill.price - order.side * adjusted_stop_distance
            take_profit_price = None
            if order.take_profit_distance is not None and order.take_profit_distance > 0:
                take_profit_price = fill.price + order.side * float(order.take_profit_distance)
            cash -= fill.fee
            positions[order.symbol] = Position(
                symbol=order.symbol,
                direction=order.side,
                units=units,
                signal_time=order.signal_time,
                order_time=order.order_time,
                entry_time=timestamp,
                expected_entry_price=fill.expected_price,
                entry_price=fill.price,
                stop_price=stop_price,
                take_profit_price=take_profit_price,
                entry_fee=fill.fee,
                entry_spread_pips=fill.spread_pips,
                entry_slippage_pips=fill.slippage_pips,
                entry_order_type=fill.order_type,
                initial_risk_usd=initial_risk,
                strategy=self.strategy.name,
            )

        pending_orders[:] = remaining
        return cash

    def _apply_price_exits(
        self,
        timestamp: Any,
        current_bars: dict[str, pd.Series],
        conversion_rates: dict[str, float],
        positions: dict[str, Position],
        trades: list[Trade],
        cash: float,
    ) -> tuple[set[str], float]:
        stopped_symbols: set[str] = set()
        for symbol, row in current_bars.items():
            position = positions.get(symbol)
            if not position:
                continue
            stop_hit = (position.direction == 1 and float(row["low"]) <= position.stop_price) or (
                position.direction == -1 and float(row["high"]) >= position.stop_price
            )
            take_profit_hit = self._take_profit_confirmed(position, row)
            # Conservative intrabar assumption: if both levels are touched, stop loss wins.
            if stop_hit:
                stop_price = self._stop_execution_price(position, row)
                cash = self._close_position(
                    position,
                    timestamp,
                    stop_price,
                    "stop_loss",
                    "stop_market",
                    row,
                    conversion_rates,
                    positions,
                    trades,
                    cash,
                )
                stopped_symbols.add(symbol)
            elif take_profit_hit and position.take_profit_price is not None:
                cash = self._close_position(
                    position,
                    timestamp,
                    position.take_profit_price,
                    "take_profit",
                    "limit",
                    row,
                    conversion_rates,
                    positions,
                    trades,
                    cash,
                )
        return stopped_symbols, cash

    def _stop_execution_price(self, position: Position, row: pd.Series) -> float:
        open_price = float(row["open"])
        if position.direction == 1 and open_price <= position.stop_price:
            return open_price
        if position.direction == -1 and open_price >= position.stop_price:
            return open_price
        return position.stop_price

    def _take_profit_confirmed(self, position: Position, row: pd.Series) -> bool:
        if position.take_profit_price is None:
            return False
        inst = self.execution
        buffer = inst.spread_pips(position.symbol, row) / 2 + inst.slippage_pips(
            position.symbol, row
        )
        pip_size = instrument_for(position.symbol).pip_size
        price_buffer = buffer * pip_size
        if position.direction == 1:
            return float(row["high"]) >= position.take_profit_price + price_buffer
        return float(row["low"]) <= position.take_profit_price - price_buffer

    def _schedule_close_all(
        self,
        pending_orders: list[PendingOrder],
        timestamp: Any,
        reason: str,
        positions: dict[str, Position],
    ) -> None:
        for symbol in list(positions):
            self._schedule_close_order(pending_orders, symbol, timestamp, reason)

    def _schedule_close_order(
        self,
        pending_orders: list[PendingOrder],
        symbol: str,
        timestamp: Any,
        reason: str,
    ) -> None:
        if self._has_pending_close_order(pending_orders, symbol):
            return
        pending_orders.append(
            PendingOrder(
                symbol=symbol,
                action="close",
                side=0,
                signal_time=timestamp,
                order_time=timestamp,
                reason=reason,
            )
        )

    def _close_position(
        self,
        position: Position,
        timestamp: Any,
        mid_price: float,
        reason: str,
        order_type: str,
        row: pd.Series,
        conversion_rates: dict[str, float],
        positions: dict[str, Position],
        trades: list[Trade],
        cash: float,
    ) -> float:
        exit_side = -position.direction
        fill = self.execution.execute_market(
            position.symbol,
            mid_price,
            exit_side,
            position.units,
            row,
            order_type,
            conversion_rates,
        )
        gross_pnl = pnl_usd(
            position.symbol,
            position.direction,
            position.units,
            position.entry_price,
            fill.price,
            conversion_rates,
        )
        net_pnl = gross_pnl - position.entry_fee - fill.fee
        r_multiple = net_pnl / position.initial_risk_usd if position.initial_risk_usd else 0.0
        trade = Trade(
            symbol=position.symbol,
            strategy=position.strategy,
            direction=position.direction,
            units=position.units,
            signal_time=position.signal_time,
            order_time=position.order_time,
            fill_time=position.entry_time,
            side="buy" if position.direction == 1 else "sell",
            expected_price=position.expected_entry_price,
            fill_price=position.entry_price,
            spread_pips=position.entry_spread_pips,
            slippage_pips=position.entry_slippage_pips,
            order_type=position.entry_order_type,
            entry_time=position.entry_time,
            exit_time=timestamp,
            entry_price=position.entry_price,
            exit_price=fill.price,
            exit_expected_price=fill.expected_price,
            exit_fill_price=fill.price,
            exit_spread_pips=fill.spread_pips,
            exit_slippage_pips=fill.slippage_pips,
            exit_order_type=fill.order_type,
            stop_price=position.stop_price,
            take_profit_price=position.take_profit_price,
            gross_pnl=gross_pnl,
            fees=position.entry_fee + fill.fee,
            net_pnl=net_pnl,
            initial_risk_usd=position.initial_risk_usd,
            r_multiple=r_multiple,
            reason=reason,
            exit_reason=reason,
        )
        trades.append(trade)
        positions.pop(position.symbol, None)
        cash_delta = gross_pnl - fill.fee
        return cash + cash_delta

    def _conversion_rates(
        self,
        current_bars: dict[str, pd.Series],
        last_prices: dict[str, float],
        price_column: str,
    ) -> dict[str, float]:
        rates = self.config.conversion_rates.copy()
        rates.update(last_prices)
        for symbol, row in current_bars.items():
            if price_column in row:
                rates[symbol] = float(row[price_column])
            elif "close" in row:
                rates[symbol] = float(row["close"])
        return rates

    def _currency_exposure_allowed(
        self,
        symbol: str,
        direction: int,
        units: float,
        price: float,
        equity: float,
        positions: dict[str, Position],
        last_prices: dict[str, float],
        conversion_rates: dict[str, float],
    ) -> bool:
        cap = self.config.risk.max_currency_exposure_pct
        if cap is None:
            return True
        exposures = self._currency_exposures_usd(positions, last_prices, conversion_rates)
        self._add_currency_exposure(
            exposures,
            symbol,
            direction,
            units,
            price,
            conversion_rates,
        )
        limit = equity * cap
        return all(abs(value) <= limit for value in exposures.values())

    def _currency_exposures_usd(
        self,
        positions: dict[str, Position],
        last_prices: dict[str, float],
        conversion_rates: dict[str, float],
    ) -> dict[str, float]:
        exposures: dict[str, float] = {}
        for symbol, position in positions.items():
            price = last_prices.get(symbol, position.entry_price)
            self._add_currency_exposure(
                exposures,
                symbol,
                position.direction,
                position.units,
                price,
                conversion_rates,
            )
        return exposures

    def _gross_notional_usd(
        self,
        positions: dict[str, Position],
        last_prices: dict[str, float],
        conversion_rates: dict[str, float],
    ) -> float:
        return sum(
            notional_usd(
                symbol,
                position.units,
                last_prices.get(symbol, position.entry_price),
                conversion_rates,
            )
            for symbol, position in positions.items()
        )

    def _add_currency_exposure(
        self,
        exposures: dict[str, float],
        symbol: str,
        direction: int,
        units: float,
        price: float,
        conversion_rates: dict[str, float],
    ) -> None:
        inst = instrument_for(symbol)
        exposure_usd = notional_usd(symbol, units, price, conversion_rates)
        exposures[inst.base] = exposures.get(inst.base, 0.0) + direction * exposure_usd
        exposures[inst.quote] = exposures.get(inst.quote, 0.0) - direction * exposure_usd


def _parse_time(value: str | None, default: time) -> time:
    if value is None:
        return default
    try:
        hour, minute = value.split(":", 1)
        return time(int(hour), int(minute))
    except ValueError as error:
        raise ValueError(f"Expected HH:MM time, got {value!r}") from error


def _optional_signal_value(frame: pd.DataFrame, timestamp: Any, column: str) -> float | None:
    if column not in frame.columns:
        return None
    value = frame.at[timestamp, column]
    if pd.isna(value):
        return None
    return float(value)


def _optional_bool_signal_value(
    frame: pd.DataFrame,
    timestamp: Any,
    column: str,
    default: bool,
) -> bool:
    if column not in frame.columns:
        return default
    value = frame.at[timestamp, column]
    if pd.isna(value):
        return default
    return bool(value)
