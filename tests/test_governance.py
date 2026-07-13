from __future__ import annotations

from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta
import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
import fx_backtester.governance as governance

from fx_backtester.governance import (
    GovernanceError,
    ModelRecord,
    ModelRegistry,
    PromotionEvidence,
    PromotionReport,
    PromotionPolicy,
    apply_hard_veto,
    evaluate_promotion,
)
from fx_backtester.overfitting import (
    deflated_sharpe_ratio,
    per_period_sharpe,
    probability_of_backtest_overfitting,
)
from fx_backtester.statistical_validation import circular_block_bootstrap_mean_ci

NOW = datetime(2026, 7, 10, tzinfo=UTC)
HASH = "a" * 64


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _label_timing(
    timestamps: list[object],
    barrier_path: Path,
    *,
    horizon_seconds: float = 3600.0,
) -> dict[str, object]:
    parsed = [pd.Timestamp(value).to_pydatetime() for value in timestamps]
    label_end = [value + timedelta(seconds=horizon_seconds) for value in parsed]
    digest = _sha256(barrier_path)
    return {
        "prediction_time": [value.isoformat() for value in parsed],
        "label_end_time": [value.isoformat() for value in label_end],
        "label_available_time": [value.isoformat() for value in label_end],
        "horizon_seconds": [horizon_seconds] * len(parsed),
        "barrier_path_sha256": [digest] * len(parsed),
        "barrier_path": {"path": str(barrier_path), "sha256": digest},
    }


