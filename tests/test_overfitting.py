"""fx_backtester.overfitting（PBO/CSCV と Deflated Sharpe Ratio）のテスト。"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from fx_backtester.overfitting import (
    deflated_sharpe_ratio,
    expected_max_sharpe,
    norm_cdf,
    norm_ppf,
    per_period_sharpe,
    probability_of_backtest_overfitting,
)

# ---------------------------------------------------------------- 正規分布近似


def test_norm_ppf_known_values() -> None:
    assert norm_ppf(0.5) == pytest.approx(0.0, abs=1e-9)
    assert norm_ppf(0.975) == pytest.approx(1.959964, abs=1e-5)
    assert norm_ppf(0.025) == pytest.approx(-1.959964, abs=1e-5)


def test_norm_ppf_cdf_round_trip() -> None:
    for p in (0.001, 0.02425, 0.3, 0.5, 0.7, 0.99, 0.9999):
        assert norm_cdf(norm_ppf(p)) == pytest.approx(p, abs=1e-8)


def test_norm_ppf_rejects_out_of_range() -> None:
    for p in (0.0, 1.0, -0.1, 1.1):
        with pytest.raises(ValueError):
            norm_ppf(p)


# ---------------------------------------------------------------- Sharpe補助


def test_per_period_sharpe_handles_degenerate_inputs() -> None:
    assert per_period_sharpe(pd.Series([0.01])) == 0.0
    assert per_period_sharpe(pd.Series([0.01, 0.01, 0.01])) == 0.0  # 分散0
    positive = pd.Series([0.01, 0.02, 0.015, 0.005])
    assert per_period_sharpe(positive) > 0.0


def test_expected_max_sharpe_grows_with_trials() -> None:
    variance = 0.05**2
    assert expected_max_sharpe(1, variance) == 0.0
    assert expected_max_sharpe(2, variance) > 0.0
    assert expected_max_sharpe(100, variance) > expected_max_sharpe(10, variance)
    assert expected_max_sharpe(100, 0.0) == 0.0  # 全試行同一なら探索の上振れ無し


# ---------------------------------------------------------------- DSR


def _drift_returns(seed: int = 3, periods: int = 500) -> pd.Series:
    rng = np.random.default_rng(seed)
    index = pd.date_range("2024-01-01", periods=periods, freq="h")
    return pd.Series(rng.normal(0.001, 0.01, periods), index=index)


def test_dsr_deflates_as_trials_increase() -> None:
    returns = _drift_returns()
    own_sharpe = per_period_sharpe(returns)

    single = deflated_sharpe_ratio(returns, [own_sharpe])
    assert single["expected_max_sharpe"] == 0.0  # 探索1回なら控除なし(=PSR)
    assert single["dsr"] > 0.9

    rng = np.random.default_rng(11)
    many_sharpes = list(rng.normal(0.0, 0.05, 199)) + [own_sharpe]
    many = deflated_sharpe_ratio(returns, many_sharpes)
    assert many["n_trials"] == 200
    assert many["expected_max_sharpe"] > 0.0  # 探索200回ぶんのまぐれ控除が入る
    assert many["dsr"] < single["dsr"]  # 同じ成績でも試行が多いほど確信度は下がる


def test_dsr_rejects_degenerate_inputs() -> None:
    with pytest.raises(ValueError):  # 観測不足
        deflated_sharpe_ratio(pd.Series([0.01, 0.02]), [0.1])
    with pytest.raises(ValueError):  # 分散0
        deflated_sharpe_ratio(pd.Series([0.01] * 10), [0.1])
    with pytest.raises(ValueError):  # 有効な試行Sharpe無し
        deflated_sharpe_ratio(_drift_returns(), [float("nan")])


# ---------------------------------------------------------------- PBO(CSCV)


def _noise_matrix(seed: int = 7, periods: int = 256, trials: int = 20) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    index = pd.date_range("2024-01-01", periods=periods, freq="h", tz="UTC")
    return pd.DataFrame(
        rng.normal(0.0, 0.01, (periods, trials)),
        index=index,
        columns=[f"t{i}" for i in range(trials)],
    )


def test_pbo_is_near_half_for_pure_noise() -> None:
    result = probability_of_backtest_overfitting(_noise_matrix(), n_blocks=8)
    # ノイズだけならISの順位にOOS予測力は無い → PBO ≈ 0.5
    assert 0.25 <= result["pbo"] <= 0.75
    assert result["n_trials"] == 20
    assert result["n_combinations"] == 70  # C(8,4)
    assert 0.0 <= result["prob_oos_loss"] <= 1.0


def test_pbo_is_low_when_genuine_skill_exists() -> None:
    matrix = _noise_matrix(seed=5)
    # 1本だけ本物のエッジ(強い正ドリフト)を混ぜる → ISの勝者がOOSでも勝ち続ける
    rng = np.random.default_rng(9)
    matrix["skill"] = rng.normal(0.003, 0.005, len(matrix))
    result = probability_of_backtest_overfitting(matrix, n_blocks=8)
    assert result["pbo"] < 0.3
    assert result["prob_oos_loss"] < 0.3


def test_pbo_is_deterministic() -> None:
    matrix = _noise_matrix(seed=13)
    first = probability_of_backtest_overfitting(matrix, n_blocks=8)
    second = probability_of_backtest_overfitting(matrix, n_blocks=8)
    assert first == second


def test_pbo_rejects_nan_instead_of_treating_unknown_as_flat_return() -> None:
    matrix = _noise_matrix(periods=128, trials=5)
    matrix.iloc[:40, 0] = np.nan
    with pytest.raises(ValueError, match="欠測"):
        probability_of_backtest_overfitting(matrix, n_blocks=4)


def test_pbo_rejects_boolean_return_instead_of_coercing_it_to_one() -> None:
    matrix = _noise_matrix(periods=64, trials=4).astype(object)
    matrix.iloc[10, 2] = True

    with pytest.raises(ValueError, match="boolean"):
        probability_of_backtest_overfitting(matrix, n_blocks=4)


@pytest.mark.parametrize(
    "invalid",
    [float("nan"), float("inf"), float("-inf"), "0.2", True],
)
def test_dsr_rejects_each_invalid_disclosed_trial_sharpe(invalid: object) -> None:
    with pytest.raises(ValueError, match="非数値または非有限値"):
        deflated_sharpe_ratio(_drift_returns(), [0.1, invalid, 0.2])


@pytest.mark.parametrize("invalid", [float("nan"), float("inf"), float("-inf"), True])
def test_dsr_rejects_invalid_selected_return_observations(invalid: object) -> None:
    returns = _drift_returns().astype(object)
    returns.iloc[10] = invalid

    with pytest.raises(ValueError, match="selected_returns contains"):
        deflated_sharpe_ratio(returns, [0.1, 0.2])


def test_pbo_degenerate_trials_return_evaluation_without_regression_crash() -> None:
    index = pd.date_range("2024-01-01", periods=16, freq="h", tz="UTC")
    matrix = pd.DataFrame(0.0, index=index, columns=["always_flat", "also_flat"])

    report = probability_of_backtest_overfitting(matrix, n_blocks=4)

    assert report["pbo"] == pytest.approx(0.5)
    assert report["degradation_available"] is False
    assert report["degradation_slope"] is None
    assert report["degradation_intercept"] is None


def test_pbo_requires_point_in_time_index_and_unique_trial_names() -> None:
    matrix = _noise_matrix(periods=64, trials=4)

    non_datetime = matrix.reset_index(drop=True)
    with pytest.raises(ValueError, match="DatetimeIndex"):
        probability_of_backtest_overfitting(non_datetime, n_blocks=4)

    naive = matrix.copy()
    naive.index = naive.index.tz_localize(None)
    with pytest.raises(ValueError, match="timezone-aware"):
        probability_of_backtest_overfitting(naive, n_blocks=4)

    unsorted = matrix.iloc[::-1]
    with pytest.raises(ValueError, match="一意かつ単調増加"):
        probability_of_backtest_overfitting(unsorted, n_blocks=4)

    duplicated_index = matrix.copy()
    duplicated_index.index = duplicated_index.index.where(
        duplicated_index.index != duplicated_index.index[1],
        duplicated_index.index[0],
    )
    with pytest.raises(ValueError, match="一意かつ単調増加"):
        probability_of_backtest_overfitting(duplicated_index, n_blocks=4)

    duplicated_columns = matrix.copy()
    duplicated_columns.columns = ["same", "same", "t2", "t3"]
    with pytest.raises(ValueError, match="試行列名は一意"):
        probability_of_backtest_overfitting(duplicated_columns, n_blocks=4)


def test_pbo_rejects_invalid_inputs() -> None:
    matrix = _noise_matrix(periods=64, trials=4)
    with pytest.raises(ValueError):  # 奇数ブロック
        probability_of_backtest_overfitting(matrix, n_blocks=7)
    with pytest.raises(ValueError):  # ブロック不足
        probability_of_backtest_overfitting(matrix, n_blocks=2)
    with pytest.raises(ValueError):  # 試行1件
        probability_of_backtest_overfitting(matrix.iloc[:, :1], n_blocks=4)
    with pytest.raises(ValueError):  # 観測不足
        probability_of_backtest_overfitting(matrix.iloc[:6], n_blocks=4)
    with pytest.raises(ValueError):  # 空行列
        probability_of_backtest_overfitting(pd.DataFrame(), n_blocks=4)
