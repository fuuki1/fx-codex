"""完全判断ログの保存テスト。ネットワーク不要。"""

from __future__ import annotations

from datetime import datetime, UTC
import json
from types import SimpleNamespace

from fx_intel import decision_log, learning, maximization, tf_learning, tp_sl_learning
from fx_intel.briefing import TradePlan
from fx_intel.sentiment import CurrencySentiment, MarketAnalysis
from fx_intel.technicals import PairTechnicals, build_interval_view
from fx_intel.timeframe import TimeframePlan

NOW = datetime(2026, 7, 8, 8, 0, tzinfo=UTC)


def _analysis() -> MarketAnalysis:
    return MarketAnalysis(
        currencies={
            "USD": CurrencySentiment("USD", score=0.3, headline_count=2),
            "JPY": CurrencySentiment("JPY", score=-0.2, headline_count=1),
        },
        regime="neutral",
        summary="test summary",
        engine="lexicon",
    )


def _tech() -> PairTechnicals:
    tech = PairTechnicals(symbol="USDJPY")
    tech.views["1h"] = build_interval_view(
        "1h",
        {"RECOMMENDATION": "BUY", "BUY": 10, "SELL": 2, "NEUTRAL": 4},
        {
            "close": 150.0,
            "RSI": 55.0,
            "ADX": 28.0,
            "ATR": 0.2,
            "SMA20": 150.2,
            "SMA100": 149.8,
        },
        20,
        100,
    )
    return tech


def _plan() -> TimeframePlan:
    return TimeframePlan(
        symbol="USDJPY",
        timeframe="1h",
        horizon_hours=1.0,
        direction="long",
        conviction=72,
        tf_score=0.5,
        news_score=0.2,
        composite=0.37,
        close=150.0,
        atr=0.2,
        rsi=55.0,
        adx=28.0,
        stop=149.5,
        target1=150.5,
        target2=151.0,
        data_quality=0.9,
        features={"rsi_1h": 55.0, "news_count": 2.0},
        components=[{"key": "tech", "score": 0.5, "weight": 0.55}],
        reason="1hレーティング 買い",
        warnings=["📈 期待値ガード: テスト"],
    )


def test_build_timeframe_decision_event_persists_full_context(tmp_path) -> None:
    learned = learning.LearnedProfile(generated_at=NOW.isoformat(), evaluated=12, hits=7)
    tf_learn = tf_learning.TimeframeLearning(
        generated_at=NOW.isoformat(),
        profiles={("USDJPY", "1h"): learned},
    )
    tp_profile = tp_sl_learning.TpSlProfile(
        generated_at=NOW.isoformat(),
        evaluated=30,
        hits=20,
    )
    tp_sl = tp_sl_learning.TimeframeTpSlLearning(
        generated_at=NOW.isoformat(),
        profiles={("USDJPY", "1h"): tp_profile},
    )
    max_cell = maximization.MaximizationCell(
        symbol="USDJPY",
        timeframe="1h",
        direction="long",
        tradable=100,
        expectancy_r=0.2,
        score=0.4,
        action="boost",
        factor=1.05,
        reason_ja="強いセル",
    )
    max_profile = maximization.TimeframeMaximization(
        generated_at=NOW.isoformat(),
        cells={("USDJPY", "1h", "long"): max_cell},
    )

    macro_snapshot = SimpleNamespace(
        cot_evidence={
            "status": "ok",
            "dataset_id": "b" * 64,
            "prediction_time": NOW.isoformat(),
        },
        cot={
            "JPY": SimpleNamespace(
                report_date="2026-07-07",
                available_time=NOW,
                source_record_id="source-JPY",
                content_hash="a" * 64,
                dataset_id="b" * 64,
                data_quality_flags=("publication_time_attested_locally",),
            )
        },
    )
    events = decision_log.build_timeframe_decision_events(
        {"USDJPY": [_plan()]},
        now=NOW,
        analysis=_analysis(),
        tech_map={"USDJPY": _tech()},
        macro_snapshot=macro_snapshot,
        timeframe_learning=tf_learn,
        tp_sl_learning=tp_sl,
        maximization_profile=max_profile,
        expectancy_summaries={"1h": {"overall": {"expectancy_r": 0.2}}},
    )

    assert len(events) == 1
    event = events[0]
    assert event["decision_id"]
    assert event["decision"]["target2"] == 151.0
    assert event["audit"]["scoring_ready"] is True
    assert event["technical_context"]["views"]["1h"]["atr"] == 0.2
    assert event["market_context"]["currency_sentiment"]["USD"]["score"] == 0.3
    macro_evidence = event["market_context"]["macro_evidence"]
    assert macro_evidence["cot_evidence"]["dataset_id"] == "b" * 64
    assert macro_evidence["cot_reports"]["JPY"]["source_record_id"] == "source-JPY"
    assert "COT-only" in macro_evidence["scope"]
    assert event["learning_context"]["timeframe_learning"]["evaluated"] == 12
    assert event["learning_context"]["tp_sl_learning"]["evaluated"] == 30
    assert event["learning_context"]["maximization"]["active_cell"]["action"] == "boost"

    jsonl = tmp_path / "decisions.jsonl"
    latest = tmp_path / "latest.json"
    decision_log.append_decision_events(jsonl, events)
    decision_log.save_latest_snapshot(latest, events, now=NOW)

    rows = [json.loads(line) for line in jsonl.read_text(encoding="utf-8").splitlines()]
    payload = json.loads(latest.read_text(encoding="utf-8"))
    assert rows[0]["decision_id"] == event["decision_id"]
    assert payload["event_count"] == 1
    assert payload["events"][0]["decision"]["direction"] == "long"