def _passing_evidence(tmp_path: Path) -> PromotionEvidence:
    artifact = tmp_path / "model.bin"
    artifact.write_bytes(b"verified-model-artifact")
    dataset_hash = HASH
    feature_version = "features-v1"
    label_version = "triple-barrier-v1"
    selected_trial_id = "trial-0"
    trial_ids = [f"trial-{index}" for index in range(8)]
    timestamps = pd.date_range(NOW - timedelta(days=50), periods=240, freq="h", tz="UTC")
    rng = np.random.default_rng(123)
    values = rng.normal(0.0, 0.01, (len(timestamps), len(trial_ids)))
    values[:, 0] = 0.002 + rng.normal(0.0, 0.005, len(timestamps))
    returns = pd.DataFrame(values, index=timestamps, columns=trial_ids)
    trial_sharpes = [per_period_sharpe(returns[trial_id]) for trial_id in trial_ids]
    dsr_probability = float(deflated_sharpe_ratio(returns[selected_trial_id], trial_sharpes)["dsr"])
    pbo_probability = float(probability_of_backtest_overfitting(returns, n_blocks=8)["pbo"])
    validation_path = tmp_path / "validation_returns.json"
    calibration_timestamps = pd.date_range(
        NOW - timedelta(days=50), periods=240, freq="h", tz="UTC"
    )
    calibration_labels = [1] * 72 + [0] * 48 + [1] * 48 + [0] * 72
    raw_calibration = [0.9] * 120 + [0.1] * 120
    calibrated_calibration = [0.7] * 120 + [0.3] * 120
    barrier_path = tmp_path / "barrier_path.json"
    barrier_path.write_text(
        json.dumps({"schema_version": 1, "label_version": label_version}),
        encoding="utf-8",
    )
    validation_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "dataset_hash": dataset_hash,
                "selected_trial_id": selected_trial_id,
                "pbo_n_blocks": 8,
                "timestamps": [timestamp.isoformat() for timestamp in timestamps],
                "returns": {
                    trial_id: [float(value) for value in returns[trial_id]]
                    for trial_id in trial_ids
                },
                "calibration_holdout": {
                    **_label_timing(list(calibration_timestamps), barrier_path),
                    "y_true": calibration_labels,
                    "raw_probability": raw_calibration,
                    "calibrated_probability": calibrated_calibration,
                },
            },
            allow_nan=False,
        ),
        encoding="utf-8",
    )
    ledger = tmp_path / "trials.jsonl"
    ledger.write_text(
        "".join(
            json.dumps(
                {
                    "trial_id": trial_id,
                    "status": "complete",
                    "dataset_hash": dataset_hash,
                    "feature_version": feature_version,
                    "label_version": label_version,
                    "config_hash": f"{index:x}" * 64,
                    "started_at": (NOW - timedelta(days=120, hours=index)).isoformat(),
                    "completed_at": (
                        NOW - timedelta(days=120, hours=index) + timedelta(minutes=30)
                    ).isoformat(),
                }
            )
            + "\n"
            for index, trial_id in enumerate(trial_ids)
        ),
        encoding="utf-8",
    )
    net_r = [0.15 + ((index % 5) - 2) * 0.001 for index in range(len(timestamps))]
    cost_stress_2x_net_r = [value - 0.12 for value in net_r]
    test_risk = [0.001] * len(timestamps)
    running_test_equity = 100_000.0
    equity: list[float] = []
    for realized_r, risk_fraction in zip(net_r, test_risk, strict=True):
        running_test_equity *= 1.0 + realized_r * risk_fraction
        equity.append(running_test_equity)
    expectancy_ci = circular_block_bootstrap_mean_ci(
        pd.Series(net_r), block_size=5, resamples=2_000, seed=42
    )
    observations_path = tmp_path / "evaluation_observations.json"
    observations_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "dataset_hash": dataset_hash,
                "selected_trial_id": selected_trial_id,
                "timestamps": [timestamp.isoformat() for timestamp in timestamps],
                "net_r": net_r,
                "cost_stress_2x_net_r": cost_stress_2x_net_r,
                "initial_equity": 100_000.0,
                "risk_fraction": test_risk,
                "equity": equity,
                **{
                    key: value
                    for key, value in _label_timing(list(timestamps), barrier_path).items()
                    if key != "prediction_time"
                },
                "pairs": [["USDJPY", "EURUSD", "GBPUSD"][index % 3] for index in range(240)],
                "regimes": [f"regime-{index % 4}" for index in range(240)],
                "integrity": {
                    "point_in_time_violations": 0,
                    "future_feature_violations": 0,
                },
                "incidents": [],
            },
            allow_nan=False,
        ),
        encoding="utf-8",
    )
    lockbox_timestamps = pd.date_range(NOW - timedelta(days=30), periods=240, freq="h", tz="UTC")
    lockbox_net_r = [0.12 + ((index % 5) - 2) * 0.001 for index in range(240)]
    lockbox_cost_2x = [value - 0.08 for value in lockbox_net_r]
    lockbox_risk = [0.001] * 240
    running_equity = 100_000.0
    lockbox_equity: list[float] = []
    for realized_r, risk_fraction in zip(lockbox_net_r, lockbox_risk, strict=True):
        running_equity *= 1.0 + realized_r * risk_fraction
        lockbox_equity.append(running_equity)
    lockbox_path = tmp_path / "lockbox_observations.json"
    lockbox_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "dataset_hash": dataset_hash,
                "selected_trial_id": selected_trial_id,
                "model_id": "model-1",
                "artifact_hash": _sha256(artifact),
                "timestamps": [timestamp.isoformat() for timestamp in lockbox_timestamps],
                "net_r": lockbox_net_r,
                "cost_stress_2x_net_r": lockbox_cost_2x,
                "initial_equity": 100_000.0,
                "risk_fraction": lockbox_risk,
                "equity": lockbox_equity,
                **{
                    key: value
                    for key, value in _label_timing(list(lockbox_timestamps), barrier_path).items()
                    if key != "prediction_time"
                },
                "calibration": {
                    **_label_timing(list(lockbox_timestamps), barrier_path),
                    "y_true": calibration_labels,
                    "raw_probability": raw_calibration,
                    "calibrated_probability": calibrated_calibration,
                },
                "integrity": {
                    "point_in_time_violations": 0,
                    "future_feature_violations": 0,
                },
                "evaluation": {
                    "evaluation_id": "lockbox-eval-1",
                    "evaluated_at": (NOW - timedelta(days=19)).isoformat(),
                    "model_id": "model-1",
                    "artifact_hash": _sha256(artifact),
                    "dataset_hash": dataset_hash,
                    "selected_trial_id": selected_trial_id,
                    "reused_for_selection": False,
                },
            },
            allow_nan=False,
        ),
        encoding="utf-8",
    )
    manifest_path = tmp_path / "promotion_manifest.json"
    evidence = PromotionEvidence(
        model_id="model-1",
        artifact_hash=_sha256(artifact),
        artifact_path=str(artifact),
        evidence_manifest_path=str(manifest_path),
        trial_ledger_path=str(ledger),
        trial_ledger_hash=_sha256(ledger),
        evaluation_observations_path=str(observations_path),
        evaluation_observations_hash=_sha256(observations_path),
        lockbox_observations_path=str(lockbox_path),
        lockbox_observations_hash=_sha256(lockbox_path),
        dataset_hash=dataset_hash,
        feature_version=feature_version,
        label_version=label_version,
        selected_trial_id=selected_trial_id,
        git_commit="1234567890abcdef",
        dirty_worktree=False,
        synthetic_data=False,
        point_in_time_violations=0,
        future_feature_violations=0,
        trial_count=len(trial_ids),
        sample_count=len(timestamps),
        net_expectancy_r=sum(net_r) / len(net_r),
        expectancy_ci_lower_r=expectancy_ci.lower,
        dsr_probability=dsr_probability,
        pbo_probability=pbo_probability,
        max_drawdown_pct=0.0,
        brier_improvement=0.08,
        cost_stress_2x_expectancy_r=sum(cost_stress_2x_net_r) / len(cost_stress_2x_net_r),
        regime_count=4,
        pair_count=3,
        lockbox_evaluated_once=True,
        lockbox_reused_for_selection=False,
        shadow_days=45,
        paper_days=90,
        major_operational_incidents=0,
        data_quality_incidents=0,
        calibration_window_separate=True,
        test_window_separate=True,
        live_like_execution_validated=True,
    )
    excluded = {
        "artifact_path",
        "evidence_manifest_path",
        "evidence_manifest_hash",
        "trial_ledger_path",
        "trial_ledger_hash",
        "evaluation_observations_path",
        "evaluation_observations_hash",
        "lockbox_observations_path",
        "lockbox_observations_hash",
    }
    manifest_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "promotion_evidence": {
                    key: value for key, value in asdict(evidence).items() if key not in excluded
                },
                "artifact": {"path": str(artifact), "sha256": evidence.artifact_hash},
                "trial_ledger": {"path": str(ledger), "sha256": evidence.trial_ledger_hash},
                "evaluation_observations": {
                    "path": str(observations_path),
                    "sha256": evidence.evaluation_observations_hash,
                },
                "lockbox_observations": {
                    "path": str(lockbox_path),
                    "sha256": evidence.lockbox_observations_hash,
                },
                "validation_returns": {
                    "path": str(validation_path),
                    "sha256": _sha256(validation_path),
                },
                "model_metadata": {
                    "schema_version": 1,
                    "model_id": evidence.model_id,
                    "dataset_hash": evidence.dataset_hash,
                    "feature_version": evidence.feature_version,
                    "label_version": evidence.label_version,
                    "selected_trial_id": evidence.selected_trial_id,
                    "git_commit": evidence.git_commit,
                    "trained_at": (NOW - timedelta(days=90)).isoformat(),
                    "data_cutoff": (NOW - timedelta(days=95)).isoformat(),
                    "selected_at": (NOW - timedelta(days=55)).isoformat(),
                    "model_format": "binary-test-fixture",
                },
                "windows": {
                    "train": {
                        "start": (NOW - timedelta(days=100)).isoformat(),
                        "end": (NOW - timedelta(days=95)).isoformat(),
                    },
                    "tune": {
                        "start": (NOW - timedelta(days=90)).isoformat(),
                        "end": (NOW - timedelta(days=80)).isoformat(),
                    },
                    "calibration": {
                        "start": (NOW - timedelta(days=70)).isoformat(),
                        "end": (NOW - timedelta(days=60)).isoformat(),
                    },
                    "test": {
                        "start": (NOW - timedelta(days=50)).isoformat(),
                        "end": (NOW - timedelta(days=40)).isoformat(),
                    },
                    "lockbox": {
                        "start": (NOW - timedelta(days=30)).isoformat(),
                        "end": (NOW - timedelta(days=20)).isoformat(),
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    return replace(evidence, evidence_manifest_hash=_sha256(manifest_path))


def _record(evidence: PromotionEvidence, model_id: str = "model-1") -> ModelRecord:
    assert evidence.artifact_hash is not None
    assert evidence.artifact_path is not None
    return ModelRecord(
        model_id=model_id,
        artifact_hash=evidence.artifact_hash,
        artifact_path=evidence.artifact_path,
        trained_at=NOW.isoformat(),
        data_cutoff=NOW.isoformat(),
        metrics={},
        calibration_metrics={},
        limitations=[],
    )


def test_missing_or_synthetic_evidence_fails_closed() -> None:
    missing = evaluate_promotion(PromotionEvidence())
    synthetic = evaluate_promotion(PromotionEvidence(dataset_hash=HASH, synthetic_data=True))

    assert not missing.passed
    assert "dataset_hash" in missing.failures
    assert "git_commit" in missing.failures
    assert "non_synthetic_data" in synthetic.failures


def test_complete_evidence_passes_research_gates_but_requires_external_seals(
    tmp_path: Path,
) -> None:
    report = evaluate_promotion(
        _passing_evidence(tmp_path),
        target_stage="validated",
        policy=PromotionPolicy(min_samples=200, max_pbo_probability=0.15),
    )

    assert report.evidence_gates_passed
    assert not report.passed
    assert set(report.failures) == {
        "durable_lockbox_consumption_seal",
        "authoritative_external_seal",
    }


def test_promotion_provenance_recomputes_artifact_manifest_and_trial_ledger(
    tmp_path: Path,
) -> None:
    evidence = _passing_evidence(tmp_path)
    assert evaluate_promotion(evidence).evidence_gates_passed

    Path(evidence.artifact_path or "").write_bytes(b"tampered")
    tampered = evaluate_promotion(evidence)
    assert not tampered.passed
    assert "verified_provenance" in tampered.failures

    evidence = _passing_evidence(tmp_path)
    manifest_path = Path(evidence.evidence_manifest_path or "")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["windows"]["test"]["start"] = manifest["windows"]["calibration"]["end"]
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    overlapping = replace(evidence, evidence_manifest_hash=_sha256(manifest_path))
    report = evaluate_promotion(overlapping)
    assert not report.passed
    assert "verified_provenance" in report.failures


def test_registry_rejects_nonexistent_self_reported_artifact() -> None:
    registry = ModelRegistry()
    record = ModelRecord(
        model_id="model-1",
        artifact_hash=HASH,
        artifact_path="/definitely/not/a/model.bin",
        trained_at=NOW.isoformat(),
        data_cutoff=NOW.isoformat(),
        metrics={},
        calibration_metrics={},
        limitations=[],
    )
    with pytest.raises(GovernanceError, match="missing or does not match"):
        registry.register(record)


def test_promotion_rechecks_manifest_and_trial_ledger_after_evaluation(tmp_path: Path) -> None:
    evidence = _passing_evidence(tmp_path)
    registry = ModelRegistry()
    registry.register(_record(evidence))
    report = evaluate_promotion(evidence)
    Path(evidence.trial_ledger_path or "").write_text(
        '{"trial_id":"replacement"}\n', encoding="utf-8"
    )

    with pytest.raises(GovernanceError, match="trial ledger changed"):
        registry.promote(
            "model-1",
            report,
            approved_by="risk-officer",
            reason="must recheck immutable evidence",
        )


def test_promotion_rechecks_validation_returns_after_evaluation(tmp_path: Path) -> None:
    evidence = _passing_evidence(tmp_path)
    registry = ModelRegistry()
    registry.register(_record(evidence))
    report = evaluate_promotion(evidence)
    manifest = json.loads(Path(evidence.evidence_manifest_path or "").read_text(encoding="utf-8"))
    Path(manifest["validation_returns"]["path"]).write_text("{}", encoding="utf-8")

    with pytest.raises(GovernanceError, match="references changed"):
        registry.promote(
            "model-1",
            report,
            approved_by="risk-officer",
            reason="must recheck immutable validation returns",
        )


def test_policy_and_empty_report_fail_closed() -> None:
    with pytest.raises(ValueError, match="min_dsr_probability"):
        PromotionPolicy(min_dsr_probability=float("nan"))
    with pytest.raises(ValueError, match="min_samples"):
        PromotionPolicy(min_samples=True)

    empty = PromotionReport(
        target_stage="validated",
        gates=(),
        policy_rationale="invalid manually constructed report",
        model_id="model-1",
        artifact_hash=HASH,
    )
    assert empty.passed is False


def test_registry_requires_adjacent_human_approved_promotion_and_blocks_live(
    tmp_path: Path,
) -> None:
    evidence = _passing_evidence(tmp_path)
    registry = ModelRegistry()
    record = _record(evidence)
    registry.register(record, now=NOW)
    validated = evaluate_promotion(evidence, target_stage="validated")
    with pytest.raises(GovernanceError, match="research-only"):
        registry.promote(
            "model-1",
            validated,
            approved_by="risk-officer",
            reason="all validated gates passed",
            now=NOW,
        )
    assert registry.models["model-1"].stage == "research"

    live_report = evaluate_promotion(evidence, target_stage="limited_live")
    with pytest.raises(GovernanceError, match="cannot enable live"):
        registry.promote(
            "model-1",
            live_report,
            approved_by="risk-officer",
            reason="must remain disabled",
        )

    path = tmp_path / "registry.json"
    registry.save(path)
    loaded = ModelRegistry.load(path)
    assert loaded.models["model-1"].artifact_hash == evidence.artifact_hash
    assert loaded.events[-1]["event_type"] == "registered"


def test_promotion_report_is_bound_fingerprinted_and_registry_remains_research_only(
    tmp_path: Path,
) -> None:
    evidence = _passing_evidence(tmp_path)
    registry = ModelRegistry()
    registry.register(_record(evidence))
    registry.register(_record(evidence, "model-2"))
    report = evaluate_promotion(evidence, target_stage="validated")

    with pytest.raises(GovernanceError, match="not bound"):
        registry.promote(
            "model-2",
            report,
            approved_by="reviewer",
            reason="report belongs to model-1",
        )

    with pytest.raises(GovernanceError, match="research-only"):
        registry.promote(
            "model-1",
            report,
            approved_by="reviewer",
            reason="evidence reviewed",
        )

    with pytest.raises(GovernanceError, match="fingerprint is invalid"):
        registry.promote(
            "model-2",
            replace(report, model_id="model-2"),
            approved_by="reviewer",
            reason="binding cannot be edited after evaluation",
        )


@pytest.mark.parametrize(
    ("field", "value", "failed_gate"),
    [
        ("net_expectancy_r", float("nan"), "net_expectancy"),
        ("net_expectancy_r", 0.0, "net_expectancy"),
        ("cost_stress_2x_expectancy_r", float("inf"), "cost_stress_2x"),
        ("dsr_probability", 1.01, "deflated_sharpe"),
        ("pbo_probability", -0.01, "probability_of_backtest_overfitting"),
        ("max_drawdown_pct", -0.01, "drawdown"),
        ("brier_improvement", float("inf"), "calibration_improvement"),
        ("sample_count", True, "sample_size"),
    ],
)
def test_promotion_evidence_rejects_nonfinite_or_out_of_domain_values(
    tmp_path: Path,
    field: str,
    value: object,
    failed_gate: str,
) -> None:
    report = evaluate_promotion(replace(_passing_evidence(tmp_path), **{field: value}))

    assert not report.passed
    assert failed_gate in report.failures


def test_registry_load_rejects_execution_stage_state(tmp_path) -> None:
    evidence = _passing_evidence(tmp_path)
    registry = ModelRegistry()
    registry.register(_record(evidence))
    path = tmp_path / "registry.json"
    registry.save(path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["models"]["model-1"]["stage"] = "limited_live"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(GovernanceError, match="cannot load limited_live/live"):
        ModelRegistry.load(path)

    payload["models"]["model-1"]["stage"] = "research"
    payload["events"][0]["report"] = {"target_stage": "live"}
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(GovernanceError, match="cannot load limited_live/live"):
        ModelRegistry.load(path)


def test_registry_load_rechecks_model_artifact_hash(tmp_path: Path) -> None:
    evidence = _passing_evidence(tmp_path)
    registry = ModelRegistry()
    registry.register(_record(evidence))
    path = tmp_path / "registry.json"
    registry.save(path)
    Path(evidence.artifact_path or "").write_bytes(b"changed-after-save")

    with pytest.raises(GovernanceError, match="missing or changed"):
        ModelRegistry.load(path)


def test_registry_load_replays_history_and_rejects_stage_without_events(tmp_path: Path) -> None:
    evidence = _passing_evidence(tmp_path)
    registry = ModelRegistry()
    registry.register(_record(evidence), now=NOW)
    path = tmp_path / "registry.json"
    registry.save(path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["models"]["model-1"]["stage"] = "paper"
    payload["events"] = []
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(GovernanceError, match="registration history"):
        ModelRegistry.load(path)


def test_promotion_recomputes_dsr_pbo_instead_of_trusting_claims(tmp_path: Path) -> None:
    evidence = _passing_evidence(tmp_path)
    forged = replace(evidence, dsr_probability=0.99, pbo_probability=0.10)
    manifest_path = Path(forged.evidence_manifest_path or "")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["promotion_evidence"]["dsr_probability"] = forged.dsr_probability
    manifest["promotion_evidence"]["pbo_probability"] = forged.pbo_probability
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    forged = replace(forged, evidence_manifest_hash=_sha256(manifest_path))

    report = evaluate_promotion(forged)

    assert not report.passed
    assert "verified_provenance" in report.failures


def test_failed_evidence_cannot_be_promoted(tmp_path: Path) -> None:
    evidence = _passing_evidence(tmp_path)
    registry = ModelRegistry()
    registry.register(_record(evidence))
    with pytest.raises(GovernanceError, match="evidence failed"):
        registry.promote(
            "model-1",
            evaluate_promotion(PromotionEvidence(), target_stage="validated"),
            approved_by="reviewer",
            reason="not enough evidence",
        )


def test_hard_veto_overrides_high_confidence_trade_request() -> None:
    vetoed = apply_hard_veto(
        "long",
        data_quality_reasons=("stale_price",),
        risk_reasons=("drawdown_breach",),
    )
    allowed = apply_hard_veto("short")

    assert vetoed.final_action == "no_trade"
    assert vetoed.vetoed
    assert vetoed.reasons == ("stale_price", "drawdown_breach")
    assert allowed.final_action == "short"
    assert not allowed.vetoed


def test_promotion_rejects_evidence_timestamped_after_evaluation_time(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    future = datetime(2035, 7, 10, tzinfo=UTC)
    monkeypatch.setitem(_passing_evidence.__globals__, "NOW", future)
    evidence = _passing_evidence(tmp_path)

    report = evaluate_promotion(
        evidence,
        as_of=datetime(2026, 7, 13, tzinfo=UTC),
    )

    assert not report.passed
    assert "verified_provenance" in report.failures


def test_promotion_requires_timezone_aware_as_of(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        evaluate_promotion(
            _passing_evidence(tmp_path),
            as_of=datetime(2026, 7, 13),
        )


def test_registry_load_rejects_forged_promoted_event_without_verified_report(
    tmp_path: Path,
) -> None:
    evidence = _passing_evidence(tmp_path)
    registry = ModelRegistry()
    registry.register(_record(evidence), now=NOW)
    path = tmp_path / "registry.json"
    registry.save(path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["models"]["model-1"]["stage"] = "validated"
    payload["events"].append(
        {
            "timestamp": NOW.isoformat(),
            "model_id": "model-1",
            "event_type": "promoted",
            "from_stage": "research",
            "to_stage": "validated",
            "actor": "attacker",
            "reason": "forged",
            "report": {
                "target_stage": "validated",
                "evidence_manifest_path": evidence.evidence_manifest_path,
                "evidence_manifest_hash": evidence.evidence_manifest_hash,
                "trial_ledger_path": evidence.trial_ledger_path,
                "trial_ledger_hash": evidence.trial_ledger_hash,
            },
        }
    )
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(GovernanceError, match="not bound|did not pass"):
        ModelRegistry.load(path)


def test_governance_calibration_metrics_are_recomputed_from_observations(
    tmp_path: Path,
) -> None:
    evidence = _passing_evidence(tmp_path)
    manifest_path = Path(evidence.evidence_manifest_path or "")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    validation_path = Path(manifest["validation_returns"]["path"])
    validation = json.loads(validation_path.read_text(encoding="utf-8"))
    validation["calibration_holdout"] = {
        "sample_count": 240,
        "raw_brier": 0.99,
        "calibrated_brier": 0.01,
    }
    validation_path.write_text(json.dumps(validation), encoding="utf-8")
    manifest["validation_returns"]["sha256"] = _sha256(validation_path)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    evidence = replace(evidence, evidence_manifest_hash=_sha256(manifest_path))

    report = evaluate_promotion(evidence)

    assert not report.passed
    assert "verified_provenance" in report.failures


def test_model_record_rejects_nonfinite_metrics_and_future_registration(tmp_path: Path) -> None:
    evidence = _passing_evidence(tmp_path)
    with pytest.raises(ValueError, match="finite numeric"):
        replace(_record(evidence), metrics={"sharpe": float("nan")})

    future_record = replace(
        _record(evidence),
        trained_at=datetime(2035, 1, 2, tzinfo=UTC).isoformat(),
        data_cutoff=datetime(2035, 1, 1, tzinfo=UTC).isoformat(),
    )
    with pytest.raises(GovernanceError, match="future"):
        ModelRegistry().register(
            future_record,
            now=datetime(2026, 7, 13, tzinfo=UTC),
        )


def test_promotion_rejects_model_cutoff_or_training_after_frozen_partitions(
    tmp_path: Path,
) -> None:
    evidence = _passing_evidence(tmp_path)
    manifest_path = Path(evidence.evidence_manifest_path or "")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["model_metadata"]["data_cutoff"] = (NOW - timedelta(days=2)).isoformat()
    manifest["model_metadata"]["trained_at"] = (NOW - timedelta(days=1)).isoformat()
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    report = evaluate_promotion(
        replace(evidence, evidence_manifest_hash=_sha256(manifest_path)),
        as_of=NOW,
    )

    assert not report.passed
    assert "verified_provenance" in report.failures


def test_promotion_recomputes_raw_expectancy_cost_coverage_incidents_and_lockbox(
    tmp_path: Path,
) -> None:
    evidence = _passing_evidence(tmp_path)
    forged = replace(
        evidence,
        net_expectancy_r=999.0,
        expectancy_ci_lower_r=998.0,
        max_drawdown_pct=0.0,
        cost_stress_2x_expectancy_r=997.0,
        regime_count=999,
        pair_count=999,
        major_operational_incidents=0,
        data_quality_incidents=0,
        lockbox_evaluated_once=True,
        lockbox_reused_for_selection=False,
    )
    manifest_path = Path(forged.evidence_manifest_path or "")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    excluded = {
        "artifact_path",
        "evidence_manifest_path",
        "evidence_manifest_hash",
        "trial_ledger_path",
        "trial_ledger_hash",
        "evaluation_observations_path",
        "evaluation_observations_hash",
        "lockbox_observations_path",
        "lockbox_observations_hash",
    }
    manifest["promotion_evidence"] = {
        key: value for key, value in asdict(forged).items() if key not in excluded
    }
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    forged = replace(forged, evidence_manifest_hash=_sha256(manifest_path))

    report = evaluate_promotion(forged, as_of=NOW)

    assert not report.passed
    assert "verified_provenance" in report.failures


def test_registry_load_semantically_rejects_rehashed_forged_report(tmp_path: Path) -> None:
    evidence = _passing_evidence(tmp_path)
    registry = ModelRegistry()
    registry.register(_record(evidence), now=NOW)
    report = evaluate_promotion(evidence, as_of=NOW)
    forged_gates = tuple(
        replace(gate, observed="forged") if gate.name == "net_expectancy" else gate
        for gate in report.gates
    )
    forged = replace(report, gates=forged_gates, report_id="")
    forged = replace(forged, report_id=governance._promotion_report_id(forged))
    path = tmp_path / "registry.json"
    registry.save(path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["models"]["model-1"]["stage"] = "validated"
    payload["events"].append(
        {
            "timestamp": NOW.isoformat(),
            "model_id": "model-1",
            "event_type": "promoted",
            "from_stage": "research",
            "to_stage": "validated",
            "actor": "attacker",
            "reason": "rehashed forged report",
            "report": forged.to_dict(),
        }
    )
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(GovernanceError, match="semantic re-evaluation"):
        ModelRegistry.load(path, as_of=NOW)


def test_promotion_rejects_reused_lockbox_consumption(tmp_path: Path) -> None:
    evidence = _passing_evidence(tmp_path)
    observations_path = Path(evidence.lockbox_observations_path or "")
    observations = json.loads(observations_path.read_text(encoding="utf-8"))
    observations["evaluation"]["reused_for_selection"] = True
    observations_path.write_text(json.dumps(observations), encoding="utf-8")
    observations_hash = _sha256(observations_path)
    manifest_path = Path(evidence.evidence_manifest_path or "")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["lockbox_observations"]["sha256"] = observations_hash
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    evidence = replace(
        evidence,
        lockbox_observations_hash=observations_hash,
        evidence_manifest_hash=_sha256(manifest_path),
    )

    report = evaluate_promotion(evidence, as_of=NOW)

    assert not report.passed
    assert "verified_provenance" in report.failures


def test_metadata_only_lockbox_cannot_satisfy_promotion_evidence(tmp_path: Path) -> None:
    evidence = _passing_evidence(tmp_path)
    lockbox_path = Path(evidence.lockbox_observations_path or "")
    lockbox_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "window_start": (NOW - timedelta(days=30)).isoformat(),
                "window_end": (NOW - timedelta(days=20)).isoformat(),
                "evaluations": [{"evaluation_id": "metadata-only"}],
            }
        ),
        encoding="utf-8",
    )
    lockbox_hash = _sha256(lockbox_path)
    manifest_path = Path(evidence.evidence_manifest_path or "")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["lockbox_observations"]["sha256"] = lockbox_hash
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    attacked = replace(
        evidence,
        lockbox_observations_hash=lockbox_hash,
        evidence_manifest_hash=_sha256(manifest_path),
    )

    report = evaluate_promotion(attacked, as_of=NOW)

    assert not report.evidence_gates_passed
    assert "verified_provenance" in report.failures
    assert "lockbox_sample_size" in report.failures


def test_lockbox_equity_must_reconcile_to_raw_r_and_risk(tmp_path: Path) -> None:
    evidence = _passing_evidence(tmp_path)
    lockbox_path = Path(evidence.lockbox_observations_path or "")
    lockbox = json.loads(lockbox_path.read_text(encoding="utf-8"))
    lockbox["equity"] = [100_000.0 + index for index in range(240)]
    lockbox_path.write_text(json.dumps(lockbox), encoding="utf-8")
    lockbox_hash = _sha256(lockbox_path)
    manifest_path = Path(evidence.evidence_manifest_path or "")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["lockbox_observations"]["sha256"] = lockbox_hash
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    attacked = replace(
        evidence,
        lockbox_observations_hash=lockbox_hash,
        evidence_manifest_hash=_sha256(manifest_path),
    )

    report = evaluate_promotion(attacked, as_of=NOW)

    assert not report.evidence_gates_passed
    assert "verified_provenance" in report.failures


def test_test_equity_must_reconcile_to_raw_r_and_risk(tmp_path: Path) -> None:
    evidence = _passing_evidence(tmp_path)
    observations_path = Path(evidence.evaluation_observations_path or "")
    observations = json.loads(observations_path.read_text(encoding="utf-8"))
    observations["equity"] = [100_000.0 + index for index in range(240)]
    observations_path.write_text(json.dumps(observations), encoding="utf-8")
    observations_hash = _sha256(observations_path)
    manifest_path = Path(evidence.evidence_manifest_path or "")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["evaluation_observations"]["sha256"] = observations_hash
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    attacked = replace(
        evidence,
        evaluation_observations_hash=observations_hash,
        evidence_manifest_hash=_sha256(manifest_path),
    )

    report = evaluate_promotion(attacked, as_of=NOW)

    assert not report.evidence_gates_passed
    assert "verified_provenance" in report.failures


def test_test_labels_must_be_available_before_lockbox(tmp_path: Path) -> None:
    evidence = _passing_evidence(tmp_path)
    observations_path = Path(evidence.evaluation_observations_path or "")
    observations = json.loads(observations_path.read_text(encoding="utf-8"))
    observations["label_available_time"][-1] = (
        NOW - timedelta(days=30) + timedelta(microseconds=1)
    ).isoformat()
    observations_path.write_text(json.dumps(observations), encoding="utf-8")
    observations_hash = _sha256(observations_path)
    manifest_path = Path(evidence.evidence_manifest_path or "")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["evaluation_observations"]["sha256"] = observations_hash
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    attacked = replace(
        evidence,
        evaluation_observations_hash=observations_hash,
        evidence_manifest_hash=_sha256(manifest_path),
    )

    report = evaluate_promotion(attacked, as_of=NOW)

    assert not report.evidence_gates_passed
    assert "verified_provenance" in report.failures


def test_lockbox_labels_must_be_available_by_evaluation_cutoff(tmp_path: Path) -> None:
    evidence = _passing_evidence(tmp_path)
    lockbox_path = Path(evidence.lockbox_observations_path or "")
    lockbox = json.loads(lockbox_path.read_text(encoding="utf-8"))
    evaluated_at = datetime.fromisoformat(lockbox["evaluation"]["evaluated_at"])
    lockbox["label_available_time"][-1] = (evaluated_at + timedelta(microseconds=1)).isoformat()
    lockbox_path.write_text(json.dumps(lockbox), encoding="utf-8")
    lockbox_hash = _sha256(lockbox_path)
    manifest_path = Path(evidence.evidence_manifest_path or "")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["lockbox_observations"]["sha256"] = lockbox_hash
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    attacked = replace(
        evidence,
        lockbox_observations_hash=lockbox_hash,
        evidence_manifest_hash=_sha256(manifest_path),
    )

    report = evaluate_promotion(attacked, as_of=NOW)

    assert not report.evidence_gates_passed
    assert "verified_provenance" in report.failures
