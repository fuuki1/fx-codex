"""検証パイプライン(validation_pipeline)の合否判定テスト。

walk-forward → PBO/DSR/SPA → ドリフト → デプロイ合否を1本に束ねた層を、
合成データ(本物のエッジ / 純ノイズ)で end-to-end に検証する。ネットワーク不要。
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from fx_backtester.engine import BacktestConfig, BacktestEngine, ExecutionConfig
from fx_backtester.strategies import MovingAverageCross
from fx_backtester.validation_pipeline import (
    DeployGateConfig,
    evaluate_deploy_gate,
)
from fx_backtester.walk_forward import WalkForwardConfig, WalkForwardValidator


def _trending_data(periods: int = 900, seed: int = 0) -> dict[str, pd.DataFrame]:
    """緩やかな上昇トレンド+ノイズ(MAクロスが順張りで取れる)合成OHLC。"""
    rng = np.random.default_rng(seed)
    index = pd.date_range("2024-01-01", periods=periods, freq="h")
    close = 100.0
    rows = []
    for i in range(periods):
        drift = 0.0008 if (i // 60) % 2 == 0 else -0.0003  # 上昇が優勢なレジーム
        close *= 1 + drift + rng.normal(0, 0.0015)
        open_ = close / (1 + rng.normal(0, 0.0005))
        high = max(open_, close) * (1 + abs(rng.normal(0, 0.0008)))
        low = min(open_, close) * (1 - abs(rng.normal(0, 0.0008)))
        rows.append({"open": open_, "high": high, "low": low, "close": close, "spread_price": 0.01})
    return {"USDJPY": pd.DataFrame(rows, index=index)}


def _noise_data(periods: int = 900, seed: int = 1) -> dict[str, pd.DataFrame]:
    """方向性の無いランダムウォーク(エッジが存在しない)合成OHLC。"""
    rng = np.random.default_rng(seed)
    index = pd.date_range("2024-01-01", periods=periods, freq="h")
    close = 100.0
    rows = []
    for _ in range(periods):
        close *= 1 + rng.normal(0, 0.002)
        open_ = close / (1 + rng.normal(0, 0.0005))
        high = max(open_, close) * (1 + abs(rng.normal(0, 0.0008)))
        low = min(open_, close) * (1 - abs(rng.normal(0, 0.0008)))
        rows.append({"open": open_, "high": high, "low": low, "close": close, "spread_price": 0.01})
    return {"USDJPY": pd.DataFrame(rows, index=index)}


def _validator() -> WalkForwardValidator:
    config = BacktestConfig(
        initial_cash=100_000,
        execution=ExecutionConfig(
            spread_pips={"USDJPY": 0.5}, slippage_pips={"USDJPY": 0.2},
            commission_per_million_usd=0.0,
        ),
    )
    grid = {"fast_window": [6, 12], "slow_window": [24, 48], "stop_atr_multiple": [1.5, 2.0]}
    return WalkForwardValidator(
        MovingAverageCross,
        grid,
        lambda strategy: BacktestEngine(strategy, config),
        WalkForwardConfig(train_bars=250, test_bars=100, embargo_bars=10, max_parameter_combinations=8),
    )


# ---------------------------------------------------------------- 構造・基本


def test_verdict_has_all_gate_fields() -> None:
    verdict = evaluate_deploy_gate(_validator(), _trending_data(), DeployGateConfig())
    d = verdict.to_dict()
    assert set(d) >= {"deploy_ok", "reasons", "dsr", "pbo", "spa_pvalue", "drift_points", "n_folds"}
    assert isinstance(verdict.deploy_ok, bool)
    assert verdict.n_folds >= 1
    # 各ゲートが動いた証跡(数値 or 理由)が残る
    assert verdict.pbo is not None or any("PBO" in r for r in verdict.reasons)


def test_noise_data_is_rejected() -> None:
    # 純ノイズはエッジが無いので、いずれかのゲートで棄却される
    verdict = evaluate_deploy_gate(_validator(), _noise_data(), DeployGateConfig())
    assert verdict.deploy_ok is False
    assert verdict.reasons  # 理由が必ず残る


# ---------------------------------------------------------------- 各ゲートの発火


def test_strict_dsr_threshold_rejects() -> None:
    # dsr_min=1.0(達成不能)にすれば必ずDSRゲートで落ちる
    verdict = evaluate_deploy_gate(
        _validator(), _trending_data(), DeployGateConfig(dsr_min=1.0, require_no_drift=False)
    )
    assert verdict.deploy_ok is False
    assert any("DSR" in r for r in verdict.reasons)


def test_strict_pbo_threshold_rejects() -> None:
    # pbo_max=0.0(達成不能)にすれば必ずPBOゲートで落ちる
    verdict = evaluate_deploy_gate(
        _validator(), _trending_data(), DeployGateConfig(pbo_max=0.0, require_no_drift=False)
    )
    assert verdict.deploy_ok is False
    assert any("PBO" in r for r in verdict.reasons)


def test_strict_spa_threshold_rejects() -> None:
    verdict = evaluate_deploy_gate(
        _validator(), _trending_data(), DeployGateConfig(spa_max=0.0, require_no_drift=False)
    )
    assert verdict.deploy_ok is False
    assert any("SPA" in r for r in verdict.reasons)


def test_drift_gate_can_be_disabled() -> None:
    # require_no_drift=False なら drift 理由は付かない
    verdict = evaluate_deploy_gate(
        _validator(), _trending_data(), DeployGateConfig(require_no_drift=False)
    )
    assert not any("ドリフト" in r for r in verdict.reasons)


# ---------------------------------------------------------------- 判定の一貫性


def test_verdict_is_deterministic() -> None:
    cfg = DeployGateConfig(spa_seed=123)
    a = evaluate_deploy_gate(_validator(), _trending_data(seed=5), cfg)
    b = evaluate_deploy_gate(_validator(), _trending_data(seed=5), cfg)
    assert a.deploy_ok == b.deploy_ok
    assert a.pbo == b.pbo
    assert a.spa_pvalue == b.spa_pvalue


def test_deploy_ok_requires_all_gates_pass() -> None:
    # deploy_ok True のときは reasons が空、False のときは必ず理由がある(相互排他)
    verdict = evaluate_deploy_gate(_validator(), _trending_data(), DeployGateConfig())
    assert verdict.deploy_ok == (len(verdict.reasons) == 0)
