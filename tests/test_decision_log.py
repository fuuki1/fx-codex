"""完全判断ログの保存テスト。ネットワーク不要。"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, UTC
import json

import pytest

from fx_intel import decision_log, learning, maximization, tf_learning, tp_sl_learning
from fx_intel.append_only import (
    AppendOnlyReadError,
    AppendOnlyWriteError,
    canonical_row_hash,
)
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
        action="long",
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

    events = decision_log.build_timeframe_decision_events(
        {"USDJPY": [_plan()]},
        now=NOW,
        analysis=_analysis(),
        tech_map={"USDJPY": _tech()},
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
    assert event["learning_context"]["timeframe_learning"]["evaluated"] == 12
    assert event["learning_context"]["tp_sl_learning"]["evaluated"] == 30
    assert event["learning_context"]["maximization"]["active_cell"]["action"] == "boost"

    jsonl = tmp_path / "decisions.jsonl"
    latest = tmp_path / "latest.json"
    decision_log.append_decision_events(jsonl, events)
    decision_log.append_decision_events(jsonl, events)
    decision_log.save_latest_snapshot(latest, events, now=NOW)

    rows = [json.loads(line) for line in jsonl.read_text(encoding="utf-8").splitlines()]
    payload = json.loads(latest.read_text(encoding="utf-8"))
    assert rows[0]["decision_id"] == event["decision_id"]
    assert rows[0]["content_hash"]
    assert len(rows) == 1
    assert payload["event_count"] == 1
    assert payload["events"][0]["decision"]["direction"] == "long"

    conflicting = dict(event)
    conflicting["source"] = "competing-writer"
    with pytest.raises(
        AppendOnlyWriteError,
        match="batch identities/content|conflicting append",
    ):
        decision_log.append_decision_events(jsonl, [conflicting])


@pytest.mark.parametrize(
    ("content", "message"),
    [
        (
            json.dumps(
                {
                    "schema_version": 2,
                    "content_hash": "0" * 64,
                    "ts": NOW.isoformat(),
                }
            ),
            "content_hash mismatch",
        ),
        ("{broken", "malformed JSONL"),
        (
            json.dumps(
                {
                    "ts": "2026-07-08T08:00:00",
                    "content_hash": canonical_row_hash({"ts": "2026-07-08T08:00:00"}),
                }
            ),
            "timestamp is naive",
        ),
    ],
)
def test_strict_decision_reader_rejects_hash_malformed_and_naive(
    tmp_path, content, message
) -> None:
    path = tmp_path / "decisions.jsonl"
    path.write_text(content + "\n", encoding="utf-8")

    with pytest.raises(AppendOnlyReadError, match=message):
        list(decision_log.read_decision_events(path, as_of=NOW))


def test_strict_decision_reader_rejects_future_row(tmp_path) -> None:
    path = tmp_path / "decisions.jsonl"
    row = {"ts": (NOW + timedelta(seconds=1)).isoformat()}
    row["content_hash"] = canonical_row_hash(row)
    path.write_text(
        json.dumps(row) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(AppendOnlyReadError, match="future row"):
        list(decision_log.read_decision_events(path, as_of=NOW))


def test_no_trade_action_is_separate_from_signal_and_not_scored_as_trade(tmp_path) -> None:
    plan = _plan()
    plan.action = "no_trade"
    events = decision_log.build_timeframe_decision_events(
        {"USDJPY": [plan]},
        now=NOW,
        analysis=_analysis(),
        tech_map={"USDJPY": _tech()},
    )
    event = events[0]

    assert event["decision"]["direction"] == "long"
    assert event["decision"]["action"] == "no_trade"
    assert event["audit"]["scoring_ready"] is False
    scoring = decision_log.decision_event_to_scoring_entry(event)
    assert scoring is not None
    assert scoring["direction"] == "no_trade"
    assert scoring["action"] == "no_trade"
    assert scoring["signal_direction"] == "long"

    latest = tmp_path / "latest.json"
    decision_log.save_latest_snapshot(latest, events, now=NOW)
    payload = json.loads(latest.read_text(encoding="utf-8"))
    assert payload["action_counts"] == {"no_trade": 1}


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

    report = decision_log.score_decision_events(
        events, price_entries=price_rows, now=NOW + timedelta(hours=1)
    )
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

    report = decision_log.score_decision_events(
        events, price_entries=price_rows, now=NOW + timedelta(hours=1)
    )
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


def test_decision_retry_twenty_seconds_later_is_idempotent(tmp_path) -> None:
    run_slot = NOW
    first = decision_log.build_timeframe_decision_events(
        {"USDJPY": [_plan()]},
        now=NOW + timedelta(minutes=4, seconds=50),
        run_slot=run_slot,
        analysis=_analysis(),
        tech_map={"USDJPY": _tech()},
    )
    retry = decision_log.build_timeframe_decision_events(
        {"USDJPY": [_plan()]},
        now=NOW + timedelta(minutes=5, seconds=10),
        run_slot=run_slot,
        analysis=_analysis(),
        tech_map={"USDJPY": _tech()},
    )

    assert retry[0]["run_id"] == first[0]["run_id"]
    assert retry[0]["decision_id"] == first[0]["decision_id"]
    path = tmp_path / "decisions.jsonl"
    decision_log.append_decision_events(path, first)
    decision_log.append_decision_events(path, retry)

    rows = list(decision_log.read_decision_events(path, as_of=NOW + timedelta(minutes=6)))
    assert len(rows) == 1


def test_direction_change_in_same_run_slot_conflicts_on_natural_identity(tmp_path) -> None:
    first_plan = _plan()
    retry_plan = _plan()
    retry_plan.direction = "short"
    retry_plan.action = "no_trade"
    first = decision_log.build_timeframe_decision_events(
        {"USDJPY": [first_plan]},
        now=NOW + timedelta(seconds=1),
        run_slot=NOW,
        analysis=_analysis(),
        tech_map={"USDJPY": _tech()},
    )
    retry = decision_log.build_timeframe_decision_events(
        {"USDJPY": [retry_plan]},
        now=NOW + timedelta(seconds=21),
        run_slot=NOW,
        analysis=_analysis(),
        tech_map={"USDJPY": _tech()},
    )

    assert retry[0]["run_id"] == first[0]["run_id"]
    assert retry[0]["decision_id"] == first[0]["decision_id"]
    path = tmp_path / "decisions.jsonl"
    decision_log.append_decision_events(path, first)
    with pytest.raises(AppendOnlyWriteError, match="conflicting append"):
        decision_log.append_decision_events(path, retry)
    assert len(list(decision_log.read_decision_events(path, as_of=NOW + timedelta(minutes=1)))) == 1


def test_append_rejects_forged_decision_id_for_same_natural_cell(tmp_path) -> None:
    events = decision_log.build_timeframe_decision_events(
        {"USDJPY": [_plan()]},
        now=NOW,
        run_slot=NOW,
        analysis=_analysis(),
        tech_map={"USDJPY": _tech()},
    )
    forged = deepcopy(events[0])
    forged["decision_id"] = "forged-distinct-id"
    forged["decision"]["direction"] = "short"

    path = tmp_path / "decisions.jsonl"
    decision_log.append_decision_events(path, events)
    with pytest.raises(AppendOnlyWriteError, match="decision_id does not match"):
        decision_log.append_decision_events(path, [forged])

    loaded = list(decision_log.read_decision_events(path, as_of=NOW))
    assert len(loaded) == 1
    assert loaded[0]["decision"]["direction"] == "long"


def test_reader_rejects_forged_natural_identity_even_with_valid_content_hash(tmp_path) -> None:
    event = decision_log.build_timeframe_decision_events(
        {"USDJPY": [_plan()]},
        now=NOW,
        run_slot=NOW,
        analysis=_analysis(),
        tech_map={"USDJPY": _tech()},
    )[0]
    event["decision_id"] = "forged-distinct-id"
    event["content_hash"] = canonical_row_hash(event)
    path = tmp_path / "decisions.jsonl"
    path.write_text(json.dumps(event) + "\n", encoding="utf-8")

    with pytest.raises(AppendOnlyReadError, match="decision_id does not match"):
        list(decision_log.read_decision_events(path, as_of=NOW))


def test_reader_rejects_batch_id_not_bound_to_current_decision_content(tmp_path) -> None:
    event = decision_log.build_timeframe_decision_events(
        {"USDJPY": [_plan()]},
        now=NOW,
        run_slot=NOW,
        analysis=_analysis(),
        tech_map={"USDJPY": _tech()},
    )[0]
    original_batch = event["notification_batch_id"]
    event["decision"]["direction"] = "short"
    event["decision"]["action"] = "no_trade"
    event["content_hash"] = canonical_row_hash(event)
    assert event["notification_batch_id"] == original_batch
    path = tmp_path / "decisions.jsonl"
    path.write_text(json.dumps(event) + "\n", encoding="utf-8")

    with pytest.raises(AppendOnlyReadError, match="batch identities/content"):
        list(decision_log.read_decision_events(path, as_of=NOW))


def test_notification_batch_keeps_distinct_symbol_timeframe_cells() -> None:
    second = _plan()
    second.timeframe = "4h"
    second.horizon_hours = 4.0
    eurusd = _plan()
    eurusd.symbol = "EURUSD"
    events = decision_log.build_timeframe_decision_events(
        {"USDJPY": [_plan(), second], "EURUSD": [eurusd]},
        now=NOW,
        analysis=_analysis(),
        tech_map={"USDJPY": _tech(), "EURUSD": _tech()},
    )

    assert len({event["decision_id"] for event in events}) == 3
    assert len({event["notification_batch_id"] for event in events}) == 1


def test_scheduled_decisions_five_minutes_apart_have_distinct_identities(tmp_path) -> None:
    first_plan = _plan()
    second_plan = _plan()
    second_plan.close = 151.25
    first = decision_log.build_timeframe_decision_events(
        {"USDJPY": [first_plan]},
        now=NOW,
        analysis=_analysis(),
        tech_map={"USDJPY": _tech()},
    )
    second = decision_log.build_timeframe_decision_events(
        {"USDJPY": [second_plan]},
        now=NOW + timedelta(minutes=5),
        analysis=_analysis(),
        tech_map={"USDJPY": _tech()},
    )

    assert second[0]["run_id"] != first[0]["run_id"]
    assert second[0]["decision_id"] != first[0]["decision_id"]
    path = tmp_path / "decisions.jsonl"
    decision_log.append_decision_events(path, first)
    decision_log.append_decision_events(path, second)
    assert len(list(decision_log.read_decision_events(path, as_of=NOW + timedelta(minutes=6)))) == 2


def test_decision_build_and_score_reject_naive_clock() -> None:
    naive = NOW.replace(tzinfo=None)
    with pytest.raises(ValueError, match="timezone-aware"):
        decision_log.build_timeframe_decision_events(
            {"USDJPY": [_plan()]},
            now=naive,
            analysis=_analysis(),
            tech_map={"USDJPY": _tech()},
        )

    events = decision_log.build_timeframe_decision_events(
        {"USDJPY": [_plan()]},
        now=NOW,
        analysis=_analysis(),
        tech_map={"USDJPY": _tech()},
    )
    with pytest.raises(ValueError, match="timezone-aware"):
        decision_log.score_decision_events(events, now=naive)


def test_score_decision_events_rejects_future_decision_event() -> None:
    events = decision_log.build_timeframe_decision_events(
        {"USDJPY": [_plan()]},
        now=NOW + timedelta(seconds=1),
        analysis=_analysis(),
        tech_map={"USDJPY": _tech()},
    )

    with pytest.raises(ValueError, match="future decision event ts"):
        decision_log.score_decision_events(events, now=NOW)


def test_score_decision_events_rejects_future_price_row() -> None:
    events = decision_log.build_timeframe_decision_events(
        {"USDJPY": [_plan()]},
        now=NOW,
        analysis=_analysis(),
        tech_map={"USDJPY": _tech()},
    )
    future_price = {
        "ts": (NOW + timedelta(seconds=1)).isoformat(),
        "symbol": "USDJPY",
        "timeframe": "1h",
        "close": 150.1,
    }

    with pytest.raises(ValueError, match="future price row ts"):
        decision_log.score_decision_events(events, price_entries=[future_price], now=NOW)


def test_fusion_decision_preserves_plan_horizon_for_scoring() -> None:
    plan = TradePlan(
        symbol="USDJPY",
        direction="long",
        action="no_trade",
        conviction=60,
        composite=0.3,
        tech_score=0.4,
        news_score=0.2,
        horizon_hours=4.0,
    )
    events = decision_log.build_fusion_decision_events(
        [plan],
        now=NOW,
        analysis=_analysis(),
        tech_map={"USDJPY": _tech()},
    )

    assert events[0]["horizon_hours"] == 4.0
    assert events[0]["decision"]["horizon_hours"] == 4.0
    scoring = decision_log.decision_event_to_scoring_entry(events[0])
    assert scoring is not None
    assert scoring["horizon_hours"] == 4.0
