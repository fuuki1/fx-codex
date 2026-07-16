"""Execution-cost stress tests that re-run the engine instead of editing PnL."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from copy import deepcopy
from dataclasses import asdict, dataclass

import pandas as pd

from fx_backtester.engine import BacktestConfig, BacktestEngine
from fx_backtester.execution import ExecutionConfig
from fx_backtester.strategies.base import Strategy


@dataclass(frozen=True)
class CostStressScenario:
    name: str
    multiplier: float

    def __post_init__(self) -> None:
        if self.multiplier <= 0:
            raise ValueError("cost stress multiplier must be positive")


DEFAULT_COST_SCENARIOS: tuple[CostStressScenario, ...] = (
    CostStressScenario("observed", 1.0),
    CostStressScenario("cost_1_5x", 1.5),
    CostStressScenario("cost_2x", 2.0),
    CostStressScenario("cost_3x", 3.0),
)


def rerun_cost_stress(
    data: dict[str, pd.DataFrame],
    strategy_factory: Callable[[], Strategy],
    base_config: BacktestConfig,
    *,
    scenarios: Sequence[CostStressScenario] = DEFAULT_COST_SCENARIOS,
) -> pd.DataFrame:
    """Re-run sizing, fills, stops, and exits under each cost scenario."""

    if not scenarios:
        raise ValueError("at least one stress scenario is required")
    rows: list[dict[str, object]] = []
    for scenario in scenarios:
        config = deepcopy(base_config)
        config.execution = scale_execution_config(base_config.execution, scenario.multiplier)
        stressed_data = _scale_observed_spreads(data, scenario.multiplier)
        result = BacktestEngine(strategy_factory(), config).run(stressed_data)
        rows.append(
            {
                "scenario": scenario.name,
                "cost_multiplier": scenario.multiplier,
                "method": "full_engine_rerun",
                "trade_count": int(result.metrics["trade_count"]),
                "net_return": float(result.metrics["total_return_pct"]),
                "expectancy_r": float(result.metrics["expectancy_r"]),
                "sharpe": float(result.metrics["sharpe_ratio"]),
                "max_drawdown": float(result.metrics["max_drawdown_pct"]),
                "profit_factor": float(result.metrics["profit_factor"]),
                "final_equity": float(result.metrics["final_equity"]),
                "execution_config": asdict(config.execution),
            }
        )
    return pd.DataFrame(rows)


def scale_execution_config(config: ExecutionConfig, multiplier: float) -> ExecutionConfig:
    if multiplier <= 0:
        raise ValueError("cost multiplier must be positive")
    return ExecutionConfig(
        spread_pips={symbol: value * multiplier for symbol, value in config.spread_pips.items()},
        slippage_pips={
            symbol: value * multiplier for symbol, value in config.slippage_pips.items()
        },
        commission_per_million_usd=config.commission_per_million_usd * multiplier,
        fixed_fee_usd=config.fixed_fee_usd * multiplier,
        minimum_fee_usd=config.minimum_fee_usd * multiplier,
        spread_time_multipliers=dict(config.spread_time_multipliers),
        slippage_time_multipliers=dict(config.slippage_time_multipliers),
    )


def _scale_observed_spreads(
    data: dict[str, pd.DataFrame], multiplier: float
) -> dict[str, pd.DataFrame]:
    output: dict[str, pd.DataFrame] = {}
    for symbol, original in data.items():
        frame = original.copy()
        for column in ("spread", "spread_pips", "spread_price"):
            if column in frame:
                frame[column] = pd.to_numeric(frame[column], errors="raise") * multiplier
        output[symbol] = frame
    return output
