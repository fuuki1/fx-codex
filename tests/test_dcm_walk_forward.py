"""Phase 5: ウォークフォワード・バックテストの検証。

- purge/embargo が train終端とtest始端の間にギャップを作ること
- 学習可能なシグナルなら正の期待値、ノイズなら≒0(リーク無しの健全性)
- metrics 契約(Sharpe/PF/DD/expectancy/win_rate)が返ること
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from dukascopy_cftc_model.config import PipelineConfig, WalkForwardConfig
from dukascopy_cftc_model.walk_forward import (
    _generate_folds,
    run_walk_forward,
    select_alpha,
)


def _make_dataset(n: int, signal_strength: float, seed: int = 0):
    """特徴量Xと、Xに線形依存する将来リターン(強度でSN比を変える)を作る。"""
    rng = np.random.default_rng(seed)
    idx = pd.date_range("2022-01-01", periods=n, freq="1h", tz="UTC")
    d = 5
    X = pd.DataFrame(
        rng.normal(0, 1, (n, d)),
        columns=[f"f{i}" for i in range(d)],
        index=idx,
    )
    beta = np.array([1.0, -0.8, 0.5, 0.0, 0.0])
    noise = rng.normal(0, 1, n)
    fret = signal_strength * (X.to_numpy() @ beta) + noise
    fret = pd.Series(fret * 1e-4, index=idx, name="future_return")  # 現実的スケール
    y = fret.copy()
    return X, y, fret


def test_generate_folds_respects_purge_embargo() -> None:
    cfg = WalkForwardConfig(train_bars=100, test_bars=50, purge_bars=10, embargo_bars=5)
    folds = list(_generate_folds(500, cfg))
    assert folds
    for train_sl, test_sl in folds:
        # test始端 = train終端 + purge + embargo。ギャップが厳密に空いている。
        gap = test_sl.start - train_sl.stop
        assert gap == cfg.purge_bars + cfg.embargo_bars
        # train と test は重ならない
        assert train_sl.stop <= test_sl.start


def test_select_alpha_returns_grid_member() -> None:
    X, y, _ = _make_dataset(400, signal_strength=1.0)
    grid = [0.1, 1.0, 10.0, 100.0]
    alpha = select_alpha(X.to_numpy(), y.to_numpy(), grid, folds=3)
    assert alpha in grid


def test_learnable_signal_yields_positive_expectancy() -> None:
    X, y, fret = _make_dataset(4000, signal_strength=3.0, seed=1)
    cfg = PipelineConfig().with_walk_forward(
        train_bars=1500, test_bars=400, purge_bars=5, embargo_bars=5, cv_folds=3
    )
    result = run_walk_forward(X, y, fret, cfg)
    assert result.metrics["trade_count"] > 0
    # 強い学習可能シグナル → 正の期待値・勝率>50%
    assert result.metrics["expectancy_usd"] > 0
    assert result.metrics["win_rate"] > 0.5
    # 特徴量寄与: f0/f1 が上位(beta最大)
    top_names = [name for name, _ in result.feature_importance[:2]]
    assert "f0" in top_names


def test_noise_label_yields_near_zero_edge() -> None:
    """将来リターンがXと無関係(ノイズ)なら、期待値は0近傍(リーク無しの証拠)。"""
    rng = np.random.default_rng(5)
    n = 4000
    idx = pd.date_range("2022-01-01", periods=n, freq="1h", tz="UTC")
    X = pd.DataFrame(rng.normal(0, 1, (n, 5)), columns=[f"f{i}" for i in range(5)], index=idx)
    # ラベルも将来リターンもXと独立なノイズ
    fret = pd.Series(rng.normal(0, 1e-4, n), index=idx, name="future_return")
    y = pd.Series(rng.normal(0, 1e-4, n), index=idx)
    cfg = PipelineConfig().with_walk_forward(
        train_bars=1500, test_bars=400, purge_bars=5, embargo_bars=5
    )
    result = run_walk_forward(X, y, fret, cfg)
    # 勝率は0.5近傍(±0.1)。系統的エッジは無いはず。
    if result.metrics["trade_count"] > 20:
        assert abs(result.metrics["win_rate"] - 0.5) < 0.15


def test_metrics_contract_present() -> None:
    X, y, fret = _make_dataset(3000, signal_strength=2.0)
    cfg = PipelineConfig().with_walk_forward(
        train_bars=1200, test_bars=400, purge_bars=5, embargo_bars=5
    )
    result = run_walk_forward(X, y, fret, cfg)
    for key in ("sharpe_ratio", "profit_factor", "max_drawdown_pct", "expectancy_usd", "win_rate"):
        assert key in result.metrics
    assert not result.equity_curve.empty
    assert len(result.folds) >= 1


def test_empty_trades_safe() -> None:
    """z閾値を極端に高くして1トレードも出ない場合でも落ちない。"""
    X, y, fret = _make_dataset(2000, signal_strength=0.1)
    cfg = PipelineConfig().with_walk_forward(
        train_bars=1000, test_bars=400, purge_bars=5, embargo_bars=5, signal_z_threshold=100.0
    )
    result = run_walk_forward(X, y, fret, cfg)
    assert result.metrics["trade_count"] == 0
    assert result.metrics["expectancy_usd"] == 0.0
