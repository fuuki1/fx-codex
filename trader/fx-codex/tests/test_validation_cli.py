from __future__ import annotations

import json

import pytest
from fx_backtester import validation
from fx_backtester.cli import main
from fx_backtester.costs import CostModel


def test_expand_grid_and_valid_params():
    grid = {"fast_window": [10, 20], "slow_window": [40, 60]}
    combos = validation.expand_grid(grid)
    assert len(combos) == 4
    assert validation.valid_params("ma_cross", {"fast_window": 10, "slow_window": 40})
    # fast >= slow は不正
    assert not validation.valid_params("ma_cross", {"fast_window": 60, "slow_window": 40})


def test_walk_forward_structure(sample_df):
    grid = {
        "fast_window": [10, 20],
        "slow_window": [40, 60],
        "atr_window": [14],
        "stop_atr_multiple": [2.0],
    }
    cost = CostModel("USDJPY", spread_pips=0.3, slippage_pips=0.1)
    result = validation.optimize(sample_df, "ma_cross", grid, cost, train=252, test=63)
    # 配備用パラメータ + 検証サマリが揃う
    for k in ("fast_window", "slow_window", "atr_window", "stop_atr_multiple", "atr_multiple"):
        assert k in result
    v = result["_validation"]
    assert v["n_folds"] > 0
    assert isinstance(v["overfit_warning"], bool)
    assert isinstance(v["param_stability"], float)


def test_walk_forward_needs_enough_data(sample_df):
    grid = {"fast_window": [10], "slow_window": [40], "atr_window": [14], "stop_atr_multiple": [2.0]}
    cost = CostModel("USDJPY")
    with pytest.raises(ValueError):
        validation.walk_forward(sample_df, "ma_cross", grid, cost, train=100000, test=100)


def test_cli_backtest_outputs_compatible_metrics(capsys, sample_prices_path):
    rc = main(
        [
            "backtest", "--data", sample_prices_path, "--strategy", "ma_cross",
            "--param", "fast_window=10", "--param", "slow_window=30",
            "--param", "atr_window=14", "--param", "stop_atr_multiple=2.0",
            "--spread-pips", "USDJPY=0.3", "--slippage-pips", "USDJPY=0.1",
        ]
    )
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    for k in ("sharpe_ratio", "profit_factor", "max_drawdown_pct"):
        assert k in out
