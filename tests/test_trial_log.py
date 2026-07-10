"""fx_backtester.trial_log（試行ログ基盤）のテスト。"""

from __future__ import annotations

import json

import pandas as pd
import pytest

from fx_backtester.engine import BacktestConfig, BacktestEngine
from fx_backtester.execution import ExecutionConfig
from fx_backtester.strategies.base import Strategy
from fx_backtester.trial_log import (
    TrialLogger,
    read_returns_matrix,
    read_trials,
)
from fx_backtester.walk_forward import WalkForwardConfig, WalkForwardValidator


def _returns(values: list[float], start: str = "2024-01-01") -> pd.Series:
    index = pd.date_range(start, periods=len(values), freq="h")
    return pd.Series(values, index=index)


def test_log_accumulates_and_rejects_duplicate_trial_id() -> None:
    logger = TrialLogger(run_id="testrun")
    logger.log(
        "t1",
        params={"fast_window": 10},
        phase="is_grid",
        metrics={"sharpe_ratio": 1.2},
        score=0.5,
    )
    assert logger.trial_count == 1
    with pytest.raises(ValueError):
        logger.log("t1", params={}, phase="is_grid", metrics={})


def test_mark_selected_flags_trial() -> None:
    logger = TrialLogger()
    logger.log("t1", params={}, phase="is_grid", metrics={})
    logger.log("t2", params={}, phase="is_grid", metrics={})
    logger.mark_selected("t2")
    assert logger.selected_trial_id == "t2"
    assert [t["selected"] for t in logger.trials] == [False, True]
    with pytest.raises(ValueError):
        logger.mark_selected("unknown")


def test_returns_matrix_aligns_on_union_index_and_dedupes_timestamps() -> None:
    logger = TrialLogger()
    logger.log("t1", params={}, phase="is_grid", metrics={}, returns=_returns([0.01, 0.02]))
    # 強制クローズで最終バーが二重になるケース: 重複時刻は後勝ち
    duplicated = pd.Series(
        [0.03, 0.04, 0.05],
        index=pd.DatetimeIndex(["2024-01-01 01:00", "2024-01-01 02:00", "2024-01-01 02:00"]),
    )
    logger.log("t2", params={}, phase="is_grid", metrics={}, returns=duplicated)

    matrix = logger.returns_matrix()
    assert list(matrix.columns) == ["t1", "t2"]
    assert len(matrix) == 3  # 00:00, 01:00, 02:00
    assert matrix.loc["2024-01-01 02:00", "t2"] == 0.05
    assert pd.isna(matrix.loc["2024-01-01 02:00", "t1"])


def test_write_and_read_back(tmp_path) -> None:
    logger = TrialLogger(run_id="r1", context={"generated_by": "test"})
    logger.log(
        "t1",
        params={"fast_window": 10, "slow_window": 40},
        phase="is_grid",
        metrics={"sharpe_ratio": 1.5, "profit_factor": float("inf")},
        score=float("-inf"),
        window={
            "kind": "IS",
            "start": pd.Timestamp("2024-01-01"),
            "end": pd.Timestamp("2024-02-01"),
        },
        returns=_returns([0.01, -0.02, 0.03]),
    )
    logger.mark_selected("t1")

    paths = logger.write(tmp_path)
    assert paths["run_dir"] == tmp_path / "r1"

    run_meta = json.loads(paths["run"].read_text(encoding="utf-8"))
    assert run_meta["trial_count"] == 1
    assert run_meta["selected_trial_id"] == "t1"
    assert run_meta["context"]["generated_by"] == "test"

    trials = read_trials(paths["trials"])
    assert len(trials) == 1
    trial = trials[0]
    assert trial["params"]["fast_window"] == 10
    assert trial["metrics"]["profit_factor"] is None  # inf はJSONに載せない
    assert trial["score"] is None  # -inf も同様
    assert trial["selected"] is True
    assert trial["window"]["kind"] == "IS"

    matrix = read_returns_matrix(paths["returns_matrix"])
    assert list(matrix.columns) == ["t1"]
    assert len(matrix) == 3
    assert matrix["t1"].iloc[1] == pytest.approx(-0.02)


def test_write_without_returns_skips_matrix(tmp_path) -> None:
    logger = TrialLogger(run_id="r2")
    logger.log("t1", params={}, phase="oos", metrics={"sharpe_ratio": 0.5})
    paths = logger.write(tmp_path)
    assert "returns_matrix" not in paths
    assert not (tmp_path / "r2" / "returns_matrix.csv").exists()


# ---------------------------------------------------------- walk-forward連携


class FixedStopLongStrategy(Strategy):
    def __init__(self, stop_distance: float = 0.01) -> None:
        self.stop_distance = stop_distance

    @property
    def name(self) -> str:
        return "fixed_stop_long"

    def generate(self, symbol: str, data: pd.DataFrame) -> pd.DataFrame:
        return self._validated_output(
            data,
            pd.DataFrame(
                {"target_position": 1, "stop_distance": self.stop_distance},
                index=data.index,
            ),
        )


def test_walk_forward_logs_all_train_trials_and_selected_tests() -> None:
    periods = 40
    index = pd.date_range("2024-01-01", periods=periods, freq="h")
    closes = [1.0 + 0.001 * i for i in range(periods)]
    data = {
        "EURUSD": pd.DataFrame(
            {
                "open": closes,
                "high": [c + 0.001 for c in closes],
                "low": [c - 0.001 for c in closes],
                "close": closes,
            },
            index=index,
        )
    }

    def engine_factory(strategy: Strategy) -> BacktestEngine:
        config = BacktestConfig(
            execution=ExecutionConfig(spread_pips={"EURUSD": 0.01}, slippage_pips={"EURUSD": 0.01})
        )
        return BacktestEngine(strategy, config)

    logger = TrialLogger()
    validator = WalkForwardValidator(
        FixedStopLongStrategy,
        {"stop_distance": [0.005, 0.01]},
        engine_factory,
        WalkForwardConfig(train_bars=20, test_bars=10, min_train_trades=0),
        trial_logger=logger,
    )
    result = validator.run(data)

    folds = len(result.folds)
    assert folds >= 1
    train_trials = [t for t in logger.trials if t["phase"] == "wf_train"]
    test_trials = [t for t in logger.trials if t["phase"] == "wf_test"]
    assert len(train_trials) == folds * 2  # fold × グリッド全組み合わせ
    assert len(test_trials) == folds
    assert all(t["selected"] for t in test_trials)
    # 学習試行はリターン行列の列になる
    assert logger.returns_matrix().shape[1] == len(train_trials)


def test_walk_forward_rejects_overlapping_test_folds() -> None:
    validator = WalkForwardValidator(
        FixedStopLongStrategy,
        {"stop_distance": [0.01]},
        lambda strategy: BacktestEngine(strategy),
        WalkForwardConfig(train_bars=20, test_bars=10, step_bars=5),
    )
    index = pd.date_range("2024-01-01", periods=40, freq="h")
    frame = pd.DataFrame({"open": 1.0, "high": 1.1, "low": 0.9, "close": 1.0}, index=index)
    with pytest.raises(ValueError, match="overlapping test folds"):
        validator.run({"EURUSD": frame})
