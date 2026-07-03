from __future__ import annotations

from dataclasses import dataclass
from itertools import product
from collections.abc import Callable

import pandas as pd

from fx_backtester.engine import BacktestEngine, BacktestResult
from fx_backtester.strategies.base import Strategy
from fx_backtester.trial_log import TrialLogger


def _wf_trial_id(fold: int, params: dict[str, object]) -> str:
    label = "-".join(f"{key}={params[key]}" for key in sorted(params))
    return f"fold{fold}-{label}"


@dataclass
class WalkForwardConfig:
    train_bars: int = 500
    test_bars: int = 100
    step_bars: int | None = None
    purge_bars: int = 0
    embargo_bars: int = 0
    max_parameter_combinations: int = 20
    min_train_trades: int = 3


@dataclass
class WalkForwardFold:
    fold: int
    train_start: pd.Timestamp
    train_end: pd.Timestamp
    test_start: pd.Timestamp
    test_end: pd.Timestamp
    purge_bars: int
    embargo_bars: int
    selected_params: dict[str, object]
    train_metrics: dict[str, float | int]
    test_metrics: dict[str, float | int]


@dataclass
class WalkForwardResult:
    folds: list[WalkForwardFold]
    selected_test_results: list[BacktestResult]

    def summary(self) -> pd.DataFrame:
        rows = []
        for fold in self.folds:
            row = {
                "fold": fold.fold,
                "train_start": fold.train_start,
                "train_end": fold.train_end,
                "test_start": fold.test_start,
                "test_end": fold.test_end,
                "purge_bars": fold.purge_bars,
                "embargo_bars": fold.embargo_bars,
                "selected_params": fold.selected_params,
            }
            row.update({f"train_{k}": v for k, v in fold.train_metrics.items()})
            row.update({f"test_{k}": v for k, v in fold.test_metrics.items()})
            rows.append(row)
        return pd.DataFrame(rows)


class WalkForwardValidator:
    """Rolling train/test validation with intentionally small parameter grids."""

    def __init__(
        self,
        strategy_cls: Callable[..., Strategy],
        parameter_grid: dict[str, list[object]],
        engine_factory: Callable[[Strategy], BacktestEngine],
        config: WalkForwardConfig | None = None,
        trial_logger: TrialLogger | None = None,
    ) -> None:
        self.strategy_cls = strategy_cls
        self.parameter_grid = parameter_grid
        self.engine_factory = engine_factory
        self.config = config or WalkForwardConfig()
        # 渡すと fold ごとの全学習試行と採択テストを記録する(PBO/DSR・監査の入力)
        self.trial_logger = trial_logger

    def run(self, data: dict[str, pd.DataFrame]) -> WalkForwardResult:
        combinations = self._parameter_combinations()
        all_times = pd.DatetimeIndex(sorted(set().union(*(frame.index for frame in data.values()))))
        if self.config.purge_bars < 0 or self.config.embargo_bars < 0:
            raise ValueError("purge_bars and embargo_bars must be >= 0")
        required_bars = self.config.train_bars + self.config.embargo_bars + self.config.test_bars
        if len(all_times) < required_bars:
            raise ValueError("Not enough bars for requested walk-forward windows")

        step = self.config.step_bars or self.config.test_bars
        folds: list[WalkForwardFold] = []
        selected_test_results: list[BacktestResult] = []
        fold_number = 1
        start = 0

        while start + required_bars <= len(all_times):
            train_stop = start + self.config.train_bars - self.config.purge_bars
            test_start = start + self.config.train_bars + self.config.embargo_bars
            test_stop = test_start + self.config.test_bars
            if train_stop <= start:
                raise ValueError("purge_bars leaves no training bars")
            train_index = all_times[start:train_stop]
            test_index = all_times[test_start:test_stop]
            train_data = self._slice_data(data, train_index)
            test_data = self._slice_data(data, test_index)

            best_params: dict[str, object] | None = None
            best_score = float("-inf")
            best_train_result: BacktestResult | None = None
            for params in combinations:
                strategy = self.strategy_cls(**params)
                result = self.engine_factory(strategy).run(train_data)
                score = self._score(result.metrics)
                if result.metrics["trade_count"] < self.config.min_train_trades:
                    score = float("-inf")
                if self.trial_logger is not None:
                    self.trial_logger.log(
                        _wf_trial_id(fold_number, params),
                        params=params,
                        phase="wf_train",
                        metrics=result.metrics,
                        score=score,
                        window={
                            "kind": "train",
                            "fold": fold_number,
                            "start": train_index[0],
                            "end": train_index[-1],
                        },
                        returns=result.equity_curve["equity"].pct_change().dropna(),
                    )
                if score > best_score:
                    best_score = score
                    best_params = params
                    best_train_result = result

            if best_params is None or best_train_result is None:
                best_params = combinations[0]
                best_train_result = self.engine_factory(self.strategy_cls(**best_params)).run(
                    train_data
                )

            test_result = self.engine_factory(self.strategy_cls(**best_params)).run(test_data)
            if self.trial_logger is not None:
                self.trial_logger.log(
                    f"fold{fold_number}-selected-test",
                    params=best_params,
                    phase="wf_test",
                    metrics=test_result.metrics,
                    window={
                        "kind": "test",
                        "fold": fold_number,
                        "start": test_index[0],
                        "end": test_index[-1],
                    },
                    selected=True,
                )
            selected_test_results.append(test_result)
            folds.append(
                WalkForwardFold(
                    fold=fold_number,
                    train_start=train_index[0],
                    train_end=train_index[-1],
                    test_start=test_index[0],
                    test_end=test_index[-1],
                    purge_bars=self.config.purge_bars,
                    embargo_bars=self.config.embargo_bars,
                    selected_params=best_params,
                    train_metrics=best_train_result.metrics,
                    test_metrics=test_result.metrics,
                )
            )

            fold_number += 1
            start += step

        return WalkForwardResult(folds=folds, selected_test_results=selected_test_results)

    def _parameter_combinations(self) -> list[dict[str, object]]:
        keys = list(self.parameter_grid)
        values = [self.parameter_grid[key] for key in keys]
        combinations = [dict(zip(keys, combo)) for combo in product(*values)]
        if len(combinations) > self.config.max_parameter_combinations:
            raise ValueError(
                "Parameter grid is too large. "
                f"{len(combinations)} combinations requested; "
                f"limit is {self.config.max_parameter_combinations} to reduce overfitting risk."
            )
        return combinations

    def _slice_data(
        self,
        data: dict[str, pd.DataFrame],
        index: pd.DatetimeIndex,
    ) -> dict[str, pd.DataFrame]:
        return {
            symbol: frame.loc[frame.index.isin(index)].copy()
            for symbol, frame in data.items()
            if frame.index.isin(index).any()
        }

    def _score(self, metrics: dict[str, float | int]) -> float:
        profit_factor = float(metrics["profit_factor"])
        if profit_factor == float("inf"):
            profit_factor = 3.0
        profit_factor = min(profit_factor, 3.0)
        return (
            float(metrics["sharpe_ratio"])
            + float(metrics["expectancy_r"])
            + 0.1 * profit_factor
            - float(metrics["max_drawdown_pct"])
        )
