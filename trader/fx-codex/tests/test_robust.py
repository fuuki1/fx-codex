from __future__ import annotations

import numpy as np
from fx_backtester import robust


def test_sharpe_ratio_basic():
    r = np.array([0.01, 0.02, -0.005, 0.015, 0.0])
    assert robust.sharpe_ratio(r) != 0.0
    assert robust.sharpe_ratio(np.array([0.01, 0.01, 0.01])) == 0.0  # 分散0 → 0
    # 年率化は倍率 sqrt(ppy)
    assert robust.sharpe_ratio(r, periods_per_year=252) == robust.sharpe_ratio(r) * np.sqrt(252)


def test_psr_high_for_consistent_edge_low_for_noise():
    rng = np.random.default_rng(1)
    edge = 0.001 + 0.0005 * rng.standard_normal(500)   # 明確な正のドリフト
    noise = 0.01 * rng.standard_normal(500)            # 平均ほぼ0
    assert robust.probabilistic_sharpe_ratio(edge) > 0.95
    assert robust.probabilistic_sharpe_ratio(noise) < 0.9  # 確信は高くない


def test_expected_max_sharpe_grows_with_trials():
    # 試行回数が増えるほど「まぐれ最大 Sharpe」の基準は上がる
    few = robust.expected_max_sharpe(sr_variance=0.01, n_trials=5)
    many = robust.expected_max_sharpe(sr_variance=0.01, n_trials=500)
    assert many > few > 0.0


def test_deflated_sharpe_below_psr_with_many_trials():
    rng = np.random.default_rng(2)
    r = 0.0008 + 0.01 * rng.standard_normal(600)
    psr = robust.probabilistic_sharpe_ratio(r)
    dsr = robust.deflated_sharpe_ratio(r, n_trials=200, sr_variance=0.02)
    # 多重検定を補正した DSR は素の PSR 以下（基準が上がるため）
    assert dsr <= psr


def test_pbo_low_for_genuine_edge_high_for_pure_noise():
    rng = np.random.default_rng(3)
    T, N = 240, 8
    # 1) 本物のエッジ: 戦略0 が全期間で高平均。IS 最良=OOS 最良 → PBO 低い
    edge = 0.005 * rng.standard_normal((T, N))
    edge[:, 0] += 0.02
    pbo_edge = robust.pbo_cscv(edge, n_splits=6)
    assert pbo_edge < 0.25

    # 2) 純ノイズ: どれも優位性なし → IS 最良は OOS で沈みやすい（PBO 高め）
    noise = rng.standard_normal((T, N))
    pbo_noise = robust.pbo_cscv(noise, n_splits=6)
    assert 0.0 <= pbo_noise <= 1.0
    assert pbo_noise > pbo_edge


def test_max_drawdown_known_series():
    # +10% の後 -50% → 最大DDは約 -0.5
    r = np.array([0.10, -0.50, 0.05])
    assert robust.max_drawdown(r) < -0.45


def test_monte_carlo_bootstrap_distribution():
    rng = np.random.default_rng(4)
    r = 0.0006 + 0.008 * rng.standard_normal(400)   # わずかに正のドリフト
    mc = robust.monte_carlo_bootstrap(r, n_paths=300, seed=0)
    d = mc.to_dict()
    assert d["paths"] == 300
    assert 0.0 <= d["prob_profit"] <= 1.0
    assert d["prob_profit"] > 0.5                    # 正ドリフトなので勝ち越しが多い
    assert d["sharpe_p05"] <= d["sharpe_median"] <= d["sharpe_p95"]
    assert d["maxdd_p95"] >= d["maxdd_median"] >= 0.0
    # 標本が短すぎる場合は空結果
    assert robust.monte_carlo_bootstrap(np.array([0.1, 0.2])).paths == 0
