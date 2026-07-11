from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from fx_backtester.pit_dataset import RawInput, SourceLineage, materialize_pit_dataset
from fx_backtester.point_in_time import PointInTimeRecord
from fx_backtester.research_experiment import (
    ResearchExperimentConfig,
    ResearchExperimentError,
    audit_research_experiment,
    evaluate_lockbox_once,
    prepare_research_experiment,
)
from fx_backtester.time_series_validation import ModelPartitions, chronological_model_partitions
from fx_backtester.trial_log import TrialLogger

DATASET_CREATED_AT = datetime(2024, 3, 1, tzinfo=UTC)
COMMIT = "b" * 40
MODEL_HASH = "c" * 64
TRIAL_IDS = tuple(f"trial-{position}" for position in range(4))


def _dataset(tmp_path: Path):
    tmp_path.mkdir(parents=True, exist_ok=True)
    raw_path = tmp_path / "source.csv"
    raw_path.write_text("timestamp,value\n2024-01-01T00:00:00Z,1.0\n", encoding="utf-8")
    record = PointInTimeRecord(
        event_time=datetime(2024, 1, 1, tzinfo=UTC),
        available_time=datetime(2024, 1, 2, tzinfo=UTC),
        ingested_time=datetime(2024, 1, 2, tzinfo=UTC),
        validated_time=datetime(2024, 1, 2, 1, tzinfo=UTC),
        source="vendor",
        source_record_id="observation-1",
        payload={"value": 1.0},
        run_id="ingest-1",
        writer_id="writer-1",
    )
    return materialize_pit_dataset(
        tmp_path / "pit",
        [record],
        source_lineage=[
            SourceLineage(
                source="vendor",
                upstream_uri="https://example.invalid/data",
                source_version="2024-01",
                contract_status="research_only",
                license_status="research_only",
                limitations=("fixture only",),
            )
        ],
        raw_inputs=[
            RawInput(
                source="vendor",
                role="observations",
                path=raw_path,
                acquired_at=datetime(2024, 1, 3, tzinfo=UTC),
            )
        ],
        transform_name="test-pit",
        transform_version="1",
        dataset_class="research_only",
        description="research experiment fixture",
        created_at=DATASET_CREATED_AT,
        code_commit=COMMIT,
        dirty_worktree=False,
    )


def _evaluation(dataset_id: str, rows: int = 320) -> pd.DataFrame:
    prediction = pd.date_range("2024-01-01T00:00:00Z", periods=rows, freq="h")
    positions = np.arange(rows)
    labels = (positions % 2).astype(int)
    raw_probability = np.where(labels == 1, 0.78, 0.22)
    net_r = np.where(labels == 1, 0.18, -0.08) + 0.02 * np.sin(positions / 4.0)
    return pd.DataFrame(
        {
            "sample_id": [f"sample-{position:04d}" for position in positions],
            "dataset_id": dataset_id,
            "candidate_id": "trial-0",
            "model_artifact_sha256": MODEL_HASH,
            "prediction_time": prediction,
            "label_end_time": prediction + pd.Timedelta(minutes=30),
            "max_feature_available_time": prediction - pd.Timedelta(minutes=1),
            "pair": "EURUSD",
            "horizon": "1h",
            "regime": np.where(positions % 3 == 0, "risk_on", "risk_off"),
            "raw_probability": raw_probability,
            "label": labels,
            "net_r": net_r,
        }
    )


def _config() -> ResearchExperimentConfig:
    return ResearchExperimentConfig(
        hypothesis="Pre-registered research-only fixture",
        pair="EURUSD",
        horizon="1h",
        lockbox_purpose="single final governance evaluation",
        expected_trial_ids=TRIAL_IDS,
        calibrator_method="platt",
        bootstrap_block_size=2,
        bootstrap_resamples=100,
        permutations=100,
        pbo_blocks=4,
        seed=17,
    )


