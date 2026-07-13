"""Authoritative experiment pipeline: manifest contract, leakage, reproducibility."""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytest

from fx_backtester.experiment_manifest import (
    load_experiment_manifest,
    manifest_sha256,
    parse_experiment_manifest,
)
from fx_backtester.experiment_pipeline import (
    NO_TRADE_CANDIDATE_ID,
    GitState,
    main,
    run_experiment,
)
from fx_backtester.failures import FailureReason, TypedFailure

COMMIT = "a" * 40
PYTHON_VERSION = f"{sys.version_info.major}.{sys.version_info.minor}"


def _write_prices(path: Path, *, rows: int = 700, seed: int = 7) -> str:
    rng = np.random.default_rng(seed)
    index = pd.date_range("2024-01-02", periods=rows, freq="1h", tz="UTC")
    drift = np.sin(np.arange(rows) / 24.0) * 0.02
    noise = rng.normal(0.0, 0.05, size=rows)
    close = 145.0 + np.cumsum(drift + noise)
    open_ = np.concatenate([[close[0]], close[:-1]])
    wick = np.abs(rng.normal(0.0, 0.01, size=rows)) + 0.002
    high = np.maximum(open_, close) + wick
    low = np.minimum(open_, close) - wick
    frame = pd.DataFrame(
        {
            "timestamp": index.strftime("%Y-%m-%d %H:%M:%S%z"),
            "symbol": "USDJPY",
            "open": open_,
            "high": high,
            "low": low,
            "close": close,
        }
    )
    frame.to_csv(path, index=False)
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _manifest_dict(csv_path: Path, csv_sha: str, **overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema_version": 1,
        "experiment_id": "usdjpy-pipeline-selftest",
        "created_at": "2026-07-12T00:00:00+00:00",
        "created_by": "pipeline-tests",
        "research_question": "Does the deterministic pipeline reproduce itself?",
        "economic_hypothesis": "None; this is an infrastructure self-test on synthetic data.",
        "invalidation_conditions": ["Any nondeterministic artifact hash invalidates the run."],
        "strategy_card": {
            "strategy_id": "selftest-momentum-v1",
            "economic_mechanism": "None claimed; synthetic self-test of pipeline mechanics.",
            "why_should_edge_exist": "It should not; synthetic data has no economic edge.",
            "who_is_paying_the_edge": "Nobody; the generator is a seeded random walk.",
            "expected_horizon": "12 hourly bars",
            "known_failure_regimes": ["all real-market regimes"],
            "capacity_assumption": "not applicable (synthetic)",
            "cost_assumption": "declared static spread/slippage/commission/financing",
            "exploratory": True,
        },
        "git": {"commit": COMMIT, "dirty_worktree_allowed": False},
        "environment": {
            "python_version": PYTHON_VERSION,
            "dependency_lock_sha256": None,
            "platform_note": "test-environment",
        },
        "data": {
            "sources": [
                {
                    "source_id": "synthetic_prices",
                    "kind": "price_csv",
                    "path": str(csv_path),
                    "raw_sha256": csv_sha,
                    "license_note": "synthetic test data; no licence required",
                }
            ],
            "symbol": "USDJPY",
            "start": "2024-01-02T00:00:00+00:00",
            "end": "2024-02-15T00:00:00+00:00",
            "timezone": "UTC",
            "as_of_cutoff": "2024-02-15T02:00:00+00:00",
            "bar_interval_minutes": 60,
            "required_quality_level": "strict",
            "synthetic": True,
        },
        "features": {
            "definitions": ["ret_1", "ret_4", "ma_ratio_24", "rsi_14", "vol_24"],
            "availability_rule": "completed_bar_close",
            "version": "features_v1",
        },
        "labels": {
            "type": "triple_barrier",
            "horizon_bars": 12,
            "take_profit_vol_multiple": 2.0,
            "stop_vol_multiple": 1.0,
            "volatility_window_bars": 24,
            "same_bar_policy": "stop_first",
            "gap_policy": "adverse_open",
        },
        "splits": {
            "method": "chronological_five_way",
            "train_fraction": 0.5,
            "tune_fraction": 0.15,
            "calibration_fraction": 0.1,
            "test_fraction": 0.15,
            "lockbox_fraction": 0.1,
            "purge_bars": 12,
            "embargo_bars": 2,
            "min_rows_per_partition": 10,
        },
        "models": {
            "candidates": [
                {
                    "candidate_id": "noskill-flat",
                    "family": "constant_probability",
                    "hyperparameters": {
                        "probability": 0.5,
                        "long_threshold": 0.55,
                        "short_threshold": 0.45,
                    },
                },
                {
                    "candidate_id": "logistic-a",
                    "family": "logistic_ridge",
                    "hyperparameters": {
                        "ridge": 1.0,
                        "long_threshold": 0.52,
                        "short_threshold": 0.48,
                    },
                },
                {
                    "candidate_id": "logistic-b",
                    "family": "logistic_ridge",
                    "hyperparameters": {
                        "ridge": 10.0,
                        "long_threshold": 0.52,
                        "short_threshold": 0.48,
                    },
                },
            ],
            "random_seed": 42,
            "trial_budget": 8,
        },
        "calibration": {"method": "platt"},
        "costs": {
            "cost_model_version": "declared_static_v1",
            "spread_pips": 0.8,
            "slippage_pips": 0.2,
            "pip_size": 0.01,
            "commission_r_per_trade": 0.001,
            "financing_r_per_bar": 0.0001,
            "stress_multipliers": [1.0, 1.25, 1.5, 2.0],
        },
        "selection": {
            "primary_metric": "net_expectancy_r",
            "minimum_trade_count": 5,
            "minimum_effective_trades": 3,
            "max_regime_concentration": 1.0,
            "max_month_concentration": 1.0,
            "multiple_testing_method": "holm",
            "bootstrap_block_size": 5,
            "pbo_blocks": 8,
        },
        "lockbox": {"access_policy": "single_use", "access_count_limit": 1},
        "promotion": {"target_stage": "validated", "policy_path": None},
    }
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(payload.get(key), dict):
            payload[key] = {**payload[key], **value}
        else:
            payload[key] = value
    return payload


