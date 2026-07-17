"""時間足別判断(fx_intel.timeframe)のテスト(ネットワーク不要)。"""

from __future__ import annotations

from datetime import datetime, timedelta, UTC

import pytest

from fx_intel.calendar import EconomicEvent, risk_windows
from fx_intel.technicals import PairTechnicals, build_interval_view
from fx_intel.timeframe import (
    AUXILIARY_HORIZON_HOURS,
    DEFAULT_TIMEFRAMES,
    PRIMARY_HORIZON_HOURS,
    TimeframePlan,
    build_timeframe_plan,
    build_timeframe_plans,
    tolerance_for,
)

# 月曜 09:00 UTC = 市場オープン中(週末クローズ近似の外)
OPEN_NOW = datetime(2026, 6, 29, 9, 0, tzinfo=UTC)
# 土曜 12:00 UTC = 週末クローズ中
WEEKEND_NOW = datetime(2026, 6, 27, 12, 0, tzinfo=UTC)


def make_view(interval: str, rec: str, **kwargs):
    summary = {"RECOMMENDATION": rec, "BUY": 10, "SELL": 5, "NEUTRAL": 11}
    indicators = {
        "close": kwargs.get("close", 150.0),
        "RSI": kwargs.get("rsi", 55.0),
        "ADX": kwargs.get("adx", 25.0),
        "ATR": kwargs.get("atr", 0.5),
        "SMA20": kwargs.get("sma_fast", 150.5),
        "SMA100": kwargs.get("sma_slow", 149.0),
    }
    return build_interval_view(interval, summary, indicators, 20, 100)


def _all_up_tech() -> PairTechnicals:
    """全時間足が買い寄りのテクニカル。"""
    tech = PairTechnicals(symbol="USDJPY")
    tech.views = {
        "15m": make_view("15m", "BUY", close=156.20, rsi=58.0, adx=22.0, atr=0.08),
        "1h": make_view("1h", "BUY", close=156.25, rsi=55.0, adx=28.0, atr=0.15),
        "4h": make_view("4h", "STRONG_BUY", close=156.30, rsi=60.0, adx=35.0, atr=0.30),
        "1d": make_view("1d", "BUY", close=156.10, rsi=52.0, adx=25.0, atr=0.80),
    }
    return tech


# ------------------------------------------------- 基本構造


def test_build_plans_one_per_timeframe_with_primary_horizon() -> None:
    plans = build_timeframe_plans(
        "USDJPY", _all_up_tech(), currency_scores={}, windows=[], news_items=[], now=OPEN_NOW
    )
    assert [p.timeframe for p in plans] == list(DEFAULT_TIMEFRAMES)
    for plan in plans:
        assert plan.horizon_hours == PRIMARY_HORIZON_HOURS[plan.timeframe]
        assert plan.auxiliary_horizons == AUXILIARY_HORIZON_HOURS[plan.timeframe]


def test_primary_horizons_match_spec() -> None:
    # ユーザー仕様: 15m→15分後 / 1h→1h / 4h→4h / 1d→24h
    assert PRIMARY_HORIZON_HOURS == {"15m": 0.25, "1h": 1.0, "4h": 4.0, "1d": 24.0}


def test_each_timeframe_carries_own_close_and_indicators() -> None:
    plans = {
        p.timeframe: p
        for p in build_timeframe_plans("USDJPY", _all_up_tech(), {}, [], [], now=OPEN_NOW)
    }
    assert plans["15m"].close == 156.20
    assert plans["4h"].close == 156.30
    assert plans["4h"].adx == 35.0
    assert plans["1d"].atr == 0.80


# ------------------------------------------------- 独立した方向判断


def test_timeframes_can_disagree() -> None:
    """短期が売り、上位足が買いなら、各足で別方向の判断になりうる。"""
    tech = PairTechnicals(symbol="USDJPY")
    tech.views = {
        # 15m は強い売り・上位足の逆行ボーナスを打ち消せないほど強い
        "15m": make_view("15m", "STRONG_SELL", close=156.2, atr=0.08),
        "1h": make_view("1h", "BUY", close=156.25, atr=0.15),
        "4h": make_view("4h", "STRONG_BUY", close=156.30, atr=0.30),
        "1d": make_view("1d", "STRONG_BUY", close=156.10, atr=0.80),
    }
    by_tf = {
        p.timeframe: p for p in build_timeframe_plans("USDJPY", tech, {}, [], [], now=OPEN_NOW)
    }
    assert by_tf["15m"].direction == "short"
    assert by_tf["4h"].direction == "long"
    assert by_tf["1d"].direction == "long"


def test_15m_shadow_analysis_records_weak_bias_below_trade_threshold() -> None:
    tech = PairTechnicals(symbol="USDJPY")
    tech.views = {
        "15m": make_view("15m", "BUY", atr=0.08),
        "1h": make_view("1h", "STRONG_SELL", atr=0.15),
        "4h": make_view("4h", "STRONG_SELL", atr=0.30),
    }

    plan = build_timeframe_plan("USDJPY", "15m", tech, {}, [], [], now=OPEN_NOW)

    assert 0.05 <= plan.composite < plan.direction_threshold
    assert plan.direction == "neutral"
    assert plan.analysis_direction == "long"
    assert plan.analysis_conviction > 0


