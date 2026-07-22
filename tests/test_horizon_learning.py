from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from fx_intel.horizon_learning import (
    HorizonScoreResult,
    ScoredHorizonForecast,
    derive_horizon_learning,
    make_calibration_provider,
    score_horizon_history,
)
from fx_intel.horizons import HORIZON_BY_LABEL

BASE = datetime(2026, 7, 14, 12, 0, tzinfo=UTC)


def _entry(ts: datetime, *, symbol="USDJPY", spread=0.01, direction="long") -> dict:
    return {
        "schema_version": 1,
        "contract": "horizon-pit-v1",
        "ts": ts.isoformat(),
        "prediction_time": ts.isoformat(),
        "source_cutoff": (ts - timedelta(seconds=10)).isoformat(),
        "max_feature_available_time": (ts - timedelta(seconds=1)).isoformat(),
        "pit_eligible": True,
        "symbol": symbol,
        "horizon": "1h",
        "horizon_hours": 1.0,
        "shadow_only": False,
        "direction": direction,
        "composite": 0.4,
        "p_up": 0.55,
        "p_down": 0.25,
        "p_flat": 0.20,
        "close": 150.0,
        "atr_h": 0.2,
        "spread": spread,
        "flat_threshold": max(0.02, spread * 2),
        "band_p10": -0.25,
        "band_p50": 0.0,
        "band_p90": 0.25,
        "expected_range": 0.5,
        "features": {
            "vol_bucket": "mid",
            "session": "london",
            "rating_15m": 0.5,
            "rating_1h": 0.5,
            "rating_4h": 0.5,
            "rating_1d": 0.5,
            "news_score": 0.4,
        },
        "gates": {"freshness_ok": True},
    }


def _price(ts: datetime, close: float, *, high=None, low=None, symbol="USDJPY") -> dict:
    return {
        "ts": ts.isoformat(),
        "available_time": ts.isoformat(),
        "symbol": symbol,
        "timeframe": "15m",
        "close": close,
        "high": high if high is not None else close,
        "low": low if low is not None else close,
    }


def test_scoring_computes_three_class_losses_band_path_and_cost_adjusted_r() -> None:
    entries = [_entry(BASE)]
    prices = [
        _price(BASE + timedelta(minutes=30), 150.05, high=150.20, low=149.95),
        _price(BASE + timedelta(hours=1), 150.10, high=150.15, low=150.0),
    ]
    result = score_horizon_history(entries, prices, now=BASE + timedelta(hours=2))
    item = result.scored[0]
    assert item.realized_class == "up"
    assert item.direction_outcome == "hit"
    assert item.brier == pytest.approx(0.305)
    assert item.log_loss == pytest.approx(-__import__("math").log(0.55))
    assert item.band_covered is True
    assert item.range_ratio == pytest.approx(0.2)
    assert item.mfe == pytest.approx(0.20)
    assert item.mae == pytest.approx(-0.05)
    assert item.net_r == pytest.approx(0.45)


def test_spread_twice_can_make_realized_class_flat() -> None:
    result = score_horizon_history(
        [_entry(BASE, spread=0.04)],
        [_price(BASE + timedelta(hours=1), 150.05)],
        now=BASE + timedelta(hours=2),
    )
    assert result.scored[0].realized_class == "flat"


def _scored(index: int, horizon: str) -> ScoredHorizonForecast:
    up = index % 2 == 0
    return ScoredHorizonForecast(
        symbol="USDJPY",
        horizon=horizon,
        ts=BASE + timedelta(hours=index * HORIZON_BY_LABEL[horizon].learn_thin_gap_hours),
        direction="long" if up else "short",
        composite=0.4 if up else -0.4,
        move=0.1 if up else -0.1,
        realized_class="up" if up else "down",
        direction_outcome="hit",
        brier=0.1,
        log_loss=0.2,
        pinball_p10=0.01,
        pinball_p50=0.02,
        pinball_p90=0.01,
        band_covered=index % 5 != 0,
        range_ratio=0.2,
        mfe=0.2,
        mae=-0.05,
        net_r=0.4,
        vol_bucket="mid",
        session="london",
        shadow_only=horizon == "5m",
        features={
            "rating_15m": 0.5 if up else -0.5,
            "rating_1h": 0.5 if up else -0.5,
            "rating_4h": 0.5 if up else -0.5,
            "rating_1d": 0.5 if up else -0.5,
            "news_score": 0.5 if up else -0.5,
        },
    )


def test_a2_calibration_weights_and_promotion_gate() -> None:
    result = HorizonScoreResult(scored=[_scored(index, "15m") for index in range(100)])
    state = derive_horizon_learning(result, now=BASE + timedelta(days=10))
    profile = state["profiles"]["USDJPY|15m"]
    assert state["contract"] == "horizon-pit-v1"
    assert state["gbdt_review_gate"] == "approved_pre_a2"
    assert profile["calibrated"] is True
    assert profile["learned_weights"] is not None
    assert profile["promotion"]["stage"] == "adopted"
    assert profile["promotion"]["remaining_n"] == 0
    probabilities = make_calibration_provider(state)("USDJPY", "15m", 0.4)
    assert probabilities is not None and sum(probabilities) == pytest.approx(1.0)


def test_5m_can_never_leave_shadow() -> None:
    result = HorizonScoreResult(scored=[_scored(index, "5m") for index in range(120)])
    state = derive_horizon_learning(result, now=BASE + timedelta(days=2))
    promotion = state["profiles"]["USDJPY|5m"]["promotion"]
    assert promotion["permanent_shadow"] is True
    assert promotion["integration_eligible"] is False
    assert promotion["required_n"] is None