@pytest.fixture()
def experiment_setup(tmp_path: Path) -> dict[str, Any]:
    csv_path = tmp_path / "USDJPY.csv"
    csv_sha = _write_prices(csv_path)
    (tmp_path / "requirements.lock").write_text("numpy==2.3.5\n", encoding="utf-8")
    return {"tmp_path": tmp_path, "csv_path": csv_path, "csv_sha": csv_sha}


def _write_manifest(setup: dict[str, Any], name: str = "manifest.json", **overrides: Any) -> Path:
    payload = _manifest_dict(setup["csv_path"], setup["csv_sha"], **overrides)
    path = setup["tmp_path"] / name
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _run(setup: dict[str, Any], manifest_path: Path, output_name: str = "out") -> Any:
    return run_experiment(
        manifest_path,
        output_root=setup["tmp_path"] / output_name,
        repository_root=setup["tmp_path"],
        git_state=GitState(commit=COMMIT, dirty=False),
    )


class TestManifestContract:
    def test_valid_manifest_round_trips_and_hashes(self, experiment_setup: dict[str, Any]) -> None:
        manifest_path = _write_manifest(experiment_setup)
        manifest = load_experiment_manifest(manifest_path)
        assert manifest.experiment_id == "usdjpy-pipeline-selftest"
        first = manifest_sha256(manifest)
        second = manifest_sha256(load_experiment_manifest(manifest_path))
        assert first == second

    def test_unknown_key_is_rejected(self, experiment_setup: dict[str, Any]) -> None:
        payload = _manifest_dict(experiment_setup["csv_path"], experiment_setup["csv_sha"])
        payload["surprise"] = 1
        with pytest.raises(TypedFailure) as excinfo:
            parse_experiment_manifest(payload)
        assert excinfo.value.reason is FailureReason.INVALID

    def test_missing_section_is_incomplete(self, experiment_setup: dict[str, Any]) -> None:
        payload = _manifest_dict(experiment_setup["csv_path"], experiment_setup["csv_sha"])
        del payload["costs"]
        with pytest.raises(TypedFailure) as excinfo:
            parse_experiment_manifest(payload)
        assert excinfo.value.reason is FailureReason.INCOMPLETE

    def test_naive_datetime_is_rejected(self, experiment_setup: dict[str, Any]) -> None:
        with pytest.raises(TypedFailure):
            parse_experiment_manifest(
                _manifest_dict(
                    experiment_setup["csv_path"],
                    experiment_setup["csv_sha"],
                    created_at="2026-07-12T00:00:00",
                )
            )

    def test_required_stress_multipliers(self, experiment_setup: dict[str, Any]) -> None:
        payload = _manifest_dict(experiment_setup["csv_path"], experiment_setup["csv_sha"])
        payload["costs"]["stress_multipliers"] = [1.0, 2.0]
        with pytest.raises(TypedFailure) as excinfo:
            parse_experiment_manifest(payload)
        assert excinfo.value.reason is FailureReason.INCOMPLETE

    def test_candidates_over_trial_budget(self, experiment_setup: dict[str, Any]) -> None:
        payload = _manifest_dict(experiment_setup["csv_path"], experiment_setup["csv_sha"])
        payload["models"]["trial_budget"] = 1
        with pytest.raises(TypedFailure) as excinfo:
            parse_experiment_manifest(payload)
        assert excinfo.value.reason is FailureReason.INVALID

    def test_end_beyond_cutoff_is_rejected(self, experiment_setup: dict[str, Any]) -> None:
        payload = _manifest_dict(experiment_setup["csv_path"], experiment_setup["csv_sha"])
        payload["data"]["as_of_cutoff"] = "2024-02-10T00:00:00+00:00"
        with pytest.raises(TypedFailure):
            parse_experiment_manifest(payload)