def test_score_decision_events_uses_tp_sl_mfe_mae(tmp_path) -> None:
    plan = _plan()
    events = decision_log.build_timeframe_decision_events(
        {"USDJPY": [plan]},
        now=NOW,
        analysis=_analysis(),
        tech_map={"USDJPY": _tech()},
    )
    price_rows = [
        {
            "ts": "2026-07-08T08:30:00+00:00",
            "symbol": "USDJPY",
            "timeframe": "1h",
            "close": 150.6,
            "high": 150.7,
            "low": 150.1,
        },
        {
            "ts": "2026-07-08T09:00:00+00:00",
            "symbol": "USDJPY",
            "timeframe": "1h",
            "close": 150.8,
            "high": 150.9,
            "low": 150.3,
        },
    ]

    report = decision_log.score_decision_events(events, price_entries=price_rows, now=NOW)
    path = tmp_path / "outcomes.json"
    decision_log.save_outcome_report(report, path)

    payload = json.loads(path.read_text(encoding="utf-8"))
    outcome = payload["outcomes"][0]
    assert payload["scoring_method"] == "tp_sl_mfe_mae_first_touch"
    assert payload["summary"]["overall"]["tradable"] == 1
    assert outcome["score_label"] == "tp_hit"
    assert outcome["first_touch"] == "tp1"
    assert outcome["realized_r"] == 1.0
    # Excursions are measured only while the trade is open; the later bar must
    # not improve MFE after TP1 has already closed the position.
    assert outcome["mfe_r"] == 1.2
    assert outcome["mae_r"] == 0.0
    assert outcome["decision_id"] == events[0]["decision_id"]


def test_score_decision_events_classifies_failure_reasons() -> None:
    plan = _plan()
    plan.conviction = 82
    plan.tf_score = 0.6
    plan.news_score = -0.5
    plan.data_quality = 0.6
    plan.features.update(
        {
            "rating_4h": -0.6,
            "rating_1d": -0.4,
            "rsi_1h": 70.0,
            "adx_1h": 15.0,
            "tf_agreement": 0.25,
        }
    )
    events = decision_log.build_timeframe_decision_events(
        {"USDJPY": [plan]},
        now=NOW,
        analysis=_analysis(),
        tech_map={"USDJPY": _tech()},
    )
    price_rows = [
        {
            "ts": "2026-07-08T08:15:00+00:00",
            "symbol": "USDJPY",
            "timeframe": "1h",
            "close": 149.5,
            "high": 150.2,
            "low": 149.4,
        },
        {
            "ts": "2026-07-08T08:30:00+00:00",
            "symbol": "USDJPY",
            "timeframe": "1h",
            "close": 149.6,
            "high": 149.8,
            "low": 149.3,
        },
        {
            "ts": "2026-07-08T09:00:00+00:00",
            "symbol": "USDJPY",
            "timeframe": "1h",
            "close": 149.7,
            "high": 149.9,
            "low": 149.5,
        },
    ]

    report = decision_log.score_decision_events(events, price_entries=price_rows, now=NOW)
    outcome = report["outcomes"][0]
    keys = {reason["key"] for reason in outcome["failure_reasons"]}

    assert outcome["score_label"] == "sl_hit"
    assert outcome["primary_failure_reason"] == "sl_first"
    assert {
        "sl_first",
        "adverse_excursion_dominant",
        "weak_favorable_excursion",
        "large_adverse_excursion",
        "confidence_overreach",
        "htf_against_4h",
        "htf_against_1d",
        "rsi_extreme_follow",
        "tech_news_conflict",
        "range_trend_call",
        "weak_tf_agreement",
        "low_data_quality",
    } <= keys
    assert report["failure_reason_summary"][0]["key"] == "sl_first"


def test_fusion_decision_persists_execution_cost_for_scoring() -> None:
    """判断時に確定した執行コスト(R換算)と期待R予測を判断ログへ保存し、

    採点スキーマへ引き継ぐ。realized_net_r 生成の入力になる。"""
    plan = TradePlan(
        symbol="USDJPY",
        direction="long",
        conviction=60,
        composite=0.3,
        tech_score=0.4,
        news_score=0.2,
    )
    # build_checklist.to_dict() 相当のコスト系フィールドを持たせる
    plan.checklist = {
        "execution_cost_r": 0.13,
        "net_expected_r": 0.42,
        "expected_r": 0.55,
        "expectancy_source": "test",
        "probability_calibrated": True,
    }
    events = decision_log.build_fusion_decision_events(
        [plan],
        now=NOW,
        analysis=_analysis(),
        tech_map={"USDJPY": _tech()},
    )
    execution = events[0]["decision"]["execution"]
    assert execution["execution_cost_r"] == 0.13
    assert execution["net_expected_r"] == 0.42
    assert execution["expected_r"] == 0.55

    scoring = decision_log.decision_event_to_scoring_entry(events[0])
    assert scoring is not None
    assert scoring["execution_cost_r"] == 0.13
    assert scoring["net_expected_r"] == 0.42


def test_fusion_decision_persists_null_execution_without_checklist() -> None:
    """checklist が無い plan は execution を全て None で保存(採点側が欠損として扱える)。"""
    plan = TradePlan(
        symbol="USDJPY",
        direction="long",
        conviction=20,
        composite=0.1,
        tech_score=0.1,
        news_score=0.1,
    )
    events = decision_log.build_fusion_decision_events(
        [plan],
        now=NOW,
        analysis=_analysis(),
        tech_map={"USDJPY": _tech()},
    )
    execution = events[0]["decision"]["execution"]
    assert execution["execution_cost_r"] is None
    assert execution["net_expected_r"] is None
