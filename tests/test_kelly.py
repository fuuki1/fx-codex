"""kelly.py(フラクショナル・ケリー + 別枠VaR)のテスト。

すべて標準ライブラリだけの純粋関数なので、既知のW/R・分布で解析的に検証する。
"""

from __future__ import annotations

import pytest

from fx_backtester.kelly import (
    fractional_kelly_risk_pct,
    historical_var,
    kelly_fraction_from_r_multiples,
    parametric_var,
    var_breached,
)

# ---------------------------------------------------------------- ケリー比率


def test_kelly_fraction_known_case() -> None:
    # 勝率60%、勝ちは+1R、負けは-1R → b=1, p=0.6, q=0.4 → f*=(1*0.6-0.4)/1=0.2
    r = [1.0] * 60 + [-1.0] * 40
    est = kelly_fraction_from_r_multiples(r, min_trades=50)
    assert est.usable
    assert est.win_rate == pytest.approx(0.6)
    assert est.payoff_ratio == pytest.approx(1.0)
    assert est.kelly_fraction == pytest.approx(0.2, abs=1e-3)


def test_kelly_fraction_higher_payoff_raises_fraction() -> None:
    # 勝率50%だが勝ちが+2R・負けが-1R → b=2 → f*=(2*0.5-0.5)/2=0.25
    r = [2.0] * 50 + [-1.0] * 50
    est = kelly_fraction_from_r_multiples(r, min_trades=50)
    assert est.kelly_fraction == pytest.approx(0.25, abs=1e-3)


def test_kelly_fraction_negative_edge_clips_to_zero() -> None:
    # 勝率40%、±1R → f* = (1*0.4-0.6)/1 = -0.2 → 0にクリップ(張らない)
    r = [1.0] * 40 + [-1.0] * 60
    est = kelly_fraction_from_r_multiples(r, min_trades=50)
    assert est.kelly_fraction == 0.0
    assert est.usable  # 標本は足りるので usable=True(比率0=張らない)


def test_kelly_fraction_insufficient_sample_not_usable() -> None:
    est = kelly_fraction_from_r_multiples([1.0, -1.0, 1.0], min_trades=50)
    assert not est.usable
    assert est.kelly_fraction == 0.0
    assert "サンプル不足" in est.note


def test_kelly_fraction_all_wins_not_usable() -> None:
    est = kelly_fraction_from_r_multiples([1.0] * 60, min_trades=50)
    assert not est.usable  # 負けが無いとペイオフ比を定義できない


def test_kelly_fraction_ignores_non_finite() -> None:
    r = [1.0] * 60 + [-1.0] * 40 + [float("nan"), float("inf")]
    est = kelly_fraction_from_r_multiples(r, min_trades=50)
    assert est.sample_size == 100  # nan/inf は除外


# ---------------------------------------------------------------- フラクショナル変換


def test_fractional_kelly_falls_back_when_not_usable() -> None:
    est = kelly_fraction_from_r_multiples([1.0, -1.0], min_trades=50)  # 不足
    risk, note = fractional_kelly_risk_pct(est, baseline_pct=0.01)
    assert risk == 0.01  # 固定フラクショナルへフォールバック
    assert "固定" in note


def test_fractional_kelly_quarter_blends_with_sample() -> None:
    # f*=0.2, quarter → target=0.05。標本100(full_confidence)で完全移行
    r = [1.0] * 60 + [-1.0] * 40  # n=100
    est = kelly_fraction_from_r_multiples(r, min_trades=50)
    risk, note = fractional_kelly_risk_pct(
        est,
        baseline_pct=0.01,
        fraction=0.25,
        max_risk_pct=0.02,
        full_confidence_trades=100,
    )
    # target=0.05 だが max_risk_pct=0.02 でクリップされる
    assert risk == pytest.approx(0.02)
    assert "クォーター" in note


def test_fractional_kelly_respects_max_cap() -> None:
    r = [3.0] * 70 + [-1.0] * 30  # 高エッジで f* 大
    est = kelly_fraction_from_r_multiples(r, min_trades=50)
    risk, _ = fractional_kelly_risk_pct(
        est,
        baseline_pct=0.01,
        fraction=0.5,
        max_risk_pct=0.015,
        full_confidence_trades=100,
    )
    assert risk <= 0.015  # ハーフでも上限を超えない


