from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from fx_intel.horizon_forecast import build_horizon_forecasts
from fx_intel.horizon_journal import (
    HorizonPointInTimeError,
    append_horizon_forecasts,
    is_pit_eligible_horizon_entry,
    read_horizon_entries,
)
from fx_intel.horizons import HORIZON_SPECS, PRIOR_WEIGHTS, flat_threshold
from fx_intel.sentiment import CurrencySentiment
from fx_intel.technicals import IntervalView, PairTechnicals

OPEN_NOW = datetime(2026, 7, 14, 12, 0, tzinfo=UTC)


def _tech() -> PairTechnicals:
    views = {}
    for index, timeframe in enumerate(("15m", "1h", "4h", "1d")):
        views[timeframe] = IntervalView(
            interval=timeframe,
            recommendation="BUY",
            buy=10,
            sell=2,
            neutral=3,
            close=150.0,
            bid=149.995,
            ask=150.005,
            spread=0.01,
            rsi=55.0,
            adx=24.0,
            atr=0.2 * (index + 1),
            sma_fast=150.2,
            sma_slow=149.8,
        )
    return PairTechnicals("USDJPY", views=views)


def _context(*, stale: bool = False) -> dict:
    reasons = ["stale_quote"] if stale else ["baseline_insufficient"]
    return {
        "context_id": "ctx-1",
        "learning_dimensions": {"session_bucket": "london_new_york_overlap"},
        "macro": {
            "features": {"vix_level": 18.0, "macro_pair_score": 0.25},
            "feature_masks": {"vix_level": 1, "macro_pair_score": 1},
        },
        "liquidity": {
            "status": "unknown" if not stale else "invalid",
            "reason_codes": reasons,
            "features": {"spread_price": 0.01},
            "quote": {"bid": 149.995, "ask": 150.005, "available_time": OPEN_NOW.isoformat()},
        },
    }


def _forecasts(context=None):
    scores = {
        "USD": CurrencySentiment("USD", score=0.4, headline_count=3),
        "JPY": CurrencySentiment("JPY", score=-0.2, headline_count=2),
    }
    return build_horizon_forecasts(
        "USDJPY", _tech(), scores, [], [], context or _context(), now=OPEN_NOW, calendar_ok=True
    )


def test_approved_horizon_set_is_exactly_eight_plus_permanent_5m_shadow() -> None:
    assert [spec.label for spec in HORIZON_SPECS] == [
        "5m", "15m", "30m", "1h", "3h", "6h", "12h", "24h", "3d"
    ]
    assert HORIZON_SPECS[0].shadow_only is True
    assert all(not spec.shadow_only for spec in HORIZON_SPECS[1:])
    assert "9h" not in PRIOR_WEIGHTS


def test_flat_threshold_uses_larger_atr_or_twice_measured_spread() -> None:
    assert flat_threshold(0.2, 0.04) == pytest.approx(0.08)
    assert flat_threshold(1.0, 0.01) == pytest.approx(0.1)


def test_generator_builds_nine_simplex_rows_with_macro_only_on_long_horizons() -> None:
    rows = _forecasts()
    assert len(rows) == 9
    assert sum(row.shadow_only for row in rows) == 1
    assert all(row.p_up + row.p_down + row.p_flat == pytest.approx(1.0) for row in rows)
    assert all(row.flat_threshold == pytest.approx(max(row.atr_h * 0.1, 0.02)) for row in rows)
    assert "macro_vix_level" not in next(row for row in rows if row.horizon == "3h").features
    assert next(row for row in rows if row.horizon == "6h").features["macro_vix_level"] == 18.0
    assert all(row.input_context_id == "ctx-1" for row in rows)


def test_stale_quote_fails_closed_for_every_horizon() -> None:
    rows = _forecasts(_context(stale=True))
    assert {row.direction for row in rows} == {"neutral"}
    assert {row.conviction for row in rows} == {0}
    assert all(row.gates["freshness_ok"] is False for row in rows)


def test_journal_enforces_pit_contract_and_probability_simplex(tmp_path) -> None:
    path = tmp_path / "horizon.jsonl"
    rows = _forecasts()
    prediction = OPEN_NOW + timedelta(seconds=10)
    assert append_horizon_forecasts(
        path,
        rows,
        prediction_time=prediction,
        source_cutoff=OPEN_NOW - timedelta(seconds=30),
        max_feature_available_time=OPEN_NOW,
    ) == 9
    stored = list(read_horizon_entries(path))
    assert len(stored) == 9
    assert all(is_pit_eligible_horizon_entry(row) for row in stored)
    assert len({row["prediction_id"] for row in stored}) == 9
    assert {row["track_stage"] for row in stored} == {"shadow"}

    with pytest.raises(HorizonPointInTimeError, match="duplicate five-minute"):
        append_horizon_forecasts(
            path,
            rows,
            prediction_time=prediction + timedelta(seconds=20),
            source_cutoff=OPEN_NOW - timedelta(seconds=30),
            max_feature_available_time=OPEN_NOW,
        )

    with pytest.raises(HorizonPointInTimeError):
        append_horizon_forecasts(
            path,
            rows,
            prediction_time=OPEN_NOW,
            source_cutoff=OPEN_NOW,
            max_feature_available_time=OPEN_NOW + timedelta(seconds=1),
        )
    assert len(path.read_text().splitlines()) == 9


def test_journal_rows_are_strict_json(tmp_path) -> None:
    path = tmp_path / "horizon.jsonl"
    append_horizon_forecasts(
        path,
        _forecasts(),
        prediction_time=OPEN_NOW,
        source_cutoff=OPEN_NOW - timedelta(seconds=1),
        max_feature_available_time=OPEN_NOW,
    )
    assert all(isinstance(json.loads(line), dict) for line in path.read_text().splitlines())
