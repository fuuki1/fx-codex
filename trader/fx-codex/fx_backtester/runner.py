"""戦略 → エンジン → 指標 をつなぐ実行口。"""
from __future__ import annotations

from typing import Any

import pandas as pd

from . import data as data_mod
from . import registry
from .costs import CostModel
from .engine import BacktestResult, run
from .metrics import compute_metrics


def run_result(
    df: pd.DataFrame,
    strategy_name: str,
    params: dict[str, Any],
    cost: CostModel,
    events: pd.DataFrame | None = None,
) -> BacktestResult:
    strat = registry.create(strategy_name, params)
    signal = strat.generate(cost.symbol, df)
    blocked = data_mod.blocked_mask(df.index, events)
    return run(df, signal, cost, blocked)


def run_backtest(
    df: pd.DataFrame,
    strategy_name: str,
    params: dict[str, Any],
    cost: CostModel,
    events: pd.DataFrame | None = None,
) -> tuple[dict[str, float], BacktestResult]:
    result = run_result(df, strategy_name, params, cost, events)
    ppy = data_mod.infer_periods_per_year(df.index)
    return compute_metrics(result, ppy), result
