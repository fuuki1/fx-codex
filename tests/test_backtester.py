from __future__ import annotations

import argparse
import json

import pandas as pd

import fx_backtester
from fx_backtester.cli import (
    _add_common_arguments,
    _build_config,
    _build_strategy_from_args,
    _webhook_secret,
    main,
)
from fx_backtester.analysis import monthly_target_summary
from fx_backtester.data import (
    build_no_trade_mask,
    filter_price_data_by_date,
    load_economic_events_csv,
    load_price_csv,
)
from fx_backtester.engine import BacktestConfig, BacktestEngine
from fx_backtester.execution import ExecutionConfig, SimulatedExecution
from fx_backtester.risk import RiskConfig
from fx_backtester.strategies import AILogisticStrategy
from fx_backtester.strategies.base import Strategy
from fx_backtester.strategies.filters import (
    FilteredStrategy,
    NoTradeFilterConfig,
    RegimeFilterConfig,
)
from fx_backtester.tradingview import append_tradingview_alert, parse_tradingview_alert
from fx_backtester.validation import ProductValidationError, validate_backtest_inputs
from fx_backtester.walk_forward import WalkForwardConfig, WalkForwardValidator


class AlwaysLongStrategy(Strategy):
    @property
    def name(self) -> str:
        return "always_long"

    def generate(self, symbol: str, data: pd.DataFrame) -> pd.DataFrame:
        return self._validated_output(
            data,
            pd.DataFrame(
                {
                    "target_position": 1,
                    "stop_distance": 0.01,
                },
                index=data.index,
            ),
        )


class WideStopLongStrategy(Strategy):
    @property
    def name(self) -> str:
        return "wide_stop_long"

    def generate(self, symbol: str, data: pd.DataFrame) -> pd.DataFrame:
        return self._validated_output(
            data,
            pd.DataFrame(
                {
                    "target_position": 1,
                    "stop_distance": 0.05,
                },
                index=data.index,
            ),
        )


class AlwaysShortStrategy(Strategy):
    @property
    def name(self) -> str:
        return "always_short"

    def generate(self, symbol: str, data: pd.DataFrame) -> pd.DataFrame:
        return self._validated_output(
            data,
            pd.DataFrame(
                {
                    "target_position": -1,
                    "stop_distance": 0.05,
                },
                index=data.index,
            ),
        )


class SymbolOnlyLongStrategy(Strategy):
    def __init__(self, traded_symbol: str, stop_distance: float = 1.0) -> None:
        self.traded_symbol = traded_symbol
        self.stop_distance = stop_distance

    @property
    def name(self) -> str:
        return "symbol_only_long"

    def generate(self, symbol: str, data: pd.DataFrame) -> pd.DataFrame:
        target = 1 if symbol == self.traded_symbol else 0
        return self._validated_output(
            data,
            pd.DataFrame(
                {
                    "target_position": target,
                    "stop_distance": self.stop_distance,
                },
                index=data.index,
            ),
        )


class ReversalStrategy(Strategy):
    @property
    def name(self) -> str:
        return "reversal"

    def generate(self, symbol: str, data: pd.DataFrame) -> pd.DataFrame:
        target = [1, 1, -1, -1, 0][: len(data)]
        return self._validated_output(
            data,
            pd.DataFrame(
                {
                    "target_position": target,
                    "stop_distance": 0.05,
                },
                index=data.index,
            ),
        )


class TakeProfitStrategy(Strategy):
    def __init__(self, stop_distance: float = 0.1, take_profit_distance: float = 0.01) -> None:
        self.stop_distance = stop_distance
        self.take_profit_distance = take_profit_distance

    @property
    def name(self) -> str:
        return "take_profit"

    def generate(self, symbol: str, data: pd.DataFrame) -> pd.DataFrame:
        return self._validated_output(
            data,
            pd.DataFrame(
                {
                    "target_position": 1,
                    "stop_distance": self.stop_distance,
                    "take_profit_distance": self.take_profit_distance,
                },
                index=data.index,
            ),
        )


class AlternatingStrategy(Strategy):
    def __init__(self, stop_distance: float = 0.01) -> None:
        self.stop_distance = stop_distance

    @property
    def name(self) -> str:
        return "alternating"

    def generate(self, symbol: str, data: pd.DataFrame) -> pd.DataFrame:
        target = [1 if index % 2 == 0 else 0 for index in range(len(data))]
        return self._validated_output(
            data,
            pd.DataFrame(
                {"target_position": target, "stop_distance": self.stop_distance},
                index=data.index,
            ),
        )


class MissingStopStrategy(Strategy):
    @property
    def name(self) -> str:
        return "missing_stop"

    def generate(self, symbol: str, data: pd.DataFrame) -> pd.DataFrame:
        return pd.DataFrame({"target_position": 1}, index=data.index)


class ShortSignalIndexStrategy(Strategy):
    @property
    def name(self) -> str:
        return "short_signal_index"

    def generate(self, symbol: str, data: pd.DataFrame) -> pd.DataFrame:
        return pd.DataFrame(
            {
                "target_position": 1,
                "stop_distance": 0.01,
            },
            index=data.index[:-1],
        )


def _zero_cost_config() -> ExecutionConfig:
    return ExecutionConfig(
        spread_pips={"EURUSD": 0.01, "USDJPY": 0.01, "GBPUSD": 0.01},
        slippage_pips={"EURUSD": 0.01, "USDJPY": 0.01, "GBPUSD": 0.01},
        commission_per_million_usd=0.0,
    )


