"""9段チェックリスト型 意思決定パイプライン(decision_pipeline.py)のテスト。

ネットワーク不要。各ゲートの ok/warn/block/skip の分岐と、コードに新規実装した
スプレッド確認・執行コスト控除・ポジションサイズ算出を検証する。
"""

from __future__ import annotations

from datetime import datetime, UTC

from fx_intel.decision_pipeline import (
    ATR_PCT_MAX,
    SPREAD_BLOCK_FRACTION,
    estimate_expected_r,
    execution_cost_in_r,
    position_units,
    run_pipeline,
)
from fx_intel.sentiment import CurrencySentiment
from fx_intel.technicals import IntervalView, PairTechnicals

# 木曜・市場オープン。build_trade_plan/is_market_open と同じ扱い。
OPEN = datetime(2026, 7, 2, 8, 0, tzinfo=UTC)
# 土曜・市場休場。
CLOSED = datetime(2026, 7, 4, 8, 0, tzinfo=UTC)


def _bullish_tech(
    *,
    spread: float | None = 0.02,
    atr: float | None = 0.30,
    close: float = 155.0,
) -> PairTechnicals:
    """全時間足ロング目線・SL/TPを引ける健全なテクニカル。"""
    tech = PairTechnicals(symbol="USDJPY", fast_window=20, slow_window=100)
    bid = close - (spread / 2 if spread else 0.0)
    ask = close + (spread / 2 if spread else 0.0)
    tech.views["1h"] = IntervalView(
        interval="1h",
        recommendation="STRONG_BUY",
        buy=14,
        sell=1,
        neutral=2,
        close=close,
        bid=bid if spread else None,
        ask=ask if spread else None,
        spread=spread,
        rsi=58.0,
        atr=atr,
        sma_fast=close + 0.6,
        sma_slow=close - 0.2,
    )
    tech.views["4h"] = IntervalView(
        interval="4h",
        recommendation="BUY",
        buy=10,
        sell=1,
        neutral=2,
        close=close,
        sma_fast=close + 0.8,
        sma_slow=close - 0.3,
    )
    tech.views["1d"] = IntervalView(
        interval="1d",
        recommendation="BUY",
        buy=9,
        sell=1,
        neutral=3,
        close=close,
        sma_fast=close + 1.0,
        sma_slow=close - 0.5,
    )
    return tech


def _scores() -> dict[str, CurrencySentiment]:
    # USD強気・JPY弱気 → USDJPYロング寄り。tech と方向を揃える。
    return {
        "USD": CurrencySentiment("USD", score=0.4),
        "JPY": CurrencySentiment("JPY", score=-0.3),
    }


def _step(checklist, key):
    return next(s for s in checklist.steps if s.key == key)


# --- 純粋関数 ---------------------------------------------------------------


def test_estimate_expected_r_grows_with_conviction() -> None:
    low = estimate_expected_r("long", 20, target1_r=1.0)
    high = estimate_expected_r("long", 90, target1_r=1.0)
    assert high > low
    # neutral は常に0
    assert estimate_expected_r("neutral", 90, 1.0) == 0.0


def test_execution_cost_in_r() -> None:
    # SL距離0.75、スプレッド0.02、スリッページ1本 → コスト0.04価格 / 0.75 = 0.0533R
    cost = execution_cost_in_r(0.02, 0.75, slippage_spreads=1.0)
    assert cost is not None
    assert abs(cost - 0.0533) < 1e-3
    # 不明入力は None
    assert execution_cost_in_r(None, 0.75) is None
    assert execution_cost_in_r(0.02, 0.0) is None


def test_position_units() -> None:
    # 残高1,000,000・0.5%リスク=5,000円許容 / SL距離0.75 = 6,666.67単位
    units = position_units(1_000_000, 0.5, 0.75)
    assert units is not None
    assert abs(units - 6666.67) < 0.5
    # 残高不明ならサイズは発注側任せ(None)
    assert position_units(None, 0.5, 0.75) is None
    assert position_units(1_000_000, 0.5, 0.0) is None


# --- チェックリスト全体 -----------------------------------------------------


def test_clean_long_passes_all_nine_steps() -> None:
    plan, checklist = run_pipeline(
        "USDJPY",
        _bullish_tech(),
        _scores(),
        [],
        [],
        now=OPEN,
        account_balance=1_000_000,
        calibrated_win_probability=0.62,
    )
    assert plan.direction == "long"
    # 9ステップが順番どおり
    assert [s.order for s in checklist.steps] == list(range(1, 10))
    assert not checklist.blocked
    # 執行コスト控除後の純期待Rが正
    assert checklist.net_expected_r is not None and checklist.net_expected_r > 0
    # ポジションサイズが確定している
    assert checklist.position_units is not None and checklist.position_units > 0


