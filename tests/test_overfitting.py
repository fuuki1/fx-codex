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
    superior_predictive_ability,
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
    index = pd.date_range("2024-01-01", periods=periods, freq="h")
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


def test_pbo_handles_nan_as_flat_periods() -> None:
    matrix = _noise_matrix(periods=128, trials=5)
    matrix.iloc[:40, 0] = np.nan  # 一部試行だけ欠測(ポジション無し)でも計算できる
    result = probability_of_backtest_overfitting(matrix, n_blocks=4)
    assert 0.0 <= result["pbo"] <= 1.0


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


# ---------------------------------------------------------------- SPA検定


def test_spa_low_pvalue_for_genuine_edge() -> None:
    # 1戦略だけ一貫して正の超過性能 → 帰無「優位ゼロ」を棄却(p値小)
    rng = np.random.default_rng(11)
    index = pd.date_range("2024-01-01", periods=300, freq="h")
    data = {f"noise_{k}": rng.normal(0.0, 0.01, 300) for k in range(9)}
    data["skilled"] = rng.normal(0.004, 0.01, 300)  # 明確な正のエッジ
    perf = pd.DataFrame(data, index=index)
    result = superior_predictive_ability(perf, n_bootstrap=500, seed=1)
    assert result["best_strategy"] == "skilled"
    assert result["spa_pvalue"] < 0.05  # 有意


def test_spa_high_pvalue_for_pure_noise() -> None:
    # 全戦略が平均ゼロのノイズ → 最良の優位はデータマイニングの産物(p値大)
    rng = np.random.default_rng(12)
    index = pd.date_range("2024-01-01", periods=300, freq="h")
    perf = pd.DataFrame({f"noise_{k}": rng.normal(0.0, 0.01, 300) for k in range(10)}, index=index)
    result = superior_predictive_ability(perf, n_bootstrap=500, seed=2)
    assert result["spa_pvalue"] > 0.10  # 有意でない


def test_spa_is_deterministic_with_seed() -> None:
    rng = np.random.default_rng(13)
    perf = pd.DataFrame(
        {f"s_{k}": rng.normal(0.001, 0.01, 200) for k in range(5)},
        index=pd.date_range("2024-01-01", periods=200, freq="h"),
    )
    a = superior_predictive_ability(perf, n_bootstrap=300, seed=42)
    b = superior_predictive_ability(perf, n_bootstrap=300, seed=42)
    assert a["spa_pvalue"] == b["spa_pvalue"]
    assert a["test_statistic"] == b["test_statistic"]


def test_spa_rejects_invalid_inputs() -> None:
    with pytest.raises(ValueError):
        superior_predictive_ability(pd.DataFrame())
    tiny = pd.DataFrame({"s": [0.01, 0.02]}, index=pd.date_range("2024-01-01", periods=2, freq="h"))
    with pytest.raises(ValueError):
        superior_predictive_ability(tiny)  # 観測不足


def test_pbo_handles_degenerate_returns_without_crashing() -> None:
    # ほぼ定数リターン(分散≈0)でも LinAlgError/LAPACK 警告を出さず PBO を返す。
    # 実データの縮退fold(ほぼ無取引)で pipeline がクラッシュしたのを回帰テスト化。
    import warnings

    index = pd.date_range("2024-01-01", periods=128, freq="h")
    matrix = pd.DataFrame({f"t{i}": [0.0] * 128 for i in range(6)}, index=index)
    matrix.iloc[0, 0] = 1e-9  # わずかな非ゼロ(全ゼロ列の縮退回避)
    with warnings.catch_warnings():
        warnings.simplefilter("error")  # LAPACK等の警告漏れも失敗にする
        result = probability_of_backtest_overfitting(matrix, n_blocks=4)
    assert 0.0 <= result["pbo"] <= 1.0
    slope = result["degradation_slope"]
    assert np.isnan(slope) or np.isfinite(slope)  # 退化でクラッシュしないことが要件


def test_safe_linfit_returns_nan_on_constant_x() -> None:
    from fx_backtester.overfitting import _safe_linfit

    slope, intercept = _safe_linfit(np.array([1.0, 1.0, 1.0]), np.array([1.0, 2.0, 3.0]))
    assert np.isnan(slope) and np.isnan(intercept)  # x が定数 → 傾き定義不能