def _ai_price_frame(periods: int = 180) -> pd.DataFrame:
    index = pd.date_range("2024-01-01 00:00:00", periods=periods, freq="h")
    rows = []
    previous_close = 1.0
    for position in range(periods):
        regime = 0.0010 if (position // 12) % 2 == 0 else -0.0008
        oscillation = ((position % 5) - 2) * 0.00008
        close = previous_close * (1 + regime + oscillation)
        open_ = previous_close
        high = max(open_, close) + 0.0015
        low = min(open_, close) - 0.0015
        rows.append(
            {
                "open": open_,
                "high": high,
                "low": low,
                "close": close,
                "spread": 1.0,
            }
        )
        previous_close = close
    return pd.DataFrame(rows, index=index)


def test_package_version_matches_pyproject() -> None:
    version_line = next(
        line for line in open("pyproject.toml", encoding="utf-8") if line.startswith("version = ")
    )
    pyproject_version = version_line.split("=", 1)[1].strip().strip('"')

    assert fx_backtester.__version__ == pyproject_version


def test_load_price_csv_with_symbol_column(tmp_path) -> None:
    path = tmp_path / "prices.csv"
    path.write_text(
        "timestamp,symbol,open,high,low,close\n" "2024-01-01 00:00:00,EUR/USD,1.1,1.2,1.0,1.15\n",
        encoding="utf-8",
    )

    loaded = load_price_csv(path)

    assert list(loaded) == ["EURUSD"]
    assert float(loaded["EURUSD"].iloc[0]["close"]) == 1.15


def test_filter_price_data_by_date_includes_full_end_date() -> None:
    index = pd.date_range("2025-01-01 00:00:00", periods=72, freq="h")
    data = {
        "EURUSD": pd.DataFrame(
            {
                "open": [1.0] * 72,
                "high": [1.01] * 72,
                "low": [0.99] * 72,
                "close": [1.0] * 72,
            },
            index=index,
        )
    }

    filtered = filter_price_data_by_date(data, "2025-01-02", "2025-01-02")

    assert filtered["EURUSD"].index[0] == pd.Timestamp("2025-01-02 00:00:00")
    assert filtered["EURUSD"].index[-1] == pd.Timestamp("2025-01-02 23:00:00")


def test_economic_event_mask_blocks_relevant_currency(tmp_path) -> None:
    path = tmp_path / "events.csv"
    path.write_text(
        "timestamp,currency,impact,name\n" "2024-01-01 01:00:00,USD,high,FOMC\n",
        encoding="utf-8",
    )
    events = load_economic_events_csv(path)
    index = pd.date_range("2024-01-01 00:00:00", periods=4, freq="h")

    mask = build_no_trade_mask(index, "EURUSD", events, minutes_before=30, minutes_after=30)

    assert mask.tolist() == [False, True, False, False]


def test_daily_loss_stop_prevents_more_entries() -> None:
    index = pd.date_range("2024-01-01 00:00:00", periods=8, freq="h")
    data = {
        "EURUSD": pd.DataFrame(
            {
                "open": [1.00] * 8,
                "high": [1.01] * 8,
                "low": [1.00, 0.99, 1.00, 0.99, 1.00, 0.99, 1.00, 1.00],
                "close": [1.00] * 8,
            },
            index=index,
        )
    }
    config = BacktestConfig(
        initial_cash=100_000,
        risk=RiskConfig(risk_per_trade_pct=0.01, max_daily_loss_pct=0.02, max_leverage=10),
        execution=_zero_cost_config(),
    )

    result = BacktestEngine(AlwaysLongStrategy(), config).run(data)

    assert len(result.trades) == 3
    assert result.trades["reason"].tolist() == ["stop_loss", "stop_loss", "stop_loss"]
    assert bool(result.equity_curve.iloc[-1]["daily_locked"]) is True


def test_no_trade_window_blocks_new_entries() -> None:
    index = pd.date_range("2024-01-01 00:00:00", periods=3, freq="h")
    data = {
        "EURUSD": pd.DataFrame(
            {
                "open": [1.0, 1.0, 1.0],
                "high": [1.01, 1.01, 1.01],
                "low": [0.99, 0.99, 0.99],
                "close": [1.0, 1.0, 1.0],
            },
            index=index,
        )
    }
    events = pd.DataFrame(
        [
            {
                "timestamp": index[0],
                "currency": "USD",
                "symbol": "",
                "impact": "high",
                "name": "event",
            }
        ]
    ).set_index("timestamp")
    config = BacktestConfig(
        initial_cash=100_000,
        risk=RiskConfig(),
        execution=_zero_cost_config(),
        no_trade_minutes_before=0,
        no_trade_minutes_after=180,
    )

    result = BacktestEngine(AlwaysLongStrategy(), config, events).run(data)

    assert result.trades.empty


def test_max_open_positions_limits_simultaneous_entries() -> None:
    index = pd.date_range("2024-01-01 00:00:00", periods=3, freq="h")
    data = {
        "EURUSD": pd.DataFrame(
            {
                "open": [1.0] * 3,
                "high": [1.02] * 3,
                "low": [1.0] * 3,
                "close": [1.0] * 3,
            },
            index=index,
        ),
        "GBPUSD": pd.DataFrame(
            {
                "open": [1.3] * 3,
                "high": [1.32] * 3,
                "low": [1.3] * 3,
                "close": [1.3] * 3,
            },
            index=index,
        ),
    }
    config = BacktestConfig(
        initial_cash=100_000,
        risk=RiskConfig(),
        execution=_zero_cost_config(),
        max_open_positions=1,
    )

    result = BacktestEngine(AlwaysLongStrategy(), config).run(data)

    assert int(result.equity_curve["open_positions"].max()) == 1
    assert len(result.trades) == 1


def test_trading_session_blocks_entries_outside_window() -> None:
    index = pd.date_range("2024-01-01 01:00:00", periods=3, freq="h")
    data = {
        "EURUSD": pd.DataFrame(
            {
                "open": [1.0] * 3,
                "high": [1.01] * 3,
                "low": [0.99] * 3,
                "close": [1.0] * 3,
            },
            index=index,
        )
    }
    config = BacktestConfig(
        initial_cash=100_000,
        risk=RiskConfig(),
        execution=_zero_cost_config(),
        trading_start_time="09:00",
        trading_end_time="17:00",
    )

    result = BacktestEngine(AlwaysLongStrategy(), config).run(data)

    assert result.trades.empty


def test_fixed_and_minimum_execution_fees() -> None:
    execution = SimulatedExecution(
        ExecutionConfig(
            spread_pips={"EURUSD": 0.0},
            slippage_pips={"EURUSD": 0.0},
            commission_per_million_usd=0.0,
            fixed_fee_usd=1.0,
            minimum_fee_usd=2.0,
        )
    )

    assert execution.commission("EURUSD", 1_000, 1.0) == 2.0
    assert execution.round_trip_fixed_fee_floor_usd() == 4.0


def test_fixed_and_minimum_fees_reduce_position_size_risk_estimate() -> None:
    index = pd.date_range("2024-01-01 00:00:00", periods=3, freq="h")
    data = {
        "EURUSD": pd.DataFrame(
            {
                "open": [1.0, 1.0, 1.0],
                "high": [1.01, 1.01, 1.01],
                "low": [0.99, 0.99, 0.99],
                "close": [1.0, 1.0, 1.0],
            },
            index=index,
        )
    }
    base_config = BacktestConfig(
        initial_cash=100_000,
        risk=RiskConfig(risk_per_trade_pct=0.005, risk_cap_pct=0.005, max_leverage=100),
        execution=ExecutionConfig(
            spread_pips={"EURUSD": 0.1},
            slippage_pips={"EURUSD": 0.1},
            commission_per_million_usd=0.0,
        ),
    )
    minimum_fee_config = BacktestConfig(
        initial_cash=100_000,
        risk=RiskConfig(risk_per_trade_pct=0.005, risk_cap_pct=0.005, max_leverage=100),
        execution=ExecutionConfig(
            spread_pips={"EURUSD": 0.1},
            slippage_pips={"EURUSD": 0.1},
            commission_per_million_usd=0.0,
            minimum_fee_usd=100.0,
        ),
    )

    base_trade = BacktestEngine(WideStopLongStrategy(), base_config).run(data).trades.iloc[0]
    minimum_fee_trade = (
        BacktestEngine(WideStopLongStrategy(), minimum_fee_config).run(data).trades.iloc[0]
    )

    assert float(minimum_fee_trade["units"]) < float(base_trade["units"])
    assert float(minimum_fee_trade["initial_risk_usd"]) <= 500.0
    assert float(minimum_fee_trade["fees"]) == 200.0


def test_market_entry_fills_next_open_with_spread_column_and_slippage() -> None:
    index = pd.date_range("2024-01-01 00:00:00", periods=3, freq="h")
    data = {
        "EURUSD": pd.DataFrame(
            {
                "open": [1.00, 1.10, 1.20],
                "high": [1.06, 1.16, 1.26],
                "low": [0.99, 1.09, 1.19],
                "close": [1.05, 1.15, 1.25],
                "spread": [2.0, 2.0, 2.0],
            },
            index=index,
        )
    }
    config = BacktestConfig(
        initial_cash=100_000,
        risk=RiskConfig(risk_per_trade_pct=0.005, max_leverage=2),
        execution=ExecutionConfig(
            spread_pips={"EURUSD": 9.0},
            slippage_pips={"EURUSD": 0.5},
            commission_per_million_usd=0.0,
        ),
    )

    result = BacktestEngine(WideStopLongStrategy(), config).run(data)
    trade = result.trades.iloc[0]

    assert trade["signal_time"] == index[0]
    assert trade["order_time"] == index[0]
    assert trade["fill_time"] == index[1]
    assert trade["expected_price"] == 1.10
    assert round(float(trade["fill_price"]), 5) == 1.10015
    assert float(trade["spread_pips"]) == 2.0
    assert float(trade["slippage_pips"]) == 0.5
    assert trade["side"] == "buy"
    assert trade["order_type"] == "market"
    assert trade["exit_reason"] == "end_of_backtest"


def test_spread_price_column_is_converted_to_pips() -> None:
    index = pd.date_range("2024-01-01 00:00:00", periods=3, freq="h")
    data = {
        "EURUSD": pd.DataFrame(
            {
                "open": [1.00, 1.10, 1.20],
                "high": [1.06, 1.16, 1.26],
                "low": [0.99, 1.09, 1.19],
                "close": [1.05, 1.15, 1.25],
                "spread_price": [0.0002, 0.0002, 0.0002],
            },
            index=index,
        )
    }
    config = BacktestConfig(
        initial_cash=100_000,
        risk=RiskConfig(risk_per_trade_pct=0.005, max_leverage=2),
        execution=ExecutionConfig(
            spread_pips={"EURUSD": 9.0},
            slippage_pips={"EURUSD": 0.5},
            commission_per_million_usd=0.0,
        ),
    )

    result = BacktestEngine(WideStopLongStrategy(), config).run(data)
    trade = result.trades.iloc[0]

    assert round(float(trade["spread_pips"]), 5) == 2.0
    assert round(float(trade["fill_price"]), 5) == 1.10015


def test_short_market_entry_uses_bid_and_adverse_slippage() -> None:
    index = pd.date_range("2024-01-01 00:00:00", periods=3, freq="h")
    data = {
        "EURUSD": pd.DataFrame(
            {
                "open": [1.00, 1.10, 1.20],
                "high": [1.06, 1.16, 1.26],
                "low": [0.99, 1.09, 1.19],
                "close": [1.05, 1.15, 1.25],
                "spread": [2.0, 2.0, 2.0],
            },
            index=index,
        )
    }
    config = BacktestConfig(
        initial_cash=100_000,
        risk=RiskConfig(risk_per_trade_pct=0.005, max_leverage=2),
        execution=ExecutionConfig(
            spread_pips={"EURUSD": 2.0},
            slippage_pips={"EURUSD": 0.5},
            commission_per_million_usd=0.0,
        ),
    )

    result = BacktestEngine(AlwaysShortStrategy(), config).run(data)
    trade = result.trades.iloc[0]

    assert trade["side"] == "sell"
    assert trade["expected_price"] == 1.10
    assert round(float(trade["fill_price"]), 5) == 1.09985


def test_zero_spread_or_slippage_is_rejected() -> None:
    index = pd.date_range("2024-01-01 00:00:00", periods=2, freq="h")
    data = {
        "EURUSD": pd.DataFrame(
            {
                "open": [1.0, 1.0],
                "high": [1.01, 1.01],
                "low": [0.99, 0.99],
                "close": [1.0, 1.0],
            },
            index=index,
        )
    }
    config = BacktestConfig(
        execution=ExecutionConfig(
            spread_pips={"EURUSD": 0.0},
            slippage_pips={"EURUSD": 0.1},
        )
    )

    try:
        BacktestEngine(AlwaysLongStrategy(), config).run(data)
    except ValueError as error:
        assert "spread must be positive" in str(error)
    else:
        raise AssertionError("Expected zero spread to be rejected")

    config = BacktestConfig(
        execution=ExecutionConfig(
            spread_pips={"EURUSD": 0.1},
            slippage_pips={"EURUSD": 0.0},
        )
    )

    try:
        BacktestEngine(AlwaysLongStrategy(), config).run(data)
    except ValueError as error:
        assert "slippage_pips must be positive" in str(error)
    else:
        raise AssertionError("Expected zero slippage to be rejected")


def test_engine_rejects_missing_signal_columns() -> None:
    index = pd.date_range("2024-01-01 00:00:00", periods=2, freq="h")
    data = {
        "EURUSD": pd.DataFrame(
            {
                "open": [1.0, 1.0],
                "high": [1.01, 1.01],
                "low": [0.99, 0.99],
                "close": [1.0, 1.0],
            },
            index=index,
        )
    }

    try:
        BacktestEngine(
            MissingStopStrategy(),
            BacktestConfig(execution=_zero_cost_config()),
        ).run(data)
    except ValueError as error:
        assert "missing columns: ['stop_distance']" in str(error)
    else:
        raise AssertionError("Expected invalid signal schema to be rejected")


def test_engine_rejects_signal_index_mismatch() -> None:
    index = pd.date_range("2024-01-01 00:00:00", periods=3, freq="h")
    data = {
        "EURUSD": pd.DataFrame(
            {
                "open": [1.0, 1.0, 1.0],
                "high": [1.01, 1.01, 1.01],
                "low": [0.99, 0.99, 0.99],
                "close": [1.0, 1.0, 1.0],
            },
            index=index,
        )
    }

    try:
        BacktestEngine(
            ShortSignalIndexStrategy(),
            BacktestConfig(execution=_zero_cost_config()),
        ).run(data)
    except ValueError as error:
        assert "must be indexed exactly like input data" in str(error)
    else:
        raise AssertionError("Expected signal index mismatch to be rejected")


def test_product_validation_rejects_unconfigured_symbol_costs() -> None:
    index = pd.date_range("2024-01-01 00:00:00", periods=2, freq="h")
    data = {
        "AUDUSD": pd.DataFrame(
            {
                "open": [0.7, 0.71],
                "high": [0.71, 0.72],
                "low": [0.69, 0.70],
                "close": [0.705, 0.715],
            },
            index=index,
        )
    }

    try:
        validate_backtest_inputs(data, BacktestConfig()).raise_for_errors()
    except ProductValidationError as error:
        assert "AUDUSD requires spread_pips/spread_price column or spread_pips config" in str(error)
        assert "AUDUSD requires slippage_pips config" in str(error)
    else:
        raise AssertionError("Expected missing commercial cost settings to be rejected")


def test_product_validation_rejects_negative_execution_fees() -> None:
    index = pd.date_range("2024-01-01 00:00:00", periods=2, freq="h")
    data = {
        "EURUSD": pd.DataFrame(
            {
                "open": [1.0, 1.0],
                "high": [1.01, 1.01],
                "low": [0.99, 0.99],
                "close": [1.0, 1.0],
                "spread_pips": [1.0, 1.0],
            },
            index=index,
        )
    }
    config = BacktestConfig(
        execution=ExecutionConfig(
            spread_pips={"EURUSD": 1.0},
            slippage_pips={"EURUSD": 0.1},
            fixed_fee_usd=-1.0,
        )
    )

    try:
        validate_backtest_inputs(data, config).raise_for_errors()
    except ProductValidationError as error:
        assert "fixed_fee_usd must be >= 0" in str(error)
    else:
        raise AssertionError("Expected negative execution fees to be rejected")


def test_take_profit_requires_more_than_touch_and_stop_has_priority() -> None:
    index = pd.date_range("2024-01-01 00:00:00", periods=2, freq="h")
    data = {
        "EURUSD": pd.DataFrame(
            {
                "open": [1.0, 1.0],
                "high": [1.0, 1.0202],
                "low": [1.0, 0.89],
                "close": [1.0, 1.0],
                "spread": [1.0, 1.0],
            },
            index=index,
        )
    }
    config = BacktestConfig(
        initial_cash=100_000,
        risk=RiskConfig(risk_per_trade_pct=0.005, max_leverage=2),
        execution=ExecutionConfig(
            spread_pips={"EURUSD": 1.0},
            slippage_pips={"EURUSD": 1.0},
            commission_per_million_usd=0.0,
        ),
    )

    result = BacktestEngine(TakeProfitStrategy(), config).run(data)

    assert result.trades.iloc[0]["exit_reason"] == "stop_loss"


def test_take_profit_touch_alone_does_not_fill_limit() -> None:
    index = pd.date_range("2024-01-01 00:00:00", periods=2, freq="h")
    spread_pips = 1.0
    slippage_pips = 1.0
    fill_price = 1.0 + (spread_pips / 2 + slippage_pips) * 0.0001
    take_profit_price = fill_price + 0.01
    data = {
        "EURUSD": pd.DataFrame(
            {
                "open": [1.0, 1.0],
                "high": [1.0, take_profit_price],
                "low": [1.0, 0.99],
                "close": [1.0, 1.0],
                "spread": [spread_pips, spread_pips],
            },
            index=index,
        )
    }
    config = BacktestConfig(
        initial_cash=100_000,
        risk=RiskConfig(risk_per_trade_pct=0.005, max_leverage=2),
        execution=ExecutionConfig(
            spread_pips={"EURUSD": spread_pips},
            slippage_pips={"EURUSD": slippage_pips},
            commission_per_million_usd=0.0,
        ),
    )

    result = BacktestEngine(TakeProfitStrategy(), config).run(data)

    assert result.trades.iloc[0]["exit_reason"] == "end_of_backtest"


def test_empty_trade_log_has_headers_and_audit_does_not_crash(tmp_path) -> None:
    output_dir = tmp_path / "blocked"

    exit_code = main(
        [
            "backtest",
            "--data",
            "examples/sample_prices.csv",
            "--events",
            "examples/sample_events.csv",
            "--strategy",
            "ma_cross",
            "--blocked-weekday",
            "mon,tue,wed,thu,fri,sat,sun",
            "--output-dir",
            str(output_dir),
            "--expected-frequency",
            "h",
        ]
    )

    assert exit_code == 0
    trade_log = pd.read_csv(output_dir / "trade_log.csv")
    assert trade_log.empty
    assert "signal_time" in trade_log.columns
    assert "exit_reason" in trade_log.columns
    assert main(["audit-run", "--run-dir", str(output_dir)]) == 0
    assert main(["analyze-run", "--run-dir", str(output_dir), "--monte-carlo-paths", "10"]) == 0
    assert (output_dir / "commercial_readiness.json").exists()


def test_stop_loss_gap_fills_at_worse_open_not_stop_price() -> None:
    index = pd.date_range("2024-01-01 00:00:00", periods=3, freq="h")
    data = {
        "EURUSD": pd.DataFrame(
            {
                "open": [1.00, 1.00, 0.80],
                "high": [1.02, 1.02, 0.82],
                "low": [0.99, 0.99, 0.79],
                "close": [1.00, 1.00, 0.81],
                "spread": [1.0, 1.0, 1.0],
            },
            index=index,
        )
    }
    config = BacktestConfig(
        initial_cash=100_000,
        risk=RiskConfig(risk_per_trade_pct=0.005, max_leverage=2),
        execution=ExecutionConfig(
            spread_pips={"EURUSD": 1.0},
            slippage_pips={"EURUSD": 1.0},
            commission_per_million_usd=0.0,
        ),
    )

    result = BacktestEngine(
        TakeProfitStrategy(stop_distance=0.05, take_profit_distance=0.5), config
    ).run(data)
    trade = result.trades.iloc[0]

    assert trade["exit_reason"] == "stop_loss"
    assert float(trade["exit_expected_price"]) == 0.80
    assert round(float(trade["exit_fill_price"]), 5) == 0.79985


def test_cross_jpy_uses_usdjpy_conversion_data() -> None:
    index = pd.date_range("2024-01-01 00:00:00", periods=3, freq="h")
    data = {
        "EURJPY": pd.DataFrame(
            {
                "open": [160.0, 161.0, 162.0],
                "high": [161.0, 162.0, 163.0],
                "low": [159.0, 160.0, 161.0],
                "close": [160.5, 161.5, 162.5],
                "spread": [1.5, 1.5, 1.5],
            },
            index=index,
        ),
        "USDJPY": pd.DataFrame(
            {
                "open": [150.0, 150.0, 150.0],
                "high": [151.0, 151.0, 151.0],
                "low": [149.0, 149.0, 149.0],
                "close": [150.0, 150.0, 150.0],
                "spread": [1.0, 1.0, 1.0],
            },
            index=index,
        ),
    }
    config = BacktestConfig(
        initial_cash=100_000,
        risk=RiskConfig(risk_per_trade_pct=0.005, max_leverage=2),
        execution=ExecutionConfig(
            spread_pips={"EURJPY": 1.5, "USDJPY": 1.0},
            slippage_pips={"EURJPY": 0.5, "USDJPY": 0.25},
            commission_per_million_usd=0.0,
        ),
    )

    result = BacktestEngine(SymbolOnlyLongStrategy("EURJPY"), config).run(data)

    assert not result.trades.empty
    assert result.metrics["trade_count"] == 1


def test_reversal_opens_opposite_side_on_same_next_bar_as_close() -> None:
    index = pd.date_range("2024-01-01 00:00:00", periods=5, freq="h")
    data = {
        "EURUSD": pd.DataFrame(
            {
                "open": [1.00, 1.01, 1.02, 1.03, 1.04],
                "high": [1.02, 1.03, 1.04, 1.05, 1.06],
                "low": [0.99, 1.00, 1.01, 1.02, 1.03],
                "close": [1.01, 1.02, 1.03, 1.04, 1.05],
                "spread": [1.0] * 5,
            },
            index=index,
        )
    }
    config = BacktestConfig(
        initial_cash=100_000,
        risk=RiskConfig(risk_per_trade_pct=0.005, max_leverage=2),
        execution=ExecutionConfig(
            spread_pips={"EURUSD": 1.0},
            slippage_pips={"EURUSD": 0.5},
            commission_per_million_usd=0.0,
        ),
    )

    result = BacktestEngine(ReversalStrategy(), config).run(data)

    assert len(result.trades) >= 2
    assert result.trades.iloc[0]["exit_time"] == index[3]
    assert result.trades.iloc[1]["fill_time"] == index[3]


def test_filtered_strategy_blocks_entries_without_forcing_exit_signal() -> None:
    index = pd.date_range("2024-01-01 00:00:00", periods=2, freq="h")
    data = pd.DataFrame(
        {
            "open": [1.0, 1.0],
            "high": [1.01, 1.01],
            "low": [0.99, 0.99],
            "close": [1.0, 1.0],
            "spread": [1.0, 1.0],
        },
        index=index,
    )
    strategy = FilteredStrategy(
        AlwaysLongStrategy(),
        RegimeFilterConfig(enabled=False),
        NoTradeFilterConfig(enabled=True, blocked_entry_hours=(0,), max_spread_multiple=10),
    )

    signals = strategy.generate("EURUSD", data)

    assert bool(signals.iloc[0]["entry_allowed"]) is False
    assert bool(signals.iloc[0]["target_position"] == 1)
    assert bool(signals.iloc[1]["entry_allowed"]) is True


def test_ai_logistic_strategy_generates_model_diagnostics() -> None:
    data = _ai_price_frame()
    strategy = AILogisticStrategy(
        min_train_bars=40,
        retrain_interval=8,
        epochs=25,
        fast_window=6,
        slow_window=18,
        momentum_window=6,
        volatility_window=8,
        rsi_window=6,
        atr_window=6,
        long_threshold=0.52,
        short_threshold=0.48,
    )

    signals = strategy.generate("EURUSD", data)

    assert {"ai_probability_up", "ai_edge", "ai_train_rows", "ai_model_ready"}.issubset(
        signals.columns
    )
    assert bool(signals["ai_model_ready"].any()) is True
    assert set(signals["target_position"].unique()).issubset({-1, 0, 1})


def test_ai_logistic_strategy_does_not_change_past_signals_when_future_changes() -> None:
    data = _ai_price_frame(160)
    modified = data.copy()
    mutation_start = 120
    modified.iloc[mutation_start:, modified.columns.get_loc("close")] *= 1.15
    modified.iloc[mutation_start:, modified.columns.get_loc("high")] *= 1.15
    modified.iloc[mutation_start:, modified.columns.get_loc("low")] *= 1.15
    modified.iloc[mutation_start:, modified.columns.get_loc("open")] *= 1.15
    strategy = AILogisticStrategy(
        min_train_bars=35,
        retrain_interval=5,
        epochs=20,
        fast_window=5,
        slow_window=16,
        momentum_window=5,
        volatility_window=8,
        rsi_window=6,
        atr_window=6,
        long_threshold=0.52,
        short_threshold=0.48,
    )

    original_signals = strategy.generate("EURUSD", data)
    modified_signals = strategy.generate("EURUSD", modified)

    pd.testing.assert_series_equal(
        original_signals["target_position"].iloc[:mutation_start],
        modified_signals["target_position"].iloc[:mutation_start],
    )


def test_ai_logistic_strategy_runs_from_cli(tmp_path) -> None:
    metrics_path = tmp_path / "ai_metrics.json"

    exit_code = main(
        [
            "backtest",
            "--data",
            "examples/sample_prices.csv",
            "--events",
            "examples/sample_events.csv",
            "--strategy",
            "ai_logistic",
            "--param",
            "min_train_bars=40",
            "--param",
            "epochs=20",
            "--param",
            "fast_window=6",
            "--param",
            "slow_window=18",
            "--param",
            "momentum_window=6",
            "--param",
            "volatility_window=8",
            "--param",
            "rsi_window=6",
            "--param",
            "atr_window=6",
            "--disable-regime-filter",
            "--disable-signal-no-trade-filter",
            "--output-metrics",
            str(metrics_path),
        ]
    )

    assert exit_code == 0
    metrics = json.loads(metrics_path.read_text(encoding="utf-8"))
    assert "trade_count" in metrics


def test_cli_append_options_override_default_lists() -> None:
    parser = _add_common_arguments(argparse.ArgumentParser())
    args = parser.parse_args(
        [
            "--data",
            "examples/sample_prices.csv",
            "--strategy",
            "ma_cross",
            "--spread-time-multiplier",
            "12=3.0",
            "--slippage-time-multiplier",
            "12=4.0",
            "--blocked-entry-hour",
            "10",
        ]
    )

    config = _build_config(args)
    strategy = _build_strategy_from_args(args, {})

    assert config.execution.spread_time_multipliers == {12: 3.0}
    assert config.execution.slippage_time_multipliers == {12: 4.0}
    assert isinstance(strategy, FilteredStrategy)
    assert strategy.no_trade.blocked_entry_hours == (10,)


def test_tradingview_json_alert_is_normalized_and_written(tmp_path) -> None:
    body = json.dumps(
        {
            "secret": "shared",
            "exchange": "OANDA",
            "ticker": "OANDA:EURUSD",
            "time": "2026-06-30T12:00:00Z",
            "interval": "60",
            "action": "buy",
            "price": "1.075",
            "contracts": "10000",
            "order_id": "ma-cross-long",
        }
    ).encode("utf-8")

    alert = parse_tradingview_alert(
        body,
        "application/json",
        secret="shared",
        received_at_utc="2026-06-30T12:00:01+00:00",
    )
    output = append_tradingview_alert(tmp_path / "alerts.jsonl", alert)

    assert alert["source"] == "tradingview"
    assert alert["symbol"] == "EURUSD"
    assert alert["side"] == "buy"
    assert alert["price"] == 1.075
    assert alert["quantity"] == 10000.0
    assert "secret" not in alert["raw"]
    stored = json.loads(output.read_text(encoding="utf-8"))
    assert stored["order_id"] == "ma-cross-long"


def test_tradingview_alert_rejects_wrong_secret() -> None:
    body = json.dumps({"secret": "wrong", "ticker": "EURUSD"}).encode("utf-8")

    try:
        parse_tradingview_alert(body, "application/json", secret="shared")
    except PermissionError as error:
        assert "invalid TradingView webhook secret" in str(error)
    else:
        raise AssertionError("Expected wrong TradingView secret to be rejected")


def test_tradingview_text_alert_is_accepted() -> None:
    alert = parse_tradingview_alert(
        b"manual alert text",
        "text/plain",
        received_at_utc="2026-06-30T12:00:01+00:00",
    )

    assert alert["message"] == "manual alert text"
    assert alert["raw"] == {"message": "manual alert text"}


def test_webhook_secret_can_come_from_environment(monkeypatch) -> None:
    monkeypatch.setenv("TRADINGVIEW_WEBHOOK_SECRET", "shared")

    secret = _webhook_secret(
        argparse.Namespace(secret=None, secret_env="TRADINGVIEW_WEBHOOK_SECRET")
    )

    assert secret == "shared"


def test_time_varying_spread_and_slippage_multiplier() -> None:
    bar = pd.Series({"open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0})
    bar.name = pd.Timestamp("2024-01-01 21:00:00")
    execution = SimulatedExecution(
        ExecutionConfig(
            spread_pips={"EURUSD": 1.0},
            slippage_pips={"EURUSD": 1.0},
            spread_time_multipliers={21: 2.0},
            slippage_time_multipliers={21: 2.0},
            commission_per_million_usd=0.0,
        )
    )

    fill = execution.execute_market("EURUSD", 1.0, 1, 1000, bar)

    assert fill.spread_pips == 2.0
    assert fill.slippage_pips == 2.0
    assert round(fill.price, 5) == 1.00030


def test_currency_exposure_cap_blocks_second_correlated_position() -> None:
    index = pd.date_range("2024-01-01 00:00:00", periods=3, freq="h")
    frame = pd.DataFrame(
        {
            "open": [1.0, 1.0, 1.0],
            "high": [1.005, 1.005, 1.005],
            "low": [0.995, 0.995, 0.995],
            "close": [1.0, 1.0, 1.0],
            "spread": [1.0, 1.0, 1.0],
        },
        index=index,
    )
    data = {"EURUSD": frame.copy(), "GBPUSD": frame.copy()}
    config = BacktestConfig(
        initial_cash=100_000,
        risk=RiskConfig(
            risk_per_trade_pct=0.005,
            risk_cap_pct=0.005,
            max_leverage=10,
            max_currency_exposure_pct=0.75,
        ),
        execution=ExecutionConfig(
            spread_pips={"EURUSD": 1.0, "GBPUSD": 1.0},
            slippage_pips={"EURUSD": 0.1, "GBPUSD": 0.1},
            commission_per_million_usd=0.0,
        ),
    )

    result = BacktestEngine(AlwaysLongStrategy(), config).run(data)

    assert result.trades["symbol"].nunique() == 1


def test_analyze_run_writes_commercial_validation_pack(tmp_path) -> None:
    output_dir = tmp_path / "candidate"

    assert (
        main(
            [
                "backtest",
                "--data",
                "examples/sample_prices.csv",
                "--events",
                "examples/sample_events.csv",
                "--strategy",
                "ma_cross",
                "--output-dir",
                str(output_dir),
                "--expected-frequency",
                "h",
            ]
        )
        == 0
    )
    assert (
        main(
            [
                "analyze-run",
                "--run-dir",
                str(output_dir),
                "--monte-carlo-paths",
                "1000",
                "--min-period-days",
                "1",
                "--min-oos-trades",
                "1",
            ]
        )
        == 0
    )

    expected_files = [
        "index.html",
        "commercial_readiness.json",
        "pair_performance.csv",
        "monthly_pnl.csv",
        "monthly_target.csv",
        "drawdown_periods.csv",
        "period_performance.csv",
        "oos_summary.json",
        "cost_sensitivity.csv",
        "pnl_breakdown.csv",
        "pnl_by_side.csv",
        "pnl_by_hour.csv",
        "pnl_by_pair.csv",
        "pnl_by_strategy.csv",
        "pnl_breakdown_summary.json",
        "usable_segments.csv",
        "strategy_diagnosis.json",
        "baseline_comparison.csv",
        "paper_backtest_diff.json",
        "lot_control_summary.json",
        "monte_carlo_summary.json",
        "monte_carlo_quantiles.csv",
        "forward_test_summary.json",
    ]
    for filename in expected_files:
        assert (output_dir / filename).exists()

    readiness = json.loads((output_dir / "commercial_readiness.json").read_text(encoding="utf-8"))
    assert readiness["commercial_ready"] is False
    assert any(
        gate["name"] == "forward_test" and gate["passed"] is False for gate in readiness["gates"]
    )
    assert any(gate["name"] == "monthly_return_target" for gate in readiness["gates"])
    assert not pd.read_csv(output_dir / "pair_performance.csv").empty
    pnl = json.loads((output_dir / "pnl_breakdown_summary.json").read_text(encoding="utf-8"))
    reconstructed = (
        pnl["pre_cost_pnl"]
        + pnl["spread_loss"]
        + pnl["slippage_loss"]
        + pnl["commission"]
        + pnl["swap"]
    )
    assert round(reconstructed, 6) == round(pnl["total_net_pnl"], 6)
    diagnosis = json.loads((output_dir / "strategy_diagnosis.json").read_text(encoding="utf-8"))
    assert "primary_cause" in diagnosis


def test_monthly_target_summary_flags_shortfall() -> None:
    monthly = pd.DataFrame(
        [
            {
                "month": "2025-01",
                "return_pct": 0.10,
                "max_drawdown_pct": 0.01,
                "trade_count": 3,
                "net_pnl": 10_000,
            },
            {
                "month": "2025-02",
                "return_pct": 0.03,
                "max_drawdown_pct": 0.02,
                "trade_count": 2,
                "net_pnl": 3_000,
            },
        ]
    )

    summary = monthly_target_summary(monthly, 0.08)

    assert summary["target_met"].tolist() == [True, False]
    assert round(float(summary.iloc[1]["shortfall_pct"]), 4) == 0.05


def test_walk_forward_limits_parameter_grid() -> None:
    index = pd.date_range("2024-01-01", periods=20, freq="h")
    data = {
        "EURUSD": pd.DataFrame(
            {
                "open": [1.0] * 20,
                "high": [1.01] * 20,
                "low": [0.99] * 20,
                "close": [1.0] * 20,
            },
            index=index,
        )
    }

    def engine_factory(strategy: Strategy) -> BacktestEngine:
        return BacktestEngine(strategy, BacktestConfig(execution=_zero_cost_config()))

    validator = WalkForwardValidator(
        AlternatingStrategy,
        {"stop_distance": [0.005, 0.01, 0.02]},
        engine_factory,
        WalkForwardConfig(train_bars=10, test_bars=5, max_parameter_combinations=2),
    )

    try:
        validator.run(data)
    except ValueError as error:
        assert "Parameter grid is too large" in str(error)
    else:
        raise AssertionError("Expected ValueError for oversized grid")


def test_walk_forward_purge_and_embargo_gap() -> None:
    index = pd.date_range("2024-01-01", periods=20, freq="h")
    data = {
        "EURUSD": pd.DataFrame(
            {
                "open": [1.0] * 20,
                "high": [1.01] * 20,
                "low": [0.99] * 20,
                "close": [1.0] * 20,
            },
            index=index,
        )
    }

    def engine_factory(strategy: Strategy) -> BacktestEngine:
        return BacktestEngine(strategy, BacktestConfig(execution=_zero_cost_config()))

    validator = WalkForwardValidator(
        AlternatingStrategy,
        {"stop_distance": [0.01]},
        engine_factory,
        WalkForwardConfig(train_bars=10, test_bars=5, purge_bars=2, embargo_bars=1),
    )

    summary = validator.run(data).summary()

    assert summary.iloc[0]["train_end"] == index[7]
    assert summary.iloc[0]["test_start"] == index[11]
    assert int(summary.iloc[0]["purge_bars"]) == 2
    assert int(summary.iloc[0]["embargo_bars"]) == 1


def test_research_pack_command_writes_artifacts(tmp_path) -> None:
    output_dir = tmp_path / "research"

    exit_code = main(["research-pack", "--output-dir", str(output_dir)])

    assert exit_code == 0
    assert (output_dir / "public_fx_sources.csv").exists()
    assert (output_dir / "major_fx_events.csv").exists()
    assert (output_dir / "research_max_config.json").exists()
    assert (output_dir / "deep_research_max_config.json").exists()
    assert (output_dir / "deep_research_decisions.csv").exists()
    assert (output_dir / "research_notes.md").exists()


def test_research_max_preset_runs_with_sample_data(tmp_path) -> None:
    output_dir = tmp_path / "research"
    metrics_path = tmp_path / "metrics.json"
    main(["research-pack", "--output-dir", str(output_dir)])

    exit_code = main(
        [
            "backtest",
            "--data",
            "examples/sample_prices.csv",
            "--events",
            str(output_dir / "major_fx_events.csv"),
            "--strategy",
            "ma_cross",
            "--preset",
            "research-max",
            "--output-metrics",
            str(metrics_path),
        ]
    )

    assert exit_code == 0
    assert metrics_path.exists()


def test_deep_research_max_preset_runs_with_sample_data(tmp_path) -> None:
    output_dir = tmp_path / "research"
    metrics_path = tmp_path / "metrics.json"
    main(["research-pack", "--output-dir", str(output_dir)])

    exit_code = main(
        [
            "backtest",
            "--data",
            "examples/sample_prices.csv",
            "--events",
            str(output_dir / "major_fx_events.csv"),
            "--strategy",
            "ma_cross",
            "--preset",
            "deep-research-max",
            "--output-metrics",
            str(metrics_path),
        ]
    )

    assert exit_code == 0
    assert metrics_path.exists()


def test_output_dir_writes_auditable_artifacts(tmp_path) -> None:
    output_dir = tmp_path / "run"

    exit_code = main(
        [
            "backtest",
            "--data",
            "examples/sample_prices.csv",
            "--events",
            "examples/sample_events.csv",
            "--strategy",
            "ma_cross",
            "--output-dir",
            str(output_dir),
            "--expected-frequency",
            "h",
        ]
    )

    assert exit_code == 0
    assert (output_dir / "manifest.json").exists()
    assert (output_dir / "config.json").exists()
    assert (output_dir / "data_qa.csv").exists()
    assert (output_dir / "trade_log.csv").exists()
    assert (output_dir / "equity_curve.csv").exists()
    assert (output_dir / "metrics.json").exists()
    assert main(["audit-run", "--run-dir", str(output_dir)]) == 0


def test_qa_data_command_writes_report(tmp_path) -> None:
    output_path = tmp_path / "qa.csv"

    exit_code = main(
        [
            "qa-data",
            "--data",
            "examples/sample_prices.csv",
            "--expected-frequency",
            "h",
            "--output",
            str(output_path),
        ]
    )

    assert exit_code == 0
    assert output_path.exists()


def test_weekly_loss_stop_locks_risk() -> None:
    index = pd.date_range("2024-01-01 00:00:00", periods=8, freq="h")
    data = {
        "EURUSD": pd.DataFrame(
            {
                "open": [1.00] * 8,
                "high": [1.01] * 8,
                "low": [1.00, 0.99, 1.00, 0.99, 1.00, 0.99, 1.00, 1.00],
                "close": [1.00] * 8,
            },
            index=index,
        )
    }
    config = BacktestConfig(
        initial_cash=100_000,
        risk=RiskConfig(
            risk_per_trade_pct=0.01,
            max_daily_loss_pct=0.99,
            max_weekly_loss_pct=0.02,
            max_leverage=10,
        ),
        execution=_zero_cost_config(),
    )

    result = BacktestEngine(AlwaysLongStrategy(), config).run(data)

    assert bool(result.equity_curve.iloc[-1]["weekly_locked"]) is True
    assert bool(result.equity_curve.iloc[-1]["risk_locked"]) is True


def test_monthly_profit_target_locks_risk_and_closes_position() -> None:
    index = pd.date_range("2024-01-01 00:00:00", periods=4, freq="h")
    data = {
        "EURUSD": pd.DataFrame(
            {
                "open": [1.00, 1.00, 1.10, 1.10],
                "high": [1.01, 1.12, 1.12, 1.12],
                "low": [0.99, 1.00, 1.08, 1.08],
                "close": [1.00, 1.10, 1.10, 1.10],
            },
            index=index,
        )
    }
    config = BacktestConfig(
        initial_cash=100_000,
        risk=RiskConfig(
            risk_per_trade_pct=0.01,
            max_daily_loss_pct=0.99,
            monthly_profit_target_pct=0.01,
            max_leverage=10,
        ),
        execution=_zero_cost_config(),
    )

    result = BacktestEngine(WideStopLongStrategy(), config).run(data)

    assert result.trades.iloc[0]["exit_reason"] == "monthly_profit_target"
    assert bool(result.equity_curve["monthly_profit_locked"].any()) is True
    assert bool(result.equity_curve.iloc[-1]["risk_locked"]) is True