def test_fractional_kelly_partial_blend_between_bounds() -> None:
    # 標本75(min50, full100)→ ブレンド50%。target を控えめにして中間値を確認
    r = [1.0] * 45 + [-1.0] * 30  # n=75, p=0.6, f*=0.2
    est = kelly_fraction_from_r_multiples(r, min_trades=50)
    risk, _ = fractional_kelly_risk_pct(
        est,
        baseline_pct=0.01,
        fraction=0.1,
        max_risk_pct=0.05,
        full_confidence_trades=100,
    )
    # target = 0.2*0.1 = 0.02。ブレンド (75-50)/(100-50)=0.5 → 0.01*0.5 + 0.02*0.5 = 0.015
    assert risk == pytest.approx(0.015, abs=1e-4)


def test_fractional_kelly_rejects_bad_fraction() -> None:
    r = [1.0] * 60 + [-1.0] * 40
    est = kelly_fraction_from_r_multiples(r, min_trades=50)
    with pytest.raises(ValueError):
        fractional_kelly_risk_pct(est, baseline_pct=0.01, fraction=1.5)
    with pytest.raises(ValueError):
        fractional_kelly_risk_pct(est, baseline_pct=0.0, fraction=0.25)


# ---------------------------------------------------------------- VaR(別枠)


def test_historical_var_picks_lower_tail() -> None:
    # 100サンプル中、下位5件を -5%〜 に置くと 95% VaR(下位5%点)が損失側に来る。
    # rank = floor(0.05*100) = 5 → sorted[5] が閾値。下位5件(index 0..4)を大損失に。
    returns = [-0.05, -0.05, -0.05, -0.05, -0.05] + [0.001 * ((i % 5) - 2) for i in range(95)]
    est = historical_var(returns, confidence=0.95, min_samples=30)
    assert est.usable
    assert est.var_pct >= 0.002  # 下位5%点は損失側(正確な値は分布次第)
    assert est.cvar_pct >= est.var_pct  # CVaR(テール平均)はVaR以上
    # 下位5件が-5%なので、それらを含むCVaRは大きい
    assert est.cvar_pct == pytest.approx(0.05, abs=1e-6)


def test_historical_var_quantile_index() -> None:
    # 20サンプル、alpha=0.05 → rank=ceil(1.0)-1=0 → sorted[0] が閾値(最悪値)。
    returns = [-0.10, -0.08] + [0.01] * 18
    est = historical_var(returns, confidence=0.95, min_samples=10)
    assert est.var_pct == pytest.approx(0.10, abs=1e-6)  # sorted[0] = -0.10
    # CVaR は下位1件のみ(rank+1=1) → -0.10 の平均
    assert est.cvar_pct == pytest.approx(0.10, abs=1e-6)


def test_historical_var_insufficient_samples() -> None:
    est = historical_var([-0.01, 0.02], confidence=0.95, min_samples=30)
    assert not est.usable
    assert est.var_pct == 0.0


def test_parametric_var_matches_gaussian() -> None:
    # μ=0, σ=0.01 の正規なら 95% VaR ≈ 1.645*0.01 = 0.01645
    import random

    rng = random.Random(0)
    returns = [rng.gauss(0.0, 0.01) for _ in range(2000)]
    est = parametric_var(returns, confidence=0.95, min_samples=30)
    assert est.var_pct == pytest.approx(0.01645, abs=2e-3)


def test_var_breached_gate() -> None:
    # 下位5%点が -5% になるよう、下位5件を大損失に
    returns = [-0.05] * 5 + [0.0] * 95
    est = historical_var(returns, confidence=0.95)
    assert est.var_pct == pytest.approx(0.05, abs=1e-6)
    assert var_breached(est, limit_pct=0.03) is True  # 5% > 3%上限
    assert var_breached(est, limit_pct=0.10) is False  # 5% < 10%上限


def test_var_breached_false_when_not_usable() -> None:
    est = historical_var([0.0, 0.0], min_samples=30)  # 標本不足
    assert var_breached(est, limit_pct=0.01) is False
