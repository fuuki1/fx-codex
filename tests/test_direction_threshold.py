from __future__ import annotations

from datetime import datetime, timedelta, UTC
import json

import pytest

from fx_intel import direction_threshold as dt
from fx_intel.evaluation_labels import (
    DEFAULT_COMMISSION_MODEL_ID,
    DEFAULT_COST_MODEL_ID,
    DEFAULT_COST_MODEL_VERSION,
    DEFAULT_COST_STATUS,
    DEFAULT_SLIPPAGE_MODEL_ID,
    NET_LABEL_PROVENANCE,
    NET_LABEL_VERSION,
)

NOW = datetime(2026, 7, 17, 8, 0, tzinfo=UTC)


def _outcomes(count: int = 300, *, losing: bool = False) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for index in range(count):
        strong = index % 2 == 0
        composite = 0.28 if strong else 0.18
        net_r = (-0.4 if losing else 0.55) if strong else -0.35
        prediction_time = NOW - timedelta(hours=8 * (count - index))
        rows.append(
            {
                "ts": prediction_time.isoformat(),
                "prediction_time": prediction_time.isoformat(),
                "holding_end_time": (prediction_time + timedelta(hours=1)).isoformat(),
                "decision_id": f"decision-{index}",
                "symbol": "USDJPY",
                "direction": "long",
                "timeframe": "1h",
                "horizon_hours": 1.0,
                "composite": composite,
                "realized_r": net_r + 0.1,
                "realized_net_r": net_r + (index % 5) * 0.01,
                "gross_realized_r": net_r + 0.1,
                "quote_realized_r": net_r + (index % 5) * 0.01,
                "tradable": True,
                "net_label_eligible": True,
                "label_version": NET_LABEL_VERSION,
                "label_provenance": NET_LABEL_PROVENANCE,
                "cost_model_id": DEFAULT_COST_MODEL_ID,
                "cost_model_version": DEFAULT_COST_MODEL_VERSION,
                "cost_status": DEFAULT_COST_STATUS,
                "entry_bid": 99.9,
                "entry_ask": 100.1,
                "planned_risk_distance": 1.0,
                "entry_spread_r": 0.2,
                "slippage_r": 0.0,
                "commission_r": 0.0,
                "financing_r": 0.0,
                "additional_cost_r": 0.0,
                "execution_cost_r": 0.1 - (index % 5) * 0.01,
                "entry_quote_source": "fixture",
                "spread_source": "fixture",
                "slippage_model_id": DEFAULT_SLIPPAGE_MODEL_ID,
                "commission_model_id": DEFAULT_COMMISSION_MODEL_ID,
                "cost_quality_flags": [],
                "path_quality": 1.0,
            }
        )
    return rows


def test_candidate_evaluation_only_promotes_stricter_profitable_threshold() -> None:
    policy = dt.evaluate_threshold_candidates(_outcomes(), now=NOW)
    assert policy.threshold >= dt.DEFAULT_THRESHOLD
    assert policy.threshold == pytest.approx(0.25)
    assert policy.stage == "ready_for_review"
    assert policy.oos_net_r_lcb is not None and policy.oos_net_r_lcb > 0
    assert policy.effective_samples >= dt.DEFAULT_MIN_TEST_SAMPLES
    assert policy.raw_samples == 300
    assert policy.effective_input_samples == 300
    assert policy.overlap_ratio == 0.0


def test_policy_requires_human_approval_and_activation(tmp_path) -> None:
    candidate = dt.evaluate_threshold_candidates(_outcomes(), now=NOW)
    assert dt.effective_threshold(candidate, now=NOW) == dt.DEFAULT_THRESHOLD
    approved = dt.approve_policy(candidate, "risk-owner", now=NOW)
    assert dt.effective_threshold(approved, now=NOW) == dt.DEFAULT_THRESHOLD
    active = dt.activate_policy(approved, now=NOW)
    assert dt.effective_threshold(active, now=NOW) == pytest.approx(candidate.threshold)
    assert dt.effective_threshold(active, now=NOW + timedelta(days=91)) == dt.DEFAULT_THRESHOLD

    path = tmp_path / "threshold.json"
    dt.save_policy(active, path)
    loaded = dt.load_policy(path)
    assert loaded == active


def test_policy_load_and_validation_fail_closed(tmp_path) -> None:
    path = tmp_path / "threshold.json"
    path.write_text("{broken", encoding="utf-8")
    assert dt.load_policy(path) is None
    assert dt.effective_threshold(None) == dt.DEFAULT_THRESHOLD

    path.write_text(
        json.dumps(
            {
                "schema": dt.SCHEMA_VERSION,
                "policy_id": "unsafe",
                "stage": "active",
                "threshold": 0.05,
                "fallback_threshold": 0.15,
                "scope": "overall",
            }
        ),
        encoding="utf-8",
    )
    assert dt.load_policy(path) is None


def test_active_policy_auto_pauses_when_recent_net_r_lcb_is_nonpositive() -> None:
    candidate = dt.evaluate_threshold_candidates(_outcomes(), now=NOW)
    active = dt.activate_policy(dt.approve_policy(candidate, "risk-owner", now=NOW), now=NOW)
    paused = dt.auto_pause_policy(active, _outcomes(losing=True))
    assert paused.stage == "auto_paused"
    assert dt.effective_threshold(paused, now=NOW) == dt.DEFAULT_THRESHOLD


def test_invalid_label_accounting_is_rejected() -> None:
    rows = _outcomes()
    rows[-1]["label_version"] = "legacy-net-label"
    with pytest.raises(ValueError, match="invalid canonical net label"):
        dt.evaluate_threshold_candidates(rows, now=NOW)


def test_overlapping_rows_do_not_satisfy_threshold_oos_sample_gate() -> None:
    rows = _outcomes()
    start = NOW - timedelta(hours=30)
    for index, row in enumerate(rows):
        prediction_time = start + timedelta(minutes=5 * index)
        row["ts"] = prediction_time.isoformat()
        row["prediction_time"] = prediction_time.isoformat()
        row["holding_end_time"] = (prediction_time + timedelta(hours=1)).isoformat()

    policy = dt.evaluate_threshold_candidates(rows, now=NOW)

    assert policy.stage == "shadow"
    assert policy.effective_samples < dt.DEFAULT_MIN_TEST_SAMPLES
    assert policy.raw_samples == 300
    assert policy.effective_input_samples < policy.raw_samples
    assert policy.overlap_ratio is not None and policy.overlap_ratio > 0


def test_missing_holding_interval_is_rejected_before_threshold_statistics() -> None:
    rows = _outcomes()
    rows[-1].pop("holding_end_time")

    with pytest.raises(ValueError, match="invalid canonical net label"):
        dt.evaluate_threshold_candidates(rows, now=NOW)
