from datetime import datetime, timedelta, UTC

from fx_intel import decision_log, promotion
from fx_intel.shadow_learning import (
    SHADOW_LABEL_PROVENANCE,
    assign_prediction_ids,
    build_shadow_predictions,
    prediction_draft,
    summarize_shadow_outcomes,
    summarize_outcome_dimensions,
)
from fx_intel.sentiment import MarketAnalysis
from fx_intel.timeframe import TimeframePlan

NOW = datetime(2026, 7, 8, 8, 0, tzinfo=UTC)
DIMENSIONS = {"session_bucket": "london", "regime": "risk_off"}


def _prediction(score: float = 0.12) -> dict[str, object]:
    return build_shadow_predictions(
        [prediction_draft("timeframe_raw", score)],
        close=150.0,
        atr=0.2,
        entry_bid=149.99,
        entry_ask=150.01,
        quote_observed_at=NOW.isoformat(),
        cost_model_id="test-quotes-v1",
        slippage_r=0.0,
        commission_r=0.0,
        atr_multiple=2.5,
        production_threshold=0.15,
        horizon_hours=1.0,
        blocked_by=["below_production_threshold"],
        market_open=True,
        learning_dimensions=DIMENSIONS,
    )[0]


def test_below_threshold_prediction_freezes_counterfactual_levels() -> None:
    row = _prediction()
    assert row["direction"] == "long"
    assert row["blocked_by"] == ["below_production_threshold"]
    assert row["stop"] == 149.5
    assert row["target1"] == 150.5
    assert row["eligible_for_production_training"] is False
    assert row["net_label_input_eligible"] is True
    identified = assign_prediction_ids([row], "decision-1")
    assert identified[0]["prediction_id"] == "decision-1:timeframe_raw:score-v1"


def test_deadband_and_closed_predictions_fail_closed() -> None:
    row = build_shadow_predictions(
        [prediction_draft("fusion_raw", 0.01)],
        close=150.0,
        atr=0.2,
        entry_bid=149.99,
        entry_ask=150.01,
        quote_observed_at=NOW.isoformat(),
        cost_model_id="test",
        slippage_r=0.0,
        commission_r=0.0,
        atr_multiple=2.5,
        production_threshold=0.15,
        horizon_hours=24.0,
        blocked_by=["market_closed"],
        market_open=False,
        learning_dimensions=DIMENSIONS,
    )[0]
    assert row["abstained"] is True
    assert row["direction"] == "neutral"
    assert row["eligible_for_scoring"] is False
    assert {"market_closed", "shadow_abstained"} <= set(row["missing_reasons"])


def test_neutral_final_decision_matures_shadow_with_canonical_net_r() -> None:
    plan = TimeframePlan(
        symbol="USDJPY",
        timeframe="1h",
        horizon_hours=1.0,
        direction="neutral",
        conviction=12,
        tf_score=0.12,
        news_score=0.0,
        composite=0.12,
        close=150.0,
        atr=0.2,
        entry_bid=149.99,
        entry_ask=150.01,
        quote_observed_at=NOW.isoformat(),
        cost_model_id="test-quotes-v1",
        learning_dimensions=DIMENSIONS,
        gate_trace=[{"gate": "below_production_threshold", "status": "blocked"}],
        shadow_predictions=[_prediction()],
    )
    events = decision_log.build_timeframe_decision_events(
        {"USDJPY": [plan]},
        now=NOW,
        analysis=MarketAnalysis(currencies={}, regime="risk_off"),
        tech_map={},
    )
    price_rows = []
    for index in range(1, 7):
        close = 150.1 + index * 0.08
        price_rows.append(
            {
                "ts": (NOW + timedelta(minutes=10 * index)).isoformat(),
                "symbol": "USDJPY",
                "timeframe": "1h",
                "close": close,
                "high": close + 0.05,
                "low": close - 0.05,
                "bid_close": close - 0.01,
                "bid_open": close - 0.03,
                "bid_high": close + 0.04,
                "bid_low": close - 0.06,
                "ask_close": close + 0.01,
                "ask_open": close - 0.01,
                "ask_high": close + 0.06,
                "ask_low": close - 0.04,
            }
        )

    report = decision_log.score_decision_events(events, price_entries=price_rows, now=NOW)
    assert report["outcomes"] == []  # final neutral is not a paper trade
    assert report["shadow_scored_outcomes"] == 1
    outcome = report["shadow_outcomes"][0]
    assert outcome["prediction_kind"] == "shadow_hypothesis"
    assert outcome["label_provenance"] == SHADOW_LABEL_PROVENANCE
    assert outcome["realized_net_r"] is not None
    assert outcome["learning_dimensions"] == DIMENSIONS
    assert report["learning_observations"][0]["training_role"] == "shadow_only"

    # A tradable final decision and its shadow stream are scored once each, never mixed.
    plan.direction = "long"
    plan.stop = 149.5
    plan.target1 = 150.5
    plan.target2 = 151.0
    events = decision_log.build_timeframe_decision_events(
        {"USDJPY": [plan]},
        now=NOW,
        analysis=MarketAnalysis(currencies={}, regime="risk_off"),
        tech_map={},
    )
    mixed_report = decision_log.score_decision_events(events, price_entries=price_rows, now=NOW)
    assert len(mixed_report["outcomes"]) == 1
    assert len(mixed_report["shadow_outcomes"]) == 1