def test_higher_timeframe_alignment_boosts_score() -> None:
    """同じ 1h BUY でも、上位足が順行だとスコア(=確信度)が上がる。"""
    aligned = PairTechnicals(symbol="USDJPY")
    aligned.views = {
        "1h": make_view("1h", "BUY", atr=0.15),
        "4h": make_view("4h", "STRONG_BUY", atr=0.3),
        "1d": make_view("1d", "STRONG_BUY", atr=0.8),
    }
    conflicted = PairTechnicals(symbol="USDJPY")
    conflicted.views = {
        "1h": make_view("1h", "BUY", atr=0.15),
        "4h": make_view("4h", "STRONG_SELL", atr=0.3),
        "1d": make_view("1d", "STRONG_SELL", atr=0.8),
    }
    plan_aligned = build_timeframe_plan("USDJPY", "1h", aligned, {}, [], [], now=OPEN_NOW)
    plan_conflicted = build_timeframe_plan("USDJPY", "1h", conflicted, {}, [], [], now=OPEN_NOW)
    assert plan_aligned.tf_score > plan_conflicted.tf_score


# ------------------------------------------------- ゲート


def test_weekend_forces_closed_on_every_timeframe() -> None:
    plans = build_timeframe_plans("USDJPY", _all_up_tech(), {}, [], [], now=WEEKEND_NOW)
    assert all(p.direction == "closed" for p in plans)
    assert all(p.conviction == 0 for p in plans)


def test_event_window_blocks_trading_but_keeps_analysis_running() -> None:
    event = EconomicEvent("FOMC", "USD", OPEN_NOW + timedelta(minutes=30), "high")
    windows = risk_windows([event], {"USD", "JPY"})
    plan = build_timeframe_plan(
        "USDJPY", "1h", _all_up_tech(), {}, windows, [], now=OPEN_NOW
    )

    assert plan.direction == "standby"
    assert plan.analysis_direction == "long"
    assert plan.analysis_conviction > 0
    event_gate = next(trace for trace in plan.gate_trace if trace["gate"] == "event_window")
    assert event_gate["event_title"] == "FOMC"
    assert event_gate["blocked_until"] == windows[0].end.isoformat()


def test_missing_timeframe_is_neutral_not_crash() -> None:
    tech = PairTechnicals(symbol="USDJPY")  # views 空 = 全時間足取得失敗
    plan = build_timeframe_plan("USDJPY", "1h", tech, {}, [], [], now=OPEN_NOW)
    assert plan.direction == "neutral"
    assert plan.close is None
    assert any("取得に失敗" in w for w in plan.warnings)


def test_expectancy_guard_can_block_timeframe_plan() -> None:
    def guard(symbol: str, direction: str, conviction: int):
        assert symbol == "USDJPY"
        assert direction == "long"
        assert conviction > 0
        return 0.4, "USDJPY:long の期待Rがマイナス", True

    plan = build_timeframe_plan(
        "USDJPY",
        "1h",
        _all_up_tech(),
        {},
        [],
        [],
        now=OPEN_NOW,
        expectancy_adjuster=guard,
    )
    assert plan.direction == "neutral"
    assert plan.conviction == 0
    assert plan.stop is None
    assert any("期待値ガード" in warning for warning in plan.warnings)


def test_timeframe_plan_uses_approved_target_r_adjuster() -> None:
    plan = build_timeframe_plan(
        "USDJPY",
        "1h",
        _all_up_tech(),
        {},
        [],
        [],
        now=OPEN_NOW,
        target_r_adjuster=lambda _symbol, _direction, _conviction: (
            0.75,
            1.5,
            "承認済み候補",
        ),
    )
    risk_distance = 0.15 * 2.5

    assert plan.direction == "long"
    assert plan.stop == pytest.approx(156.25 - risk_distance)
    assert plan.target1 == pytest.approx(156.25 + risk_distance * 0.75)
    assert plan.target2 == pytest.approx(156.25 + risk_distance * 1.5)
    assert plan.target_policy["target1_r"] == 0.75
    assert any("承認済みTP/SL" in warning for warning in plan.warnings)


def test_features_use_shared_learning_keys() -> None:
    """learning.py の FEATURE_SPECS と同じキー名で特徴量を記録する。"""
    plan = build_timeframe_plan("USDJPY", "4h", _all_up_tech(), {}, [], [], now=OPEN_NOW)
    # 4h 判断でも rsi_1h / adx_1h キーに 4h 足の値が入る(セルは timeframe で分離)
    assert plan.features["rsi_1h"] == 60.0
    assert plan.features["adx_1h"] == 35.0
    assert "rating_1d" in plan.features


def test_tolerance_scales_with_horizon() -> None:
    assert tolerance_for(0.25) < tolerance_for(24.0)
    assert tolerance_for(999.0) == 2.0  # 未知ホライズンは既定許容


def test_returns_timeframe_plan_instances() -> None:
    plans = build_timeframe_plans("USDJPY", _all_up_tech(), {}, [], [], now=OPEN_NOW)
    assert all(isinstance(p, TimeframePlan) for p in plans)
