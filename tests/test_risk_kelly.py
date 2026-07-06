"""RiskManager のフラクショナル・ケリー / 別枠VaR 統合のテスト。

ケリーOFF時に従来と完全一致(後方互換)、ON時に実現R倍数でサイズが変わること、
VaRゲートが新規建てを止めることを、RiskManager 単体で検証する。
"""

from __future__ import annotations

import pandas as pd

from fx_backtester.risk import RiskConfig, RiskManager

T0 = pd.Timestamp("2025-06-02 09:00:00")


def _size(manager: RiskManager, equity: float = 100_000.0) -> float:
    """EURUSD・ストップ20pips相当で1トレードのユニット数を得る簡易ヘルパ。"""
    units, _stop, _risk = manager.position_size(
        "EURUSD",
        equity,
        entry_price=1.10,
        stop_distance=0.0020,
        conversion_rates={"EUR": 1.10},
    )
    return units


# ---------------------------------------------------------------- 後方互換(ケリーOFF)


def test_kelly_off_is_backward_compatible() -> None:
    manager = RiskManager(RiskConfig(risk_per_trade_pct=0.01, risk_cap_pct=0.01))
    before = _size(manager)
    # 実現R倍数を流し込んでも、OFFなら effective は固定値のまま
    manager.update_risk_budget([1.0] * 60 + [-1.0] * 40)
    after = _size(manager)
    assert manager.effective_risk_pct == 0.01
    assert before == after  # サイズ不変
    assert manager.kelly_estimate is None  # 推定もしない


def test_kelly_off_no_var_lock_by_default() -> None:
    manager = RiskManager(RiskConfig())
    manager.update_var([-0.05] * 10 + [0.0] * 90)  # 大きな損失分布でも
    assert manager.var_locked is False  # var_limit_pct 未設定ならロックしない
    assert manager.can_open(T0, 100_000.0, no_trade_window=False) is True


# ---------------------------------------------------------------- ケリーON


def test_kelly_on_changes_effective_risk() -> None:
    manager = RiskManager(
        RiskConfig(
            risk_per_trade_pct=0.01,
            risk_cap_pct=0.01,
            use_fractional_kelly=True,
            kelly_fraction=0.5,
            kelly_min_trades=50,
            kelly_full_confidence_trades=100,
            kelly_max_risk_pct=0.02,
        )
    )
    # f*=0.2(勝率60%±1R), ハーフ → target=0.10。上限0.02でクリップされ、
    # 標本100で完全移行 → effective=0.02
    manager.update_risk_budget([1.0] * 60 + [-1.0] * 40)
    assert manager.kelly_estimate is not None
    assert manager.kelly_estimate.usable
    assert manager.effective_risk_pct > 0.01  # 固定1%より積み増し
    assert manager.effective_risk_pct <= 0.02  # 安全上限内


def test_kelly_on_bigger_risk_gives_more_units() -> None:
    fixed = RiskManager(RiskConfig(risk_per_trade_pct=0.01, risk_cap_pct=0.01))
    kelly = RiskManager(
        RiskConfig(
            risk_per_trade_pct=0.01,
            risk_cap_pct=0.01,
            use_fractional_kelly=True,
            kelly_fraction=0.5,
            kelly_max_risk_pct=0.02,
        )
    )
    kelly.update_risk_budget([2.0] * 60 + [-1.0] * 40)  # 高エッジ
    assert _size(kelly) > _size(fixed)  # ケリーのほうが大きく張る


def test_kelly_insufficient_sample_falls_back() -> None:
    manager = RiskManager(
        RiskConfig(
            risk_per_trade_pct=0.01,
            use_fractional_kelly=True,
            kelly_min_trades=50,
        )
    )
    manager.update_risk_budget([1.0, -1.0, 1.0])  # 3件だけ
    assert manager.effective_risk_pct == 0.01  # 固定へフォールバック
    assert manager.kelly_estimate is not None
    assert not manager.kelly_estimate.usable


def test_kelly_negative_edge_shrinks_to_zero_risk() -> None:
    manager = RiskManager(
        RiskConfig(
            risk_per_trade_pct=0.01,
            use_fractional_kelly=True,
            kelly_fraction=0.5,
            kelly_min_trades=50,
            kelly_full_confidence_trades=50,
        )
    )
    # 勝率40%で負のエッジ → f*=0 → target=0、標本50で完全移行 → effective≈0
    manager.update_risk_budget([1.0] * 40 + [-1.0] * 60)
    assert manager.effective_risk_pct == 0.0
    assert _size(manager) == 0.0  # 張らない


# ---------------------------------------------------------------- VaRゲート


def test_var_gate_blocks_new_entries_when_breached() -> None:
    manager = RiskManager(RiskConfig(var_limit_pct=0.03, var_confidence=0.95, var_min_samples=30))
    # 下位5%点が -5% になる分布 → VaR 5% > 上限3% → ロック
    manager.update_var([-0.05] * 5 + [0.0] * 95)
    assert manager.var_estimate is not None
    assert manager.var_estimate.var_pct >= 0.03
    assert manager.var_locked is True
    assert manager.risk_locked is True
    assert manager.can_open(T0, 100_000.0, no_trade_window=False) is False


def test_var_gate_allows_when_within_limit() -> None:
    manager = RiskManager(RiskConfig(var_limit_pct=0.10, var_min_samples=30))
    manager.update_var([-0.05] * 5 + [0.0] * 95)  # VaR 5% < 上限10%
    assert manager.var_locked is False
    assert manager.can_open(T0, 100_000.0, no_trade_window=False) is True


def test_var_reset_clears_lock() -> None:
    manager = RiskManager(RiskConfig(var_limit_pct=0.03, var_min_samples=30))
    manager.update_var([-0.05] * 5 + [0.0] * 95)
    assert manager.var_locked is True
    manager.reset()
    assert manager.var_locked is False
    assert manager.var_estimate is None
    assert manager.effective_risk_pct == manager.config.risk_per_trade_pct
