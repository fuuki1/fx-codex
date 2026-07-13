from __future__ import annotations

from datetime import UTC, datetime

import pandas as pd
import pytest

from fx_backtester.execution import ExecutionConfig, SimulatedExecution
from fx_backtester.risk import RiskConfig, RiskManager

RISK_NUMERIC_FIELDS = (
    "risk_per_trade_pct",
    "risk_cap_pct",
    "max_daily_loss_pct",
    "max_weekly_loss_pct",
    "max_monthly_drawdown_pct",
    "monthly_profit_target_pct",
    "hard_drawdown_pct",
    "min_stop_pips",
    "max_leverage",
    "max_currency_exposure_pct",
    "max_position_units",
)


@pytest.mark.parametrize("field", RISK_NUMERIC_FIELDS)
@pytest.mark.parametrize("invalid", [True, float("nan"), float("inf"), 1e308])
def test_risk_config_rejects_boolean_nonfinite_and_unbounded_values(
    field: str,
    invalid: object,
) -> None:
    with pytest.raises(ValueError, match=field):
        RiskConfig(**{field: invalid})  # type: ignore[arg-type]


def test_risk_config_requires_a_real_boolean_flag() -> None:
    with pytest.raises(ValueError, match="allow_fractional_units"):
        RiskConfig(allow_fractional_units=1)  # type: ignore[arg-type]


@pytest.mark.parametrize("field", ["spread_pips", "slippage_pips"])
@pytest.mark.parametrize("invalid", [True, float("nan"), float("inf"), 1e308])
def test_execution_config_rejects_invalid_symbol_costs(field: str, invalid: object) -> None:
    kwargs = {field: {"EURUSD": invalid}}
    with pytest.raises(ValueError, match=field):
        ExecutionConfig(**kwargs)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "field",
    ["commission_per_million_usd", "fixed_fee_usd", "minimum_fee_usd"],
)
@pytest.mark.parametrize("invalid", [True, float("nan"), float("inf"), 1e308])
def test_execution_config_rejects_invalid_fees(field: str, invalid: object) -> None:
    with pytest.raises(ValueError, match=field):
        ExecutionConfig(**{field: invalid})  # type: ignore[arg-type]


def test_execution_config_rejects_boolean_hour_and_nonfinite_multiplier() -> None:
    with pytest.raises(ValueError, match="integer UTC hours"):
        ExecutionConfig(spread_time_multipliers={True: 2.0})  # type: ignore[dict-item]
    with pytest.raises(ValueError, match="slippage_time_multipliers"):
        ExecutionConfig(slippage_time_multipliers={21: float("nan")})


@pytest.mark.parametrize("invalid", [True, float("nan"), float("inf"), 1e308])
def test_risk_runtime_inputs_fail_closed(invalid: object) -> None:
    manager = RiskManager()
    now = datetime(2026, 7, 13, tzinfo=UTC)
    with pytest.raises(ValueError):
        manager.check_gross_leverage(invalid, 100_000.0)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        manager.position_size(
            "EURUSD",
            100_000.0,
            1.1,
            invalid,  # type: ignore[arg-type]
        )
    if not isinstance(invalid, bool):
        with pytest.raises(ValueError):
            manager.can_open(now, 100_000.0, invalid)  # type: ignore[arg-type]


def test_risk_runtime_requires_boolean_no_trade_window() -> None:
    with pytest.raises(ValueError, match="no_trade_window"):
        RiskManager().can_open(datetime(2026, 7, 13, tzinfo=UTC), 100_000.0, 1)  # type: ignore[arg-type]


@pytest.mark.parametrize("invalid", [True, float("nan"), float("inf"), 1e308])
def test_execution_runtime_inputs_fail_closed(invalid: object) -> None:
    execution = SimulatedExecution()
    with pytest.raises(ValueError):
        execution.fill_price("EURUSD", invalid, 1)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        execution.commission("EURUSD", invalid, 1.1)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        execution.execute_market("EURUSD", 1.1, 1, invalid)  # type: ignore[arg-type]


def test_execution_rejects_boolean_side_nonfinite_bar_and_bad_conversion() -> None:
    execution = SimulatedExecution()
    with pytest.raises(ValueError, match="side"):
        execution.fill_price("EURUSD", 1.1, True)  # type: ignore[arg-type]

    bar = pd.Series({"spread_pips": float("inf")}, name=pd.Timestamp("2026-07-13T12:00:00Z"))
    with pytest.raises(ValueError, match="finite"):
        execution.spread_pips("EURUSD", bar)

    with pytest.raises(ValueError, match="conversion_rates"):
        execution.commission("EURJPY", 1_000.0, 170.0, {"USDJPY": True})  # type: ignore[dict-item]