class TestPipelineRun:
    def test_end_to_end_denies_promotion_on_synthetic_data(
        self, experiment_setup: dict[str, Any]
    ) -> None:
        result = _run(experiment_setup, _write_manifest(experiment_setup))
        assert result.promotion_passed is False
        assert "non_synthetic_data" in result.promotion_failures
        assert "untouched_lockbox" in result.promotion_failures
        for name in (
            "manifest.json",
            "data_lineage.json",
            "dataset_rows.jsonl",
            "lockbox.json",
            "evaluation.json",
            "cost_stress.json",
            "promotion_decision.json",
            "environment.json",
            "git.json",
            "trial_ledger_snapshot.jsonl",
            "run_info.json",
            "artifact_hashes.json",
        ):
            assert (result.output_dir / name).is_file(), name

        lineage = json.loads((result.output_dir / "data_lineage.json").read_text("utf-8"))
        artifacts = [entry["artifact"] for entry in lineage["chain"]]
        for expected in (
            "manifest",
            "raw:synthetic_prices",
            "normalized_dataset",
            "feature_dataset",
            "label_dataset",
            "split",
            "trained_model",
            "test_predictions",
            "evaluation",
            "cost_stress",
            "promotion_decision",
        ):
            assert expected in artifacts

        stress = json.loads((result.output_dir / "cost_stress.json").read_text("utf-8"))
        assert [row["cost_multiplier"] for row in stress["rows"]] == [1.0, 1.25, 1.5, 2.0]

        ledger = [
            json.loads(line)
            for line in (result.output_dir / "trial_ledger_snapshot.jsonl")
            .read_text("utf-8")
            .splitlines()
        ]
        # The flat book (no-trade) is injected into every run as an explicit,
        # ledgered candidate alongside the three declared ones.
        assert len(ledger) == 4
        assert {entry["candidate_id"] for entry in ledger} == {
            "noskill-flat",
            "logistic-a",
            "logistic-b",
            NO_TRADE_CANDIDATE_ID,
        }

    def test_lockbox_outcomes_are_withheld(self, experiment_setup: dict[str, Any]) -> None:
        result = _run(experiment_setup, _write_manifest(experiment_setup))
        lockbox = json.loads((result.output_dir / "lockbox.json").read_text("utf-8"))
        assert lockbox["status"] == "unopened"
        assert lockbox["row_count"] > 0
        evaluation = json.loads((result.output_dir / "evaluation.json").read_text("utf-8"))
        dataset_rows = [
            json.loads(line)
            for line in (result.output_dir / "dataset_rows.jsonl").read_text("utf-8").splitlines()
        ]
        development_rows = evaluation["partition_audit"]
        total_development = (
            development_rows["train_rows"]
            + development_rows["tune_rows"]
            + development_rows["calibration_rows"]
            + development_rows["test_rows"]
        )
        assert len(dataset_rows) >= total_development
        raw_bundle = (result.output_dir / "lockbox.json").read_text("utf-8")
        assert "gross_r" not in raw_bundle
        assert "label_up" not in raw_bundle

    def test_run_is_reproducible(self, experiment_setup: dict[str, Any]) -> None:
        manifest_path = _write_manifest(experiment_setup)
        first = _run(experiment_setup, manifest_path, "out-a")
        second = _run(experiment_setup, manifest_path, "out-b")
        assert first.deterministic_result_sha256 == second.deterministic_result_sha256
        assert first.manifest_sha256 == second.manifest_sha256

    def test_no_trade_run_is_reproducible(self, experiment_setup: dict[str, Any]) -> None:
        # The flat-book path must not leak wall-clock time into the lineage:
        # two runs where no-trade wins produce identical deterministic hashes.
        payload = _manifest_dict(experiment_setup["csv_path"], experiment_setup["csv_sha"])
        for candidate in payload["models"]["candidates"]:
            candidate["hyperparameters"]["long_threshold"] = 0.999
            candidate["hyperparameters"]["short_threshold"] = 0.001
        manifest_path = experiment_setup["tmp_path"] / "manifest.json"
        manifest_path.write_text(json.dumps(payload), encoding="utf-8")
        first = _run(experiment_setup, manifest_path, "out-a")
        second = _run(experiment_setup, manifest_path, "out-b")
        assert first.selected_candidate_id == NO_TRADE_CANDIDATE_ID
        assert first.deterministic_result_sha256 == second.deterministic_result_sha256

    def test_seed_change_requires_new_experiment_and_changes_identity(
        self, experiment_setup: dict[str, Any]
    ) -> None:
        base = _run(experiment_setup, _write_manifest(experiment_setup), "out-a")
        reseeded_path = _write_manifest(
            experiment_setup,
            name="manifest-b.json",
            experiment_id="usdjpy-pipeline-selftest-seed43",
            models={"random_seed": 43},
        )
        reseeded = _run(experiment_setup, reseeded_path, "out-b")
        assert base.manifest_sha256 != reseeded.manifest_sha256
        assert base.deterministic_result_sha256 != reseeded.deterministic_result_sha256

    def test_dirty_worktree_rejects_formal_claim(self, experiment_setup: dict[str, Any]) -> None:
        manifest_path = _write_manifest(experiment_setup)
        with pytest.raises(TypedFailure) as excinfo:
            run_experiment(
                manifest_path,
                output_root=experiment_setup["tmp_path"] / "out",
                repository_root=experiment_setup["tmp_path"],
                git_state=GitState(commit=COMMIT, dirty=True),
            )
        assert excinfo.value.reason is FailureReason.INVALID

    def test_commit_mismatch_is_rejected(self, experiment_setup: dict[str, Any]) -> None:
        manifest_path = _write_manifest(experiment_setup)
        with pytest.raises(TypedFailure):
            run_experiment(
                manifest_path,
                output_root=experiment_setup["tmp_path"] / "out",
                repository_root=experiment_setup["tmp_path"],
                git_state=GitState(commit="b" * 40, dirty=False),
            )

    def test_raw_hash_mismatch_fails_closed(self, experiment_setup: dict[str, Any]) -> None:
        manifest_path = _write_manifest(experiment_setup, data={"sources": None})
        payload = _manifest_dict(experiment_setup["csv_path"], experiment_setup["csv_sha"])
        payload["data"]["sources"][0]["raw_sha256"] = "0" * 64
        manifest_path.write_text(json.dumps(payload), encoding="utf-8")
        with pytest.raises(TypedFailure) as excinfo:
            _run(experiment_setup, manifest_path)
        assert excinfo.value.reason is FailureReason.HASH_MISMATCH

    def test_dependency_lock_mismatch_fails_closed(self, experiment_setup: dict[str, Any]) -> None:
        manifest_path = _write_manifest(
            experiment_setup, environment={"dependency_lock_sha256": "1" * 64}
        )
        with pytest.raises(TypedFailure) as excinfo:
            _run(experiment_setup, manifest_path)
        assert excinfo.value.reason is FailureReason.HASH_MISMATCH

    def test_holds_cash_when_no_candidate_qualifies(self, experiment_setup: dict[str, Any]) -> None:
        # Thresholds so extreme that no declared candidate trades: rather than
        # failing, the run selects the flat book, holds cash and denies promotion
        # for want of a sample (§1-2). A model-free evidence bundle is emitted.
        payload = _manifest_dict(experiment_setup["csv_path"], experiment_setup["csv_sha"])
        for candidate in payload["models"]["candidates"]:
            candidate["hyperparameters"]["long_threshold"] = 0.999
            candidate["hyperparameters"]["short_threshold"] = 0.001
        manifest_path = experiment_setup["tmp_path"] / "manifest.json"
        manifest_path.write_text(json.dumps(payload), encoding="utf-8")
        result = _run(experiment_setup, manifest_path)
        assert result.promotion_passed is False
        evaluation = json.loads((result.output_dir / "evaluation.json").read_text("utf-8"))
        assert evaluation["selected_candidate_id"] == NO_TRADE_CANDIDATE_ID
        assert evaluation["performance_claim"] == "no_trade_selected"
        assert evaluation["test"]["trade_count"] == 0
        # No trained_model artifact is bound when the flat book wins.
        lineage = json.loads((result.output_dir / "data_lineage.json").read_text("utf-8"))
        artifacts = [entry["artifact"] for entry in lineage["chain"]]
        assert "trained_model" not in artifacts

    def test_experiment_output_directory_is_single_use(
        self, experiment_setup: dict[str, Any]
    ) -> None:
        manifest_path = _write_manifest(experiment_setup)
        _run(experiment_setup, manifest_path, "out")
        with pytest.raises(TypedFailure) as excinfo:
            _run(experiment_setup, manifest_path, "out")
        assert excinfo.value.reason is FailureReason.INVALID


class TestCli:
    def test_cli_reports_typed_failure_on_commit_mismatch(
        self, experiment_setup: dict[str, Any], capsys: pytest.CaptureFixture[str]
    ) -> None:
        manifest_path = _write_manifest(experiment_setup)
        exit_code = main(
            [
                "run",
                "--experiment-manifest",
                str(manifest_path),
                "--output-root",
                str(experiment_setup["tmp_path"] / "cli-out"),
            ]
        )
        assert exit_code == 1
        payload = json.loads(capsys.readouterr().out.strip())
        assert payload["status"] == "failed"
        assert payload["reason"] in {"invalid", "unavailable"}