def _trial_bundle(
    tmp_path: Path,
    evaluation: pd.DataFrame,
    dataset_id: str,
    config: ResearchExperimentConfig,
) -> Path:
    partitions = chronological_model_partitions(
        pd.DatetimeIndex(evaluation["prediction_time"]),
        pd.DatetimeIndex(evaluation["label_end_time"]),
        config.partition_config,
    )
    tune_times = pd.DatetimeIndex(
        evaluation.iloc[list(partitions.tune)]["prediction_time"]
    ).tz_convert("UTC")
    logger = TrialLogger(
        run_id="complete-tune-family",
        context={
            "declared_complete_trial_family": True,
            "selection_partition": "tune",
            "dataset_id": dataset_id,
            "pair": config.pair,
            "horizon": config.horizon,
            "expected_trial_ids": list(TRIAL_IDS),
            "selected_model_artifact_sha256": MODEL_HASH,
        },
    )
    rng = np.random.default_rng(19)
    for trial_number in range(4):
        drift = 0.012 - trial_number * 0.003
        returns = pd.Series(
            rng.normal(drift, 0.06 + trial_number * 0.005, len(tune_times)),
            index=tune_times,
        )
        logger.log(
            f"trial-{trial_number}",
            params={"depth": trial_number + 1},
            phase="tune",
            metrics={"mean_r": float(returns.mean())},
            score=float(returns.mean()),
            returns=returns,
        )
    logger.mark_selected("trial-0")
    return logger.write(tmp_path / "trials")["run_dir"]


def _cost_stress(test_rows: int, dataset_id: str) -> pd.DataFrame:
    rows = []
    for name, multiplier, expectancy, drawdown in (
        ("observed", 1.0, 0.05, 0.08),
        ("cost_1_5x", 1.5, 0.03, 0.09),
        ("cost_2x", 2.0, 0.01, 0.10),
        ("cost_3x", 3.0, -0.02, 0.13),
    ):
        rows.append(
            {
                "scenario": name,
                "cost_multiplier": multiplier,
                "method": "full_engine_rerun",
                "dataset_id": dataset_id,
                "candidate_id": "trial-0",
                "model_artifact_sha256": MODEL_HASH,
                "trade_count": test_rows,
                "expectancy_r": expectancy,
                "max_drawdown": drawdown,
                "execution_config": {
                    "spread_multiplier": multiplier,
                    "commission_multiplier": multiplier,
                },
            }
        )
    return pd.DataFrame(rows)


def _fixture(tmp_path: Path):
    dataset = _dataset(tmp_path)
    full_evaluation = _evaluation(dataset.dataset_id)
    config = _config()
    partitions = chronological_model_partitions(
        pd.DatetimeIndex(full_evaluation["prediction_time"]),
        pd.DatetimeIndex(full_evaluation["label_end_time"]),
        config.partition_config,
    )
    trial_bundle = _trial_bundle(tmp_path, full_evaluation, dataset.dataset_id, config)
    lockbox_rows = list(partitions.withheld_lockbox_positions)
    outcomes = full_evaluation.iloc[lockbox_rows][["sample_id", "label", "net_r"]].copy()
    outcomes.insert(
        1,
        "outcome_available_time",
        pd.DatetimeIndex(full_evaluation.iloc[lockbox_rows]["label_end_time"])
        + pd.Timedelta(minutes=1),
    )
    evaluation = full_evaluation.copy()
    evaluation.loc[lockbox_rows, ["label", "net_r"]] = None
    stress = _cost_stress(len(partitions.test), dataset.dataset_id)
    created_at = datetime.now(UTC)
    return dataset, evaluation, outcomes, config, trial_bundle, stress, created_at


def _prepare(tmp_path: Path, *, root_name: str = "experiments", evaluation=None):
    dataset, default_evaluation, outcomes, config, trial_bundle, stress, created_at = _fixture(
        tmp_path
    )
    artifact = prepare_research_experiment(
        tmp_path / root_name,
        dataset_dir=dataset.directory,
        lockbox_claim_store=tmp_path / "claims",
        evaluation=default_evaluation if evaluation is None else evaluation,
        trial_run_dir=trial_bundle,
        cost_stress=stress,
        config=config,
        created_at=created_at,
        code_commit=COMMIT,
        dirty_worktree=False,
    )
    return artifact, outcomes, created_at