def test_shadow_summary_and_promotion_use_separate_producer_stream() -> None:
    outcomes = []
    for index in range(50):
        outcomes.append(
            {
                "ts": (NOW + timedelta(hours=5 * index)).isoformat(),
                "symbol": "USDJPY",
                "producer": "ml_direction",
                "direction_outcome": "hit" if index < 35 else "miss",
                "realized_net_r": 0.2 if index < 35 else -0.1,
                "net_label_eligible": True,
                "learning_dimensions": DIMENSIONS,
            }
        )
    summary = summarize_shadow_outcomes(outcomes)
    assert summary["by_producer"]["ml_direction"]["effective"] == 50
    assert summary["by_producer"]["ml_direction"]["by_session"]["london"]["hits"] == 35

    perf = promotion.evaluate_shadow_member("ml", outcomes)
    assert perf.evaluated == 50
    assert perf.expectancy_net_r is not None and perf.expectancy_net_r > 0


def test_final_outcomes_aggregate_net_r_by_regime() -> None:
    outcomes = [
        {
            "ts": (NOW + timedelta(hours=5 * index)).isoformat(),
            "symbol": "USDJPY",
            "direction": "long",
            "realized_r": 0.4 if index == 0 else -0.2,
            "realized_net_r": 0.3 if index == 0 else -0.3,
            "net_label_eligible": True,
            "learning_dimensions": DIMENSIONS,
        }
        for index in range(2)
    ]
    summary = summarize_outcome_dimensions(outcomes)
    cell = summary["regime"]["risk_off"]["long"]
    assert cell["net_labels"] == 2
    assert cell["net_expectancy_r"] == 0.0


def test_shadow_promotion_does_not_mix_producer_versions_and_resets_stage() -> None:
    outcomes = []
    for index in range(20):
        outcomes.append(
            {
                "ts": (NOW + timedelta(hours=5 * index)).isoformat(),
                "symbol": "USDJPY",
                "producer": "ml_direction",
                "producer_version": "ml-v2@old",
                "direction_outcome": "hit",
                "realized_net_r": 0.3,
                "net_label_eligible": True,
            }
        )
    for index in range(10):
        outcomes.append(
            {
                "ts": (NOW + timedelta(hours=120 + 5 * index)).isoformat(),
                "symbol": "USDJPY",
                "producer": "ml_direction",
                "producer_version": "ml-v3@new",
                "direction_outcome": "miss",
                "realized_net_r": -0.2,
                "net_label_eligible": True,
            }
        )
    perf = promotion.evaluate_shadow_member("ml", outcomes)
    assert perf.producer_version == "ml-v3@new"
    assert perf.evaluated == 10
    assert perf.hits == 0

    previous = promotion.MemberPerformance(
        member="ml",
        evaluated=80,
        hits=50,
        expectancy_net_r=0.2,
        p_value=0.02,
        producer_version="ml-v2@old",
    )
    state = promotion.PromotionState(
        stages={"macro": "shadow", "ml": "paper"},
        last_performance={"ml": previous.to_dict()},
    )
    promotion.update_stages(state, {"ml": perf}, now=NOW + timedelta(days=10))
    assert state.stage_of("ml") == "shadow"
    assert any("producer版変更" in row["reason"] for row in state.history)