def test_weekend_blocks_at_regime_step() -> None:
    plan, checklist = run_pipeline("USDJPY", _bullish_tech(), _scores(), [], [], now=CLOSED)
    assert plan.direction == "closed"
    assert _step(checklist, "regime").status == "block"
    assert checklist.blocked


def test_missing_atr_blocks_volatility_step() -> None:
    plan, checklist = run_pipeline("USDJPY", _bullish_tech(atr=None), _scores(), [], [], now=OPEN)
    assert _step(checklist, "volatility").status == "block"
    assert checklist.blocked


def test_huge_atr_warns_volatility() -> None:
    # close=155, atr=3.5 → 2.26% > ATR_PCT_MAX
    tech = _bullish_tech(atr=3.5)
    _, checklist = run_pipeline("USDJPY", tech, _scores(), [], [], now=OPEN)
    vol = _step(checklist, "volatility")
    assert vol.status == "warn"
    assert (3.5 / 155.0 * 100) > ATR_PCT_MAX


def test_wide_spread_blocks_at_spread_step() -> None:
    # SL距離 = atr*2.5 = 0.75。スプレッドをその25%超(=0.20)に。
    tech = _bullish_tech(spread=0.20, atr=0.30)
    _, checklist = run_pipeline("USDJPY", tech, _scores(), [], [], now=OPEN)
    spread_step = _step(checklist, "spread")
    assert spread_step.status == "block"
    assert (0.20 / 0.75) >= SPREAD_BLOCK_FRACTION
    assert checklist.blocked


def test_wide_spread_makes_net_expectancy_negative_or_blocks() -> None:
    # スプレッドが広いと執行コスト控除で純期待Rが削られる。
    tech = _bullish_tech(spread=0.20, atr=0.30)
    _, checklist = run_pipeline("USDJPY", tech, _scores(), [], [], now=OPEN)
    # 前段(spread)でblock済み。コストR自体も大きい。
    assert checklist.execution_cost_r is not None
    assert checklist.execution_cost_r > 0.2


def test_no_balance_still_ok_at_sizing() -> None:
    # 残高未指定でも、リスク%方針として ok(発注側で確定)。
    plan, checklist = run_pipeline(
        "USDJPY",
        _bullish_tech(),
        _scores(),
        [],
        [],
        now=OPEN,
        account_balance=None,
        calibrated_win_probability=0.62,
    )
    if plan.direction == "long":
        size_step = _step(checklist, "position_size")
        assert size_step.status == "ok"
        assert checklist.position_units is None


def test_realized_expectancy_overrides_theoretical() -> None:
    # 実測期待Rが非正なら期待値ステップでblock。
    _, checklist = run_pipeline(
        "USDJPY",
        _bullish_tech(),
        _scores(),
        [],
        [],
        now=OPEN,
        realized_expectancy_r=-0.3,
    )
    exp = _step(checklist, "expectancy")
    assert exp.status == "block"
    assert checklist.expected_r == -0.3


def test_uncalibrated_conviction_cannot_pass_expectancy_gate() -> None:
    _, checklist = run_pipeline("USDJPY", _bullish_tech(), _scores(), [], [], now=OPEN)

    assert _step(checklist, "expectancy").status == "block"
    assert not checklist.probability_calibrated
    assert "未較正" in checklist.expectancy_source


def test_missing_spread_blocks_cost_gate_instead_of_assuming_zero_cost() -> None:
    _, checklist = run_pipeline(
        "USDJPY",
        _bullish_tech(spread=None),
        _scores(),
        [],
        [],
        now=OPEN,
        calibrated_win_probability=0.62,
    )

    assert _step(checklist, "execution_cost").status == "block"
    assert checklist.net_expected_r is None


def test_operational_freshness_veto_forces_neutral_and_blocks_checklist() -> None:
    plan, checklist = run_pipeline(
        "USDJPY",
        _bullish_tech(),
        _scores(),
        [],
        [],
        now=OPEN,
        calibrated_win_probability=0.62,
        operational_data_ok=False,
        operational_data_reason="freshness report stale",
    )

    assert plan.direction == "neutral"
    assert plan.conviction == 0
    assert _step(checklist, "event").status == "block"
    assert "freshness report stale" in _step(checklist, "event").note


def test_checklist_serialization_roundtrip() -> None:
    _, checklist = run_pipeline("USDJPY", _bullish_tech(), _scores(), [], [], now=OPEN)
    data = checklist.to_dict()
    assert data["symbol"] == "USDJPY"
    assert len(data["steps"]) == 9
    assert all({"order", "key", "status"} <= set(step) for step in data["steps"])
