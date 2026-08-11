from __future__ import annotations

from datetime import UTC, datetime, timedelta
import hashlib
import json

import pytest

from fx_intel.multi_axis_learning import build_multi_axis_shadow_model
from fx_intel.virtual_portfolio_learning import (
    InvalidLearningDataset,
    build_challenger_manifest,
    build_outcome_memory,
    build_promotion_audit,
    build_validation_evidence,
)
from fx_intel.virtual_portfolio_net_labels import (
    InvalidVirtualNetLabel,
    VIRTUAL_LEARNING_CONTRACT,
    canonical_outcome_from_virtual_row,
    canonical_outcome_sha256,
)
from tools.virtual_portfolio_learning import _create_artifacts

START = datetime(2025, 1, 1, tzinfo=UTC)


def _sha256(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _row(index: int) -> dict[str, object]:
    prediction = START + timedelta(hours=48 * index)
    label_end = prediction + timedelta(hours=1)
    net = 100 if index % 2 == 0 else -50
    gross = net + 20
    direction = "long" if index % 2 == 0 else "short"
    row: dict[str, object] = {
        "trade_id": f"trade-{index:04d}",
        "decision_id": f"decision-{index:04d}",
        "prediction_time": prediction.isoformat(),
        "simulated_entry_time": prediction.isoformat(),
        "label_end_time": label_end.isoformat(),
        "label_available_time": label_end.isoformat(),
        "label_ingested_time": label_end.isoformat(),
        "label_knowledge_basis": "test_fixture",
        "label_time_basis": "single_clock",
        "simulated_entry_available_time": prediction.isoformat(),
        "simulated_entry_knowledge_basis": "test_fixture",
        "symbol": "USDJPY",
        "timeframe": "1h",
        "direction": direction,
        "planned_risk_jpy": 5_000,
        "entry_bid": 100.0,
        "entry_ask": 100.001,
        "entry_mid": 100.0005,
        "entry_executable": 100.001 if direction == "long" else 100.0,
        "stop_price": 99.501 if direction == "long" else 100.5,
        "target_price": 101.001 if direction == "long" else 99.0,
        "units": 10_000.0,
        "entry_quote_to_jpy_rate": 1.0,
        "entry_quote_conversion_source": "JPY quote currency",
        "exit_bid": 100.012 if direction == "long" else 100.003,
        "exit_ask": 100.013 if direction == "long" else 100.004,
        "exit_mid": 100.0125 if direction == "long" else 100.0035,
        "exit_executable": 100.012 if direction == "long" else 100.004,
        "exit_quote_to_jpy_rate": 1.0,
        "exit_quote_conversion_source": "JPY quote currency",
        "entry_quote": {
            "source": "fixture_quotes",
            "source_record_id": f"entry-quote-{index}",
            "revision_id": f"entry-revision-{index}",
            "event_time": prediction.isoformat(),
            "available_time": prediction.isoformat(),
            "ingested_time": prediction.isoformat(),
            "bid": 100.0,
            "ask": 100.001,
        },
        "exit_quote": {
            "source": "fixture_quotes",
            "source_record_id": f"exit-quote-{index}",
            "revision_id": f"exit-revision-{index}",
            "event_time": label_end.isoformat(),
            "available_time": label_end.isoformat(),
            "ingested_time": label_end.isoformat(),
            "bid": 100.012 if direction == "long" else 100.003,
            "ask": 100.013 if direction == "long" else 100.004,
        },
        "features": {"rsi": 40 + index % 20},
        "components": {"technical": 0.4},
        "data_sha256": "a" * 64,
        "model_sha256": "b" * 64,
        "policy_sha256": "c" * 64,
        "decision_record_sha256": "d" * 64,
        "open_record_sha256": "e" * 64,
        "open_observation_sha256": "1" * 64,
        "close_record_sha256": "f" * 64,
        "close_observation_sha256": "0" * 64,
        "net_pnl_jpy": net,
        "realized_net_r": net / 5_000,
        "attribution": {
            "gross_market_pnl_jpy": gross,
            "spread_quote_cost_jpy": 10,
            "slippage_cost_jpy": 5,
            "commission_jpy": 5,
            "financing_jpy": 0,
            "conversion_cost_jpy": 0,
            "net_pnl_jpy": net,
            "cost_contract": {
                "cost_model_id": "explicit-test-costs-v1",
                "cost_model_version": "1",
                "cost_source": "test-fixture",
                "costs_complete": True,
            },
            "execution_observation_mode": "contemporaneous_quote",
            "replay_evidence": {},
        },
        "failures": [],
        "close_reason": "time_exit",
        "contract": VIRTUAL_LEARNING_CONTRACT,
        "research_only": True,
        "promotion_eligible": False,
    }
    _refresh_canonical(row)
    return row


def _refresh_canonical(row: dict[str, object]) -> None:
    outcome = canonical_outcome_from_virtual_row(row)
    payload = outcome.to_dict()
    row.update(payload)
    row["canonical_outcome"] = payload
    row["canonical_outcome_sha256"] = canonical_outcome_sha256(outcome)


def test_canonical_v3_rejects_quote_json_fill_mismatch() -> None:
    row = _row(0)
    row["entry_quote"]["bid"] = 999.0

    with pytest.raises(InvalidVirtualNetLabel, match="quote JSON bid/ask"):
        canonical_outcome_from_virtual_row(row)


def test_canonical_v3_rejects_quote_after_simulated_fill() -> None:
    row = _row(0)
    after_fill = START + timedelta(seconds=1)
    row["simulated_entry_available_time"] = after_fill.isoformat()
    row["entry_quote"]["event_time"] = after_fill.isoformat()
    row["entry_quote"]["available_time"] = after_fill.isoformat()
    row["entry_quote"]["ingested_time"] = after_fill.isoformat()

    with pytest.raises(InvalidVirtualNetLabel, match="after the simulated fill"):
        canonical_outcome_from_virtual_row(row)


def _pit_value(value: float, key: str, prediction: datetime) -> dict[str, object]:
    available = prediction - timedelta(minutes=5)
    return {
        "value": value,
        "unit": "ratio",
        "event_time": (prediction - timedelta(hours=1)).isoformat(),
        "available_time": available.isoformat(),
        "ingested_time": available.isoformat(),
        "first_seen_time": available.isoformat(),
        "source": "fixture",
        "source_record_id": f"source-{key}",
        "content_hash": _sha256({"key": key, "value": value}),
        "quality_flags": [],
    }


def _multi_axis_row(index: int) -> dict[str, object]:
    row = _row(index)
    prediction = datetime.fromisoformat(str(row["prediction_time"]))
    regime = "risk_on" if index % 2 == 0 else "risk_off"
    spread_price = 0.02 + index % 3 * 0.001
    macro_features = {
        "vix_change_5d_pct": float(index % 7) / 10.0,
        "usd_index_change_5d_pct": float(index % 5) / 10.0,
    }
    surprise_features = {
        "base_latest_z": 0.8 if index % 2 == 0 else -0.6,
        "quote_latest_z": -0.2,
        "pair_diff_z": 1.0 if index % 2 == 0 else -0.4,
        "relevant_event_count": 2.0,
        "max_abs_z": 0.8,
    }
    row["learning_dimensions"] = {
        "session_bucket": "london",
        "regime": regime,
        "regime_source": "macro_real_data",
    }
    row["input_context_id"] = f"context-{index}"
    row["input_context"] = {
        "context_schema_version": "decision-input-v1",
        "decision_time": prediction.isoformat(),
        "learning_dimensions": row["learning_dimensions"],
        "macro": {
            "schema_version": "macro-features-v1",
            "features": macro_features,
            "feature_masks": {key: 1 for key in macro_features},
            "values": {
                key: _pit_value(value, key, prediction) for key, value in macro_features.items()
            },
        },
        "macro_surprises": {
            "schema_version": "macro-surprise-features-v1",
            "features": surprise_features,
            "feature_masks": {key: 1 for key in surprise_features},
            "values": {
                key: _pit_value(value, f"surprise-{key}", prediction)
                for key, value in surprise_features.items()
            },
            "revision_policy": "first_release_only",
            "unknown_is_not_zero": True,
        },
        "liquidity": {
            "schema_version": "fx-liquidity-proxy-v1",
            "status": "normal",
            "features": {
                "spread_price": spread_price,
                "quote_age_sec": 1.0,
            },
            "feature_masks": {
                "spread_price": 1,
                "quote_age_sec": 1,
            },
            "quote": {
                "bid": 150.0,
                "ask": 150.0 + spread_price,
                "observed_at": (prediction - timedelta(seconds=2)).isoformat(),
                "available_time": (prediction - timedelta(seconds=1)).isoformat(),
                "ingested_time": (prediction - timedelta(seconds=1)).isoformat(),
                "source": "fixture",
                "role": "decision_quote",
                "source_record_id": f"quote-{index}",
                "content_hash": _sha256({"quote": index}),
                "quality_status": "measured",
                "quality_flags": [],
            },
        },
    }
    row["input_features"] = {
        "macro__vix_change_5d_pct": macro_features["vix_change_5d_pct"],
        "liquidity__spread_price": spread_price,
    }
    row["input_feature_masks"] = {
        "macro__vix_change_5d_pct": 1,
        "liquidity__spread_price": 1,
    }
    risk_snapshot = {
        "contract": "virtual-portfolio-risk-snapshot-v1",
        "as_of": prediction.isoformat(),
        "knowledge_as_of": prediction.isoformat(),
        "currency": "JPY",
        "initial_capital_jpy": 1_000_000,
        "cash_balance_jpy": 1_000_000 + index * 10,
        "peak_cash_balance_jpy": 1_001_500,
        "cash_drawdown_jpy": max(0, 1_500 - index * 10),
        "cash_drawdown_ratio": max(0, 1_500 - index * 10) / 1_001_500,
        "open_position_count": index % 2,
        "open_planned_risk_jpy": 5_000 if index % 2 else 0,
        "open_planned_risk_ratio": 0.005 if index % 2 else 0.0,
        "period_pnl_jpy": {"day": 100, "week": 200, "month": 300},
        "remaining_loss_budget_jpy": {
            "day": 15_100,
            "week": 30_200,
            "month": 60_300,
        },
        "hard_drawdown_limit_jpy": 100_000,
        "risk_gate_reason": None,
        "policy_sha256": "9" * 64,
        "source_record_ids": ["portfolio-policy:test"],
        "source_record_sha256": ["9" * 64],
        "liquidation_heat_status": "unavailable_in_prediction_snapshot",
        "research_only": True,
    }
    risk_snapshot["snapshot_sha256"] = _sha256(risk_snapshot)
    row["portfolio_risk_snapshot"] = risk_snapshot
    row["execution_snapshot"] = {
        "diagnostic_execution_cost_r": 0.04,
        "diagnostic_net_expected_r": 0.12,
        "expected_r": 0.16,
    }
    row["source_record_ids"] = [f"decision-source-{index}"]
    return row


def test_manifest_stays_closed_below_minimum() -> None:
    rows = [_row(index) for index in range(149)]
    manifest = build_challenger_manifest(
        {"rows": rows},
        as_of=START + timedelta(days=400),
    )

    assert manifest["status"] == "insufficient_data"
    assert manifest["eligible_for_candidate_training"] is False
    assert manifest["promotion_eligible"] is False
    assert manifest["daily_target_used_as_label"] is False
    assert manifest["splits"] == {}


def test_manifest_builds_temporal_windows_without_claiming_a_fixed_lockbox() -> None:
    rows = [_row(index) for index in range(150)]
    manifest = build_challenger_manifest(
        {"rows": list(reversed(rows))},
        as_of=START + timedelta(days=400),
    )

    assert manifest["status"] == "ready_for_candidate_training"
    assert manifest["eligible_for_candidate_training"] is True
    assert manifest["execution_target"] == "shadow_decision_only"
    splits = manifest["splits"]
    assert splits["train"]["row_count"] == 60
    assert splits["tune"]["row_count"] == 20
    assert splits["calibration"]["row_count"] == 20
    assert splits["test"]["row_count"] == 30
    assert splits["lockbox"]["row_count"] == 20
    assert splits["lockbox"]["sealed"] is False
    assert splits["lockbox"]["role"] == "rolling_development_holdout"
    assert manifest["lockbox_access"] == "unavailable_no_fixed_lockbox_commitment"


def test_daily_target_feature_and_bad_cost_identity_are_rejected() -> None:
    target_leak = _row(0)
    target_leak["features"] = {"daily_target_jpy": 70_000}
    with pytest.raises(InvalidLearningDataset, match="daily KPI leaked"):
        build_challenger_manifest(
            {"rows": [target_leak]},
            as_of=START + timedelta(days=2),
        )

    bad_cost = _row(0)
    bad_cost["net_pnl_jpy"] = 999
    with pytest.raises(
        InvalidLearningDataset,
        match="does not reconcile|does not equal",
    ):
        build_challenger_manifest(
            {"rows": [bad_cost]},
            as_of=START + timedelta(days=2),
        )


def test_outcome_memory_aggregates_segments_and_failure_codes() -> None:
    rows = [_row(index) for index in range(4)]
    rows[1]["failures"] = [{"code": "wrong_direction"}]
    memory = build_outcome_memory(
        {"rows": rows, "dataset_sha256": "d" * 64},
        as_of=START + timedelta(days=20),
    )

    assert memory["source_row_count"] == 4
    assert memory["failure_counts"] == {"wrong_direction": 1}
    assert memory["daily_target_used_as_label"] is False
    assert memory["promotion_eligible"] is False
    assert len(memory["segments"]) == 2
    assert len(str(memory["memory_sha256"])) == 64


def test_validation_evidence_computes_test_stress_but_blocks_promotion() -> None:
    rows = [_row(index) for index in range(150)]
    payload = {"rows": rows}
    as_of = START + timedelta(days=400)
    manifest = build_challenger_manifest(payload, as_of=as_of)
    evidence = build_validation_evidence(payload, manifest, as_of=as_of, seed=7)

    assert evidence["status"] == "blocked_missing_trial_and_calibration_evidence"
    assert evidence["promotion_eligible"] is False
    assert evidence["lockbox_access"] == "unavailable_no_fixed_lockbox_commitment"
    assert evidence["test_window"]["row_count"] == 30
    assert evidence["test_window"]["bootstrap_mean_net_r"]["status"] == "available"
    assert set(evidence["test_window"]["cost_stress"]) == {
        "1.0x",
        "1.5x",
        "2.0x",
        "3.0x",
    }
    assert evidence["gates"]["pbo"] == "unavailable_no_trial_matrix"
    assert evidence["gates"]["deflated_sharpe"] == "unavailable_no_trial_ledger"
    assert len(str(evidence["evidence_sha256"])) == 64


def test_validation_evidence_stays_unavailable_below_minimum() -> None:
    payload = {"rows": [_row(index) for index in range(10)]}
    as_of = START + timedelta(days=30)
    manifest = build_challenger_manifest(payload, as_of=as_of)
    evidence = build_validation_evidence(payload, manifest, as_of=as_of)

    assert evidence["status"] == "insufficient_data"
    assert evidence["test_window"] == {}
    assert evidence["gates"]["lockbox"] == "unavailable_no_fixed_lockbox_commitment"


def test_promotion_audit_records_every_missing_gate_and_never_auto_promotes() -> None:
    payload = {"rows": [_row(index) for index in range(150)]}
    as_of = START + timedelta(days=400)
    manifest = build_challenger_manifest(payload, as_of=as_of)
    evidence = build_validation_evidence(payload, manifest, as_of=as_of)
    audit = build_promotion_audit(
        payload,
        manifest,
        evidence,
        git_commit="a" * 40,
        dirty_worktree=False,
        as_of=as_of,
    )

    assert audit["current_stage"] == "research"
    assert audit["requested_stage"] == "validated"
    assert audit["decision"] == "remain_research"
    assert audit["automatic_promotion"] is False
    assert audit["promotion_eligible"] is False
    report = audit["promotion_report"]
    assert report["passed"] is False
    assert set(report["failures"]) >= {
        "non_synthetic_data",
        "all_trials_recorded",
        "deflated_sharpe",
        "probability_of_backtest_overfitting",
        "test_split",
        "untouched_lockbox",
        "operational_incidents",
        "data_quality_incidents",
    }
    assert len(str(audit["promotion_audit_sha256"])) == 64


def test_promotion_audit_rejects_forged_validation_evidence() -> None:
    payload = {"rows": [_row(index) for index in range(150)]}
    as_of = START + timedelta(days=400)
    manifest = build_challenger_manifest(payload, as_of=as_of)
    evidence = build_validation_evidence(payload, manifest, as_of=as_of)
    forged = {**evidence, "reason": "forged pass"}

    with pytest.raises(InvalidLearningDataset, match="validation evidence does not match"):
        build_promotion_audit(
            payload,
            manifest,
            forged,
            git_commit="a" * 40,
            dirty_worktree=False,
            as_of=as_of,
        )


def test_multi_axis_shadow_model_trains_all_eight_axes_without_opening_lockbox() -> None:
    rows = [_multi_axis_row(index) for index in range(150)]
    payload = {"rows": rows}
    as_of = START + timedelta(days=400)
    manifest = build_challenger_manifest(payload, as_of=as_of)

    artifact = build_multi_axis_shadow_model(payload, manifest, as_of=as_of, seed=11)

    assert artifact["status"] == "trained_shadow_candidate"
    assert artifact["lockbox_access"] == "unavailable_no_fixed_lockbox_commitment"
    assert artifact["lockbox_row_count"] == 0
    assert artifact["rolling_holdout_row_count"] == 20
    assert artifact["promotion_eligible"] is False
    assert artifact["model_usable_for_decisions"] is False
    assert len(artifact["axes"]) == 8
    assert all(axis["ready"] for axis in artifact["axes"].values())
    assert set(artifact["models"]["quantiles"]) == {"p10", "p50", "p90"}
    assert len(artifact["trial_ledger"]) == 8
    assert all(trial["status"] == "completed" for trial in artifact["trial_ledger"])
    interval_metrics = artifact["rolling_test_diagnostics"]["net_return_distribution"]
    assert interval_metrics["interval_calibration"]["method"] == (
        "split_conformalized_quantile_interval"
    )
    assert "train_empirical_interval_baseline" in interval_metrics
    assert artifact["remaining_validation_gates"]["independent_review"] == "required"
    feature_names = artifact["feature_schema"]["raw_feature_names"]
    assert all("realized" not in name for name in feature_names)
    assert all("attribution" not in name for name in feature_names)
    assert all("net_pnl" not in name for name in feature_names)
    assert all("execution_estimate" not in name for name in feature_names)


def test_multi_axis_model_fails_closed_when_new_axes_are_missing() -> None:
    rows = [_row(index) for index in range(150)]
    payload = {"rows": rows}
    as_of = START + timedelta(days=400)
    manifest = build_challenger_manifest(payload, as_of=as_of)

    artifact = build_multi_axis_shadow_model(payload, manifest, as_of=as_of)

    assert artifact["status"] == "insufficient_axis_coverage"
    assert set(artifact["unavailable_axes"]) == {
        "market_regime",
        "liquidity_proxy",
        "macro_surprise",
        "cross_asset_relationship",
        "portfolio_risk",
    }
    assert artifact["models"] == {}


def test_multi_axis_model_rejects_future_macro_and_bad_portfolio_hash() -> None:
    rows = [_multi_axis_row(index) for index in range(150)]
    as_of = START + timedelta(days=400)
    future = rows[0]["input_context"]["macro_surprises"]["values"]["base_latest_z"]
    future["available_time"] = (START + timedelta(days=1)).isoformat()
    manifest = build_challenger_manifest({"rows": rows}, as_of=as_of)
    with pytest.raises(InvalidLearningDataset, match="not available at prediction_time"):
        build_multi_axis_shadow_model({"rows": rows}, manifest, as_of=as_of)

    rows = [_multi_axis_row(index) for index in range(150)]
    rows[0]["portfolio_risk_snapshot"]["cash_balance_jpy"] = 999_999
    manifest = build_challenger_manifest({"rows": rows}, as_of=as_of)
    with pytest.raises(InvalidLearningDataset, match="hash mismatch"):
        build_multi_axis_shadow_model({"rows": rows}, manifest, as_of=as_of)


def test_multi_axis_never_uses_display_components_as_features() -> None:
    as_of = START + timedelta(days=400)
    clean_rows = [_multi_axis_row(index) for index in range(150)]
    clean_payload = {"rows": clean_rows}
    clean_manifest = build_challenger_manifest(clean_payload, as_of=as_of)
    clean_artifact = build_multi_axis_shadow_model(
        clean_payload,
        clean_manifest,
        as_of=as_of,
    )

    display_rows = [_multi_axis_row(index) for index in range(150)]
    display_rows[0]["components"] = [
        {
            "key": "technical",
            "label_ja": "テクニカル",
            "score": 0.4,
            "weight": 1.0,
            "realized_net_r_clone": display_rows[0]["realized_net_r"],
        }
    ]
    display_payload = {"rows": display_rows}
    display_manifest = build_challenger_manifest(display_payload, as_of=as_of)
    display_artifact = build_multi_axis_shadow_model(
        display_payload,
        display_manifest,
        as_of=as_of,
    )

    assert display_artifact["components_used_as_features"] is False
    assert display_artifact["feature_schema"] == clean_artifact["feature_schema"]
    assert display_artifact["models"] == clean_artifact["models"]


def test_multi_axis_rejects_stale_manifest() -> None:
    as_of = START + timedelta(days=400)
    original_rows = [_multi_axis_row(index) for index in range(150)]
    stale_manifest = build_challenger_manifest({"rows": original_rows}, as_of=as_of)
    changed_rows = [_multi_axis_row(index) for index in range(150)]
    changed_rows[0]["features"]["rsi_1h"] = 99.0
    with pytest.raises(InvalidLearningDataset, match="manifest does not match"):
        build_multi_axis_shadow_model(
            {"rows": changed_rows},
            stale_manifest,
            as_of=as_of,
        )


def test_multi_axis_does_not_extract_lockbox_feature_or_label_content() -> None:
    rows = [_multi_axis_row(index) for index in range(150)]
    lockbox_row = rows[-1]
    prediction = datetime.fromisoformat(str(lockbox_row["prediction_time"]))
    lockbox_row["input_context"]["macro_surprises"]["values"]["base_latest_z"]["available_time"] = (
        prediction + timedelta(minutes=1)
    ).isoformat()
    payload = {"rows": rows}
    as_of = START + timedelta(days=400)
    manifest = build_challenger_manifest(payload, as_of=as_of)

    artifact = build_multi_axis_shadow_model(payload, manifest, as_of=as_of)

    assert artifact["status"] == "trained_shadow_candidate"
    assert artifact["lockbox_access"] == "unavailable_no_fixed_lockbox_commitment"


def test_multi_axis_rejects_future_quote_ingestion_and_envelope_mismatch() -> None:
    as_of = START + timedelta(days=400)
    rows = [_multi_axis_row(index) for index in range(150)]
    prediction = datetime.fromisoformat(str(rows[0]["prediction_time"]))
    rows[0]["input_context"]["liquidity"]["quote"]["ingested_time"] = (
        prediction + timedelta(seconds=1)
    ).isoformat()
    manifest = build_challenger_manifest({"rows": rows}, as_of=as_of)
    with pytest.raises(InvalidLearningDataset, match="quote follows prediction_time"):
        build_multi_axis_shadow_model({"rows": rows}, manifest, as_of=as_of)

    rows = [_multi_axis_row(index) for index in range(150)]
    rows[0]["input_context"]["liquidity"]["features"]["spread_price"] = 999_999.0
    manifest = build_challenger_manifest({"rows": rows}, as_of=as_of)
    with pytest.raises(InvalidLearningDataset, match="does not match the decision quote"):
        build_multi_axis_shadow_model({"rows": rows}, manifest, as_of=as_of)

    rows = [_multi_axis_row(index) for index in range(150)]
    rows[0]["input_context"]["macro"]["features"]["vix_change_5d_pct"] = 99.0
    manifest = build_challenger_manifest({"rows": rows}, as_of=as_of)
    with pytest.raises(InvalidLearningDataset, match="PIT envelope value"):
        build_multi_axis_shadow_model({"rows": rows}, manifest, as_of=as_of)


def test_multi_axis_accepts_only_legacy_six_decimal_macro_rounding() -> None:
    as_of = START + timedelta(days=400)
    canonical_rows = [_multi_axis_row(index) for index in range(150)]
    for row in canonical_rows:
        macro = row["input_context"]["macro"]
        macro["values"]["usd_index_change_5d_pct"]["value"] = 0.12345649
        macro["features"]["usd_index_change_5d_pct"] = 0.123456
    canonical_payload = {"rows": canonical_rows}
    canonical_manifest = build_challenger_manifest(canonical_payload, as_of=as_of)
    canonical_artifact = build_multi_axis_shadow_model(
        canonical_payload,
        canonical_manifest,
        as_of=as_of,
    )

    attacked_rows = [_multi_axis_row(index) for index in range(150)]
    for row in attacked_rows:
        macro = row["input_context"]["macro"]
        macro["values"]["usd_index_change_5d_pct"]["value"] = 0.12345649
        epsilon = 5e-13 if row["realized_net_r"] > 0 else -5e-13
        macro["features"]["usd_index_change_5d_pct"] = 0.123456 + epsilon
    attacked_payload = {"rows": attacked_rows}
    attacked_manifest = build_challenger_manifest(attacked_payload, as_of=as_of)
    with pytest.raises(InvalidLearningDataset, match="PIT envelope value"):
        build_multi_axis_shadow_model(
            attacked_payload,
            attacked_manifest,
            as_of=as_of,
        )

    assert canonical_artifact["status"] == "trained_shadow_candidate"

    rows = [_multi_axis_row(index) for index in range(150)]
    macro = rows[0]["input_context"]["macro"]
    macro["values"]["usd_index_change_5d_pct"]["value"] = 0.12345649
    macro["features"]["usd_index_change_5d_pct"] = 0.123457
    payload = {"rows": rows}
    manifest = build_challenger_manifest(payload, as_of=as_of)
    with pytest.raises(InvalidLearningDataset, match="PIT envelope value"):
        build_multi_axis_shadow_model(payload, manifest, as_of=as_of)


def test_multi_axis_rejects_implausible_macro_values() -> None:
    as_of = START + timedelta(days=400)
    rows = [_multi_axis_row(index) for index in range(150)]
    macro = rows[0]["input_context"]["macro"]
    macro["values"]["usd_index_change_5d_pct"]["value"] = 1e15
    macro["features"]["usd_index_change_5d_pct"] = 1e15
    payload = {"rows": rows}
    manifest = build_challenger_manifest(payload, as_of=as_of)

    with pytest.raises(InvalidLearningDataset, match="outside its plausible range"):
        build_multi_axis_shadow_model(payload, manifest, as_of=as_of)


def test_multi_axis_fails_closed_for_single_class_calibration() -> None:
    rows = [_multi_axis_row(index) for index in range(150)]
    for row in rows[80:100]:
        row["net_pnl_jpy"] = 100
        row["realized_net_r"] = 0.02
        row["attribution"]["gross_market_pnl_jpy"] = 120
        row["attribution"]["net_pnl_jpy"] = 100
        if row["direction"] == "short":
            row["exit_bid"] = 99.988
            row["exit_ask"] = 99.989
            row["exit_mid"] = 99.9885
            row["exit_executable"] = 99.989
            row["exit_quote"]["bid"] = 99.988
            row["exit_quote"]["ask"] = 99.989
        _refresh_canonical(row)
    payload = {"rows": rows}
    as_of = START + timedelta(days=400)
    manifest = build_challenger_manifest(payload, as_of=as_of)

    artifact = build_multi_axis_shadow_model(payload, manifest, as_of=as_of)

    assert artifact["status"] == "insufficient_class_support"
    assert artifact["models"] == {}
    assert artifact["class_support"]["calibration"]["negative"] == 0


def test_learning_rows_reject_duplicate_decision_and_net_r_identity_mismatch() -> None:
    as_of = START + timedelta(days=10)
    duplicate = [_row(0), _row(1)]
    duplicate[1]["decision_id"] = duplicate[0]["decision_id"]
    _refresh_canonical(duplicate[1])
    with pytest.raises(InvalidLearningDataset, match="duplicate decision_id"):
        build_challenger_manifest({"rows": duplicate}, as_of=as_of)

    bad_identity = _row(0)
    bad_identity["realized_net_r"] = 999.0
    with pytest.raises(InvalidLearningDataset, match="does not equal"):
        build_challenger_manifest({"rows": [bad_identity]}, as_of=as_of)


def test_learning_rejects_broken_bid_ask_or_missing_conversion_cost() -> None:
    as_of = START + timedelta(days=2)
    crossed = _row(0)
    crossed_label = crossed["canonical_outcome"]
    crossed_label["exit_bid"] = crossed_label["exit_ask"] + 0.01
    crossed["canonical_outcome_sha256"] = _sha256(crossed_label)
    with pytest.raises(InvalidLearningDataset, match="canonical net-R contract violation"):
        build_challenger_manifest({"rows": [crossed]}, as_of=as_of)

    missing_conversion = _row(0)
    conversion_label = missing_conversion["canonical_outcome"]
    conversion_label["conversion_r"] = None
    missing_conversion["canonical_outcome_sha256"] = _sha256(conversion_label)
    with pytest.raises(InvalidLearningDataset, match="missing_or_invalid_conversion_r"):
        build_challenger_manifest({"rows": [missing_conversion]}, as_of=as_of)


def test_learning_artifacts_publish_atomically_with_completion_hashes(tmp_path) -> None:
    manifest = {
        "generated_at": START.isoformat(),
        "source_dataset_sha256": "a" * 64,
        "manifest_sha256": "b" * 64,
    }
    targets = _create_artifacts(
        tmp_path,
        manifest,
        {"memory": 1},
        {"evidence": 2},
        {"promotion": 3},
        {"multi_axis": 4},
    )

    completion = json.loads((targets[0].parent / "completion.json").read_text(encoding="utf-8"))
    assert set(completion["files"]) == {
        "challenger_gate.json",
        "outcome_memory.json",
        "validation_evidence.json",
        "promotion_audit.json",
        "multi_axis_model.json",
    }
    for target in targets:
        assert target.is_file()
        assert target.stat().st_mode & 0o222 == 0
        assert hashlib.sha256(target.read_bytes()).hexdigest() == completion["files"][target.name]

    with pytest.raises(FileExistsError, match="immutable learning run"):
        _create_artifacts(
            tmp_path,
            manifest,
            {"memory": 1},
            {"evidence": 2},
            {"promotion": 3},
            {"multi_axis": 4},
        )


def test_label_time_basis_rejects_unknown_value() -> None:
    """自己申告が壊れた行は fail-closed で弾く。"""

    rows = [_row(index) for index in range(150)]
    rows[0] = {**rows[0], "label_time_basis": "bogus"}
    as_of = START + timedelta(days=400)

    with pytest.raises(InvalidLearningDataset, match="unknown label_time_basis"):
        build_challenger_manifest({"rows": rows}, as_of=as_of)


def test_label_time_basis_missing_is_rejected() -> None:
    """省略も許容しない(黙って unknown に落とす後方互換を作らない)。"""

    rows = [_row(index) for index in range(150)]
    rows[0] = {key: value for key, value in rows[0].items() if key != "label_time_basis"}
    as_of = START + timedelta(days=400)

    with pytest.raises(InvalidLearningDataset, match="label_time_basis is required"):
        build_challenger_manifest({"rows": rows}, as_of=as_of)


def test_single_clock_makes_ingestion_stage_tautological() -> None:
    """single_clock では available==ingested なので第3段は何も弾かない。

    4段検証が「通った」ことを取込ラグの検証と読んではいけない、という
    契約上の事実をテストとして固定する。
    """

    rows = [_row(index) for index in range(150)]
    assert all(row["label_time_basis"] == "single_clock" for row in rows)
    assert all(row["label_available_time"] == row["label_ingested_time"] for row in rows)
    as_of = START + timedelta(days=400)

    manifest = build_challenger_manifest({"rows": rows}, as_of=as_of)

    # available/ingested を落として2段版にしても、通る行数は変わらない。
    two_stage = [
        {
            key: value
            for key, value in row.items()
            if key not in {"label_available_time", "label_ingested_time"}
        }
        for row in rows
    ]
    admissible = [
        row
        for row in two_stage
        if row["prediction_time"] < row["label_end_time"] <= as_of.isoformat()
    ]
    assert manifest["source_row_count"] == len(admissible)


def test_label_time_basis_does_not_leak_into_hashed_row() -> None:
    """内部キーは公開行へ出さない(dataset hash を変えない)。"""

    rows = [_row(index) for index in range(150)]
    as_of = START + timedelta(days=400)
    manifest = build_challenger_manifest({"rows": rows}, as_of=as_of)

    assert "_label_time_basis" not in json.dumps(manifest)