def test_prepared_manifest_binds_declared_trial_family_and_withholds_lockbox_outcomes(
    tmp_path: Path,
) -> None:
    artifact, _outcomes, _created_at = _prepare(tmp_path)
    manifest = json.loads(artifact.manifest_path.read_text(encoding="utf-8"))
    audit = audit_research_experiment(artifact.directory)
    rows = [
        json.loads(line)
        for line in (artifact.directory / "evaluation.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]

    assert audit.passed
    assert manifest["state"] == "descriptive_test_recomputed_lockbox_outcomes_withheld"
    assert manifest["analysis"]["selection"]["declared_complete_trial_family"] is True
    assert manifest["analysis"]["selection"]["independent_preregistration_attested"] is False
    assert manifest["analysis"]["partitions"]["lockbox"]["state"] == "outcomes_withheld"
    lockbox_rows = manifest["analysis"]["partitions"]["lockbox"]["rows"]
    assert all(row["label"] is None and row["net_r"] is None for row in rows[-lockbox_rows:])
    assert not (artifact.directory / "lockbox.open.json").exists()
    assert manifest["promotion_eligible"] is False
    assert manifest["analysis"]["promotion"]["evidence"]["trial_count"] is None
    assert manifest["analysis"]["promotion"]["evidence"]["net_expectancy_r"] is None
    assert manifest["analysis"]["promotion"]["report"]["passed"] is False
    assert {
        "point_in_time_integrity",
        "future_feature_integrity",
        "test_split",
        "untouched_lockbox",
        "pair_coverage",
    }.issubset(manifest["analysis"]["promotion"]["report"]["failures"])


def test_lockbox_is_persisted_before_open_and_can_complete_only_once(tmp_path: Path) -> None:
    artifact, outcomes, _created_at = _prepare(tmp_path)

    result = evaluate_lockbox_once(
        artifact.directory,
        outcomes,
        actor="independent-reviewer",
        opened_at=datetime.now(UTC),
    )

    assert result.result["state"] == "completed_under_configured_local_claim_store"
    assert result.result["promotion"]["evidence"]["lockbox_evaluated_once"] is None
    assert result.result["promotion"]["evidence"]["lockbox_reused_for_selection"] is None
    assert result.result["promotion"]["report"]["passed"] is False
    assert audit_research_experiment(artifact.directory).passed
    with pytest.raises(ResearchExperimentError, match="already been consumed"):
        evaluate_lockbox_once(
            artifact.directory,
            outcomes,
            actor="second-reviewer",
            opened_at=datetime.now(UTC),
        )


def test_prepare_never_calls_process_local_lockbox_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden(*args, **kwargs):
        raise AssertionError("prepare must not open lockbox")

    monkeypatch.setattr(ModelPartitions, "open_lockbox", forbidden)

    artifact, _outcomes, _created_at = _prepare(tmp_path)

    assert artifact.manifest_path.exists()


def test_failure_after_durable_marker_consumes_lockbox_without_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    artifact, outcomes, _created_at = _prepare(tmp_path)

    def injected_failure(*args, **kwargs):
        raise RuntimeError("injected lockbox evaluator crash")

    monkeypatch.setattr(ModelPartitions, "open_lockbox", injected_failure)
    with pytest.raises(RuntimeError, match="injected"):
        evaluate_lockbox_once(
            artifact.directory,
            outcomes,
            actor="reviewer",
            opened_at=datetime.now(UTC),
        )

    assert (artifact.directory / "lockbox.open.json").exists()
    assert not (artifact.directory / "lockbox.result.json").exists()
    audit = audit_research_experiment(artifact.directory)
    assert not audit.passed
    assert any("incomplete" in error for error in audit.errors)


def test_tampered_evaluation_and_incomplete_trial_matrix_fail_closed(tmp_path: Path) -> None:
    artifact, _outcomes, _created_at = _prepare(tmp_path, root_name="tamper")
    evaluation_path = artifact.directory / "evaluation.jsonl"
    evaluation_path.write_bytes(evaluation_path.read_bytes() + b"{}\n")
    assert not audit_research_experiment(artifact.directory).passed

    dataset, evaluation, _outcomes, config, trial_bundle, stress, created_at = _fixture(
        tmp_path / "incomplete"
    )
    matrix_path = trial_bundle / "returns_matrix.csv"
    matrix = pd.read_csv(matrix_path)
    matrix.drop(columns="trial-3").to_csv(matrix_path, index=False)
    with pytest.raises(ResearchExperimentError, match="columns must exactly match"):
        prepare_research_experiment(
            tmp_path / "rejected",
            dataset_dir=dataset.directory,
            lockbox_claim_store=tmp_path / "incomplete-claims",
            evaluation=evaluation,
            trial_run_dir=trial_bundle,
            cost_stress=stress,
            config=config,
            created_at=created_at,
            code_commit=COMMIT,
            dirty_worktree=False,
        )


def test_future_feature_availability_is_rejected(tmp_path: Path) -> None:
    dataset, evaluation, _outcomes, config, trial_bundle, stress, created_at = _fixture(tmp_path)
    evaluation.loc[0, "max_feature_available_time"] = evaluation.loc[
        0, "prediction_time"
    ] + pd.Timedelta(seconds=1)

    with pytest.raises(ResearchExperimentError, match="future feature"):
        prepare_research_experiment(
            tmp_path / "rejected",
            dataset_dir=dataset.directory,
            lockbox_claim_store=tmp_path / "claims",
            evaluation=evaluation,
            trial_run_dir=trial_bundle,
            cost_stress=stress,
            config=config,
            created_at=created_at,
            code_commit=COMMIT,
            dirty_worktree=False,
        )


def test_prepare_rejects_plaintext_lockbox_outcomes(tmp_path: Path) -> None:
    dataset, evaluation, outcomes, config, trial_bundle, stress, created_at = _fixture(tmp_path)
    partitions = chronological_model_partitions(
        pd.DatetimeIndex(evaluation["prediction_time"]),
        pd.DatetimeIndex(evaluation["label_end_time"]),
        config.partition_config,
    )
    leaked = evaluation.copy()
    lockbox_rows = list(partitions.withheld_lockbox_positions)
    leaked.loc[lockbox_rows, "label"] = outcomes["label"].to_numpy()
    leaked.loc[lockbox_rows, "net_r"] = outcomes["net_r"].to_numpy()

    with pytest.raises(ResearchExperimentError, match="must be withheld"):
        prepare_research_experiment(
            tmp_path / "leaked",
            dataset_dir=dataset.directory,
            lockbox_claim_store=tmp_path / "claims",
            evaluation=leaked,
            trial_run_dir=trial_bundle,
            cost_stress=stress,
            config=config,
            created_at=created_at,
            code_commit=COMMIT,
            dirty_worktree=False,
        )


def test_prepare_rejects_future_lockbox_predictions_and_predataset_trials(
    tmp_path: Path,
) -> None:
    dataset, evaluation, _outcomes, config, trial_bundle, stress, created_at = _fixture(tmp_path)
    future = created_at + pd.Timedelta(days=1)
    evaluation.loc[len(evaluation) - 1, "prediction_time"] = future
    evaluation.loc[len(evaluation) - 1, "label_end_time"] = future + pd.Timedelta(minutes=30)
    evaluation.loc[len(evaluation) - 1, "max_feature_available_time"] = future - pd.Timedelta(
        minutes=1
    )
    with pytest.raises(ResearchExperimentError, match="prediction created in the future"):
        prepare_research_experiment(
            tmp_path / "future-lockbox",
            dataset_dir=dataset.directory,
            lockbox_claim_store=tmp_path / "claims",
            evaluation=evaluation,
            trial_run_dir=trial_bundle,
            cost_stress=stress,
            config=config,
            created_at=created_at,
            code_commit=COMMIT,
            dirty_worktree=False,
        )

    dataset, evaluation, _outcomes, config, trial_bundle, stress, created_at = _fixture(
        tmp_path / "old-trial"
    )
    run_path = trial_bundle / "run.json"
    run = json.loads(run_path.read_text(encoding="utf-8"))
    run["started_at"] = "2020-01-01T00:00:00+00:00"
    run_path.write_text(json.dumps(run, ensure_ascii=False, indent=2), encoding="utf-8")
    with pytest.raises(ResearchExperimentError, match="before the bound PIT dataset"):
        prepare_research_experiment(
            tmp_path / "old-trial-rejected",
            dataset_dir=dataset.directory,
            lockbox_claim_store=tmp_path / "old-trial-claims",
            evaluation=evaluation,
            trial_run_dir=trial_bundle,
            cost_stress=stress,
            config=config,
            created_at=created_at,
            code_commit=COMMIT,
            dirty_worktree=False,
        )


def test_mixed_iso_timestamp_precision_round_trips_through_self_audit(tmp_path: Path) -> None:
    dataset, evaluation, _outcomes, config, trial_bundle, stress, created_at = _fixture(tmp_path)
    values = []
    for position, value in enumerate(evaluation["max_feature_available_time"]):
        timestamp = pd.Timestamp(value)
        if position % 2:
            timestamp += pd.Timedelta(microseconds=123)
            values.append(timestamp.isoformat(timespec="microseconds"))
        else:
            values.append(timestamp.isoformat(timespec="seconds"))
    evaluation["max_feature_available_time"] = values

    artifact = prepare_research_experiment(
        tmp_path / "mixed-precision",
        dataset_dir=dataset.directory,
        lockbox_claim_store=tmp_path / "mixed-claims",
        evaluation=evaluation,
        trial_run_dir=trial_bundle,
        cost_stress=stress,
        config=config,
        created_at=created_at,
        code_commit=COMMIT,
        dirty_worktree=False,
    )

    assert audit_research_experiment(artifact.directory).passed


def test_shared_claim_store_blocks_duplicate_open_across_artifact_copies(tmp_path: Path) -> None:
    dataset, evaluation, outcomes, config, trial_bundle, stress, created_at = _fixture(tmp_path)
    claim_store = tmp_path / "shared-claims"
    first = prepare_research_experiment(
        tmp_path / "first",
        dataset_dir=dataset.directory,
        lockbox_claim_store=claim_store,
        evaluation=evaluation,
        trial_run_dir=trial_bundle,
        cost_stress=stress,
        config=config,
        created_at=created_at,
        code_commit=COMMIT,
        dirty_worktree=False,
    )
    second = prepare_research_experiment(
        tmp_path / "second",
        dataset_dir=dataset.directory,
        lockbox_claim_store=claim_store,
        evaluation=evaluation,
        trial_run_dir=trial_bundle,
        cost_stress=stress,
        config=config,
        created_at=created_at,
        code_commit=COMMIT,
        dirty_worktree=False,
    )

    assert first.experiment_id == second.experiment_id
    evaluate_lockbox_once(
        first.directory,
        outcomes,
        actor="first-reviewer",
        opened_at=datetime.now(UTC),
    )
    with pytest.raises(ResearchExperimentError, match="audit failed|already been consumed"):
        evaluate_lockbox_once(
            second.directory,
            outcomes,
            actor="second-reviewer",
            opened_at=datetime.now(UTC),
        )


def test_candidate_binding_and_unattested_cost_metrics_fail_closed_for_promotion(
    tmp_path: Path,
) -> None:
    dataset, evaluation, _outcomes, config, trial_bundle, stress, created_at = _fixture(tmp_path)
    mismatched = evaluation.copy()
    mismatched["candidate_id"] = "trial-1"
    with pytest.raises(ResearchExperimentError, match="selected trial"):
        prepare_research_experiment(
            tmp_path / "candidate-mismatch",
            dataset_dir=dataset.directory,
            lockbox_claim_store=tmp_path / "claims",
            evaluation=mismatched,
            trial_run_dir=trial_bundle,
            cost_stress=stress,
            config=config,
            created_at=created_at,
            code_commit=COMMIT,
            dirty_worktree=False,
        )

    extreme = stress.copy()
    extreme.loc[extreme["cost_multiplier"] == 2.0, "expectancy_r"] = 999.0
    artifact = prepare_research_experiment(
        tmp_path / "unattested-cost",
        dataset_dir=dataset.directory,
        lockbox_claim_store=tmp_path / "other-claims",
        evaluation=evaluation,
        trial_run_dir=trial_bundle,
        cost_stress=extreme,
        config=config,
        created_at=created_at,
        code_commit=COMMIT,
        dirty_worktree=False,
    )
    manifest = json.loads(artifact.manifest_path.read_text(encoding="utf-8"))

    assert manifest["analysis"]["cost_stress"]["scenarios"][2]["expectancy_r"] == 999.0
    assert manifest["analysis"]["promotion"]["evidence"]["cost_stress_2x_expectancy_r"] is None
    assert manifest["analysis"]["promotion"]["evidence"]["max_drawdown_pct"] is None


def test_opened_at_chronology_and_malformed_config_audit_fail_closed(tmp_path: Path) -> None:
    artifact, outcomes, created_at = _prepare(tmp_path)
    with pytest.raises(ResearchExperimentError, match="precede experiment creation"):
        evaluate_lockbox_once(
            artifact.directory,
            outcomes,
            actor="reviewer",
            opened_at=created_at - pd.Timedelta(seconds=1),
        )
    assert not (artifact.directory / "lockbox.open.json").exists()

    manifest = json.loads(artifact.manifest_path.read_text(encoding="utf-8"))
    manifest["identity"]["config"]["partition"]["purge_seconds"] = 10**1000
    manifest_bytes = (
        json.dumps(
            manifest,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        + b"\n"
    )
    artifact.manifest_path.write_bytes(manifest_bytes)
    (artifact.directory / "manifest.sha256").write_text(
        f"{hashlib.sha256(manifest_bytes).hexdigest()}\n",
        encoding="ascii",
    )

    audit = audit_research_experiment(artifact.directory)
    assert not audit.passed
    assert any("cannot be reconstructed" in error for error in audit.errors)
