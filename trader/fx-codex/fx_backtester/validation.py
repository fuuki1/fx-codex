"""ウォークフォワード / アウトオブサンプル(OOS)検証。

過剰最適化(オーバーフィット)対策の中核。時系列を「学習(in-sample)→検証(out-of-sample)」の
窓で前進させ、各窓で in-sample 最良パラメータを選び、未学習の OOS で評価する。
採否は OOS 成績・パラメータ安定性・取引数の十分性で判断する。
"""
from __future__ import annotations

import math
from collections import Counter
from itertools import product
from statistics import mean
from typing import Any

import pandas as pd

from . import data as data_mod
from . import registry
from .costs import CostModel
from .engine import BacktestResult
from .metrics import compute_metrics
from .runner import run_backtest, run_result


def expand_grid(grid: dict[str, list[Any]]) -> list[dict[str, Any]]:
    keys = list(grid)
    return [dict(zip(keys, combo, strict=True)) for combo in product(*[grid[k] for k in keys])]


def valid_params(strategy_name: str, params: dict[str, Any]) -> bool:
    try:
        registry.create(strategy_name, params)
        return True
    except (ValueError, TypeError):
        return False


# フォールド内のパラメータ選択で「最低限これだけは取引していてほしい」本数。
# 統計的有意性の最終判断は集計側の min_trades（OOS 合計）で行う。
SELECT_TRADE_FLOOR = 5


def selection_score(m: dict[str, float], floor: int = SELECT_TRADE_FLOOR) -> float:
    """パラメータ選択用スコア。取引数が少なすぎる候補は強く減点（ノイズ採用を防ぐ）。"""
    if m["num_trades"] < floor:
        return -999.0 + m["num_trades"]
    return m["sharpe_ratio"] - 0.5 * (m["max_drawdown_pct"] / 100.0)


def _metrics_on_range(
    result: BacktestResult, start_ts: pd.Timestamp, end_ts: pd.Timestamp, ppy: float
) -> dict[str, float]:
    r = result.bar_returns
    mask = (r.index >= start_ts) & (r.index <= end_ts)
    sub_r = r[mask]
    sub_e = (1.0 + sub_r).cumprod()
    sub_e.name = "equity"
    sub_t = [t for t in result.trades if start_ts <= t.exit_ts <= end_ts]
    return compute_metrics(BacktestResult(sub_r, sub_e, sub_t), ppy)


def walk_forward(
    df: pd.DataFrame,
    strategy_name: str,
    grid: dict[str, list[Any]],
    cost: CostModel,
    events: pd.DataFrame | None = None,
    *,
    train: int = 252,
    test: int = 63,
    min_trades: int = 20,
) -> dict[str, Any]:
    n = len(df)
    ppy = data_mod.infer_periods_per_year(df.index)
    combos = [p for p in expand_grid(grid) if valid_params(strategy_name, p)]
    if not combos:
        raise ValueError("grid produced no valid parameter combinations")

    folds: list[dict[str, Any]] = []
    start = 0
    while start + train + test <= n:
        train_df = df.iloc[start : start + train]
        te0, te1 = start + train, start + train + test

        # in-sample 最良を選ぶ
        best_p, best_s, best_m = None, -math.inf, None
        for p in combos:
            m, _ = run_backtest(train_df, strategy_name, p, cost, events)
            s = selection_score(m)
            if s > best_s:
                best_s, best_p, best_m = s, p, m
        assert best_p is not None and best_m is not None

        # OOS 評価（指標の暖機のため warmup を前置し、テスト区間のみ集計）
        warm = int(best_p.get("slow_window", 60)) + int(best_p.get("atr_window", 14)) + 2
        ext = df.iloc[max(te0 - warm, 0) : te1]
        res = run_result(ext, strategy_name, best_p, cost, events)
        oos = _metrics_on_range(res, df.index[te0], df.index[te1 - 1], ppy)

        folds.append({"train": [start, start + train], "test": [te0, te1],
                      "params": best_p, "is": best_m, "oos": oos})
        start += test

    return _aggregate(folds, strategy_name, df, grid, cost, events, min_trades, ppy)


def _aggregate(
    folds: list[dict[str, Any]],
    strategy_name: str,
    df: pd.DataFrame,
    grid: dict[str, list[Any]],
    cost: CostModel,
    events: pd.DataFrame | None,
    min_trades: int,
    ppy: float,
) -> dict[str, Any]:
    if not folds:
        raise ValueError("not enough data for any walk-forward fold (increase data or reduce train/test)")

    oos_sharpe = [f["oos"]["sharpe_ratio"] for f in folds]
    is_sharpe = [f["is"]["sharpe_ratio"] for f in folds]
    mean_oos = mean(oos_sharpe)
    mean_is = mean(is_sharpe)

    # パラメータ安定性: 最頻のパラメータ組とその一致率
    ctr = Counter(tuple(sorted(f["params"].items())) for f in folds)
    top_items, top_count = ctr.most_common(1)[0]
    recommended = dict(top_items)
    stability = top_count / len(folds)

    # 全期間で推奨パラメータの最終バックテストも添える（参考）
    full_metrics, _ = run_backtest(df, strategy_name, recommended, cost, events)

    overfit_warning = bool((mean_is > 0 and mean_oos < 0.5 * mean_is) or mean_oos <= 0)
    insufficient_trades = bool(sum(f["oos"]["num_trades"] for f in folds) < min_trades)

    return {
        "strategy": strategy_name,
        "grid": grid,
        "n_folds": len(folds),
        "oos_sharpe_mean": round(mean_oos, 4),
        "is_sharpe_mean": round(mean_is, 4),
        "oos_is_ratio": round(mean_oos / mean_is, 4) if mean_is > 0 else 0.0,
        "oos_max_drawdown_mean": round(mean(f["oos"]["max_drawdown_pct"] for f in folds), 4),
        "oos_profit_factor_mean": round(mean(f["oos"]["profit_factor"] for f in folds), 4),
        "oos_total_trades": sum(f["oos"]["num_trades"] for f in folds),
        "param_stability": round(stability, 4),
        "recommended_params": recommended,
        "full_period_metrics": full_metrics,
        "overfit_warning": overfit_warning,
        "insufficient_trades": insufficient_trades,
        "folds": folds,
    }


def optimize(
    df: pd.DataFrame,
    strategy_name: str,
    grid: dict[str, list[Any]],
    cost: CostModel,
    events: pd.DataFrame | None = None,
    *,
    train: int = 252,
    test: int = 63,
    min_trades: int = 20,
) -> dict[str, Any]:
    """OOS 検証で選んだ「配備用」パラメータと検証サマリを返す。"""
    report = walk_forward(
        df, strategy_name, grid, cost, events, train=train, test=test, min_trades=min_trades
    )
    params = dict(report["recommended_params"])
    # ライブ strategy.py 互換キー（atr_multiple）も付与
    if "stop_atr_multiple" in params and "atr_multiple" not in params:
        params["atr_multiple"] = params["stop_atr_multiple"]
    return {
        **params,
        "_validation": {
            "method": "walk_forward",
            "n_folds": report["n_folds"],
            "oos_sharpe_mean": report["oos_sharpe_mean"],
            "oos_is_ratio": report["oos_is_ratio"],
            "oos_max_drawdown_mean": report["oos_max_drawdown_mean"],
            "oos_total_trades": report["oos_total_trades"],
            "param_stability": report["param_stability"],
            "overfit_warning": report["overfit_warning"],
            "insufficient_trades": report["insufficient_trades"],
        },
    }
