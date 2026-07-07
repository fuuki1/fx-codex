"""Detailed notice feedback profile tests."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, UTC

from fx_intel import notice_feedback as nf
from fx_intel.briefing import TradePlan
from fx_intel.calendar import EconomicEvent
from fx_intel.market_structure import EntryLevels
from fx_intel.notice_quality import (
    ENTRY_CHECK_TRIGGERED,
    ENTRY_SCENARIO_BREAKOUT,
    ENTRY_SCENARIO_PULLBACK,
    NoticeQualityOutcome,
    OUTCOME_HIT,
    OUTCOME_MISS,
)
from fx_intel.sentiment import CurrencySentiment, MarketAnalysis
from fx_intel.technicals import PairTechnicals, build_interval_view
from fx_intel.trade_notice import build_detailed_notice

NOW = datetime(2026, 7, 6, 12, 0, tzinfo=UTC)


def _entry(symbol: str = "USDJPY", conviction: int = 52, source: str = "recent_ohlc") -> dict:
    return {
        "symbol": symbol,
        "direction": "long",
        "conviction": conviction,
        "entry_level_source": {"source": source},
        "important_event": {"title": "ISM Services PMI"},
        "no_entry_window": {"start": "2026-07-06T13:30:00+00:00"},
    }


def _outcome(
    outcome: str,
    *,
    entry_check: str = "",
    entry_scenario: str = "",
) -> NoticeQualityOutcome:
    return NoticeQualityOutcome(
        "USDJPY",
        NOW,
        "long",
        outcome,
        entry_check=entry_check,
        entry_scenario=entry_scenario,
    )


def _notice_and_levels():
    summary = {"RECOMMENDATION": "BUY", "BUY": 12, "SELL": 5, "NEUTRAL": 9}
    indicators = {
        "close": 162.296,
        "RSI": 57.0,
        "ADX": 24.0,
        "ATR": 0.153,
        "SMA20": 162.20,
        "SMA100": 162.40,
    }
    tech = PairTechnicals(symbol="USDJPY")
    tech.views = {
        tf: build_interval_view(tf, summary, indicators, 20, 100)
        for tf in ("15m", "1h", "4h", "1d")
    }
    plan = TradePlan(
        symbol="USDJPY",
        direction="long",
        conviction=52,
        composite=0.52,
        tech_score=0.55,
        news_score=0.1,
        close=162.296,
        atr=0.153,
        stop=161.914,
        target1=162.678,
        target2=163.060,
    )
    analysis = MarketAnalysis(
        engine="analyst",
        regime="neutral",
        currencies={
            "USD": CurrencySentiment("USD", score=0.2),
            "JPY": CurrencySentiment("JPY", score=-0.1),
        },
    )
    event = EconomicEvent("ISM Services PMI", "USD", NOW + timedelta(hours=2), "high")
    levels = EntryLevels(
        "USDJPY",
        "long",
        162.20,
        162.30,
        162.35,
        162.45,
        162.20,
        162.45,
        162.10,
        162.50,
        "recent_ohlc",
        48,
    )
    return (
        build_detailed_notice(plan, tech, analysis, [event], now=NOW, entry_levels=levels),
        levels,
    )


def test_build_feedback_profile_marks_weak_conditions_after_sample_guard() -> None:
    entries = [_entry() for _ in range(5)]
    outcomes = [_outcome(OUTCOME_MISS) for _ in range(4)] + [_outcome(OUTCOME_HIT)]

    profile = nf.build_feedback_profile(entries, outcomes, now=NOW, min_evaluated=5)

    assert profile.total == 5
    assert profile.evaluated == 5
    assert profile.hit_rate == 0.2
    assert profile.cells["symbol:USDJPY"].factor == 0.7
    assert "symbol:USDJPY" in profile.weak_keys
    assert "弱い条件" in nf.format_profile_ja(profile)


def test_build_feedback_profile_does_not_mark_small_samples() -> None:
    entries = [_entry() for _ in range(4)]
    outcomes = [_outcome(OUTCOME_MISS) for _ in range(4)]

    profile = nf.build_feedback_profile(entries, outcomes, now=NOW, min_evaluated=5)

    assert profile.weak_keys == []
    assert all(cell.factor == 1.0 for cell in profile.cells.values())


def test_save_load_profile_round_trips(tmp_path) -> None:
    profile = nf.build_feedback_profile(
        [_entry() for _ in range(5)],
        [_outcome(OUTCOME_MISS) for _ in range(5)],
        now=NOW,
        min_evaluated=5,
    )
    path = tmp_path / "feedback.json"

    nf.save_profile(profile, path)
    loaded = nf.load_profile(path)

    assert loaded.generated_at == profile.generated_at
    assert loaded.cells["overall"].factor == 0.7


def test_apply_feedback_to_notice_adds_matching_warning() -> None:
    notice, levels = _notice_and_levels()
    profile = nf.build_feedback_profile(
        [_entry() for _ in range(5)],
        [_outcome(OUTCOME_MISS) for _ in range(5)],
        now=NOW,
        min_evaluated=5,
    )

    adjusted = nf.apply_feedback_to_notice(notice, profile, levels)

    assert adjusted is not notice
    assert any("詳細通知学習" in item for item in adjusted.caution_factors)
    assert any("詳細通知学習" in item for item in adjusted.warnings)


def test_build_feedback_profile_tracks_entry_scenario_cells() -> None:
    entries = [_entry() for _ in range(5)]
    outcomes = [
        _outcome(
            OUTCOME_MISS,
            entry_check=ENTRY_CHECK_TRIGGERED,
            entry_scenario=ENTRY_SCENARIO_BREAKOUT,
        )
        for _ in range(4)
    ] + [
        _outcome(
            OUTCOME_HIT,
            entry_check=ENTRY_CHECK_TRIGGERED,
            entry_scenario=ENTRY_SCENARIO_BREAKOUT,
        )
    ]

    profile = nf.build_feedback_profile(entries, outcomes, now=NOW, min_evaluated=5)

    scenario = profile.cells[f"entry_scenario:{ENTRY_SCENARIO_BREAKOUT}"]
    assert scenario.evaluated == 5
    assert scenario.hit_rate == 0.2
    assert scenario.factor == 0.7
    assert f"entry_scenario:{ENTRY_SCENARIO_BREAKOUT}" in profile.weak_keys
    assert profile.cells[f"entry_trigger:{ENTRY_CHECK_TRIGGERED}"].evaluated == 5


def test_apply_feedback_to_notice_matches_planned_entry_scenario() -> None:
    notice, levels = _notice_and_levels()
    cell = nf.FeedbackCell(
        key=f"entry_scenario:{ENTRY_SCENARIO_BREAKOUT}",
        label_ja=nf.ENTRY_SCENARIO_LABELS[ENTRY_SCENARIO_BREAKOUT],
        total=5,
        evaluated=5,
        hits=0,
        misses=5,
        factor=0.7,
    )
    profile = nf.NoticeFeedbackProfile(
        generated_at=NOW.isoformat(),
        total=5,
        evaluated=5,
        hits=0,
        cells={cell.key: cell},
        weak_keys=[cell.key],
    )

    adjusted = nf.apply_feedback_to_notice(notice, profile, levels)

    assert any("ブレイク維持" in item for item in adjusted.caution_factors)
    assert any("詳細通知学習" in item for item in adjusted.warnings)


def test_apply_feedback_to_notice_adds_expectancy_warning() -> None:
    notice, levels = _notice_and_levels()
    profile = nf.NoticeFeedbackProfile()
    expectancy = {
        "by_symbol": {
            "USDJPY": {
                "tradable": 20,
                "min_samples": 20,
                "sample_ok": True,
                "expectancy_r": -0.2,
                "profit_factor_r": 0.8,
                "avg_mfe_r": 0.7,
                "avg_mae_r": 0.9,
            }
        }
    }

    adjusted = nf.apply_feedback_to_notice(
        notice,
        profile,
        levels,
        expectancy_summary=expectancy,
    )

    assert any("期待値学習" in item and "非正" in item for item in adjusted.caution_factors)
    assert any("期待値学習" in item for item in adjusted.warnings)
    assert adjusted.conviction == round(notice.conviction * nf.EXPECTANCY_BLOCK_FACTOR)
    assert adjusted.priority == "期待値ガードを優先し、新規エントリーは見送り"
    assert "見送り優先" in adjusted.final_evaluation
    assert adjusted.final_actions[0].startswith("期待値ガード")


def test_apply_feedback_does_not_double_apply_existing_expectancy_guard() -> None:
    notice, levels = _notice_and_levels()
    guarded = replace(
        notice,
        warnings=["📉 期待値ガード: 通貨ペア USDJPYの期待Rは-0.20Rで非正"],
        caution_factors=[
            *notice.caution_factors,
            "📉 期待値ガード: 通貨ペア USDJPYの期待Rは-0.20Rで非正",
        ],
    )
    profile = nf.NoticeFeedbackProfile()
    expectancy = {
        "by_symbol": {
            "USDJPY": {
                "tradable": 20,
                "min_samples": 20,
                "sample_ok": True,
                "expectancy_r": -0.2,
            }
        }
    }

    adjusted = nf.apply_feedback_to_notice(
        guarded,
        profile,
        levels,
        expectancy_summary=expectancy,
    )

    assert adjusted.conviction == guarded.conviction
    assert adjusted.final_actions == guarded.final_actions
    assert adjusted.warnings.count(guarded.warnings[0]) == 1


def test_expectancy_warning_marks_sample_guard() -> None:
    notice, _levels = _notice_and_levels()
    expectancy = {
        "by_direction": {
            "long": {
                "tradable": 6,
                "min_samples": 20,
                "sample_ok": False,
                "expectancy_r": 0.3,
            }
        }
    }

    warnings = nf.expectancy_warnings_for_notice(notice, expectancy)
    adjustment = nf.expectancy_adjustment_for_notice(notice, expectancy)

    assert warnings
    assert "サンプル不足" in warnings[0]
    assert adjustment["factor"] == nf.EXPECTANCY_WEAK_FACTOR


def test_condition_keys_include_stable_notice_dimensions() -> None:
    keys = dict(nf.condition_keys_for_entry(_entry(conviction=70, source="atr_fallback")))

    assert keys["overall"] == "全詳細通知"
    assert keys["symbol:USDJPY"] == "通貨ペア USDJPY"
    assert keys["direction:long"] == "方向 long"
    assert keys["conviction:70-101"] == "確信度 70〜100"
    assert keys["entry_source:atr_fallback"] == "エントリー根拠 atr_fallback"
    assert keys["event:present"] == "重要イベントあり"
    assert keys["no_entry_window:present"] == "新規禁止時間あり"


def test_planned_scenario_keys_for_notice_are_stable() -> None:
    notice, _levels = _notice_and_levels()
    keys = dict(nf.planned_scenario_keys_for_notice(notice))

    assert keys[f"entry_scenario:{ENTRY_SCENARIO_PULLBACK}"] == "エントリー条件 押し目/戻り売り確認"
    assert keys[f"entry_scenario:{ENTRY_SCENARIO_BREAKOUT}"] == "エントリー条件 ブレイク維持"
