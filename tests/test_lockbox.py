"""Lockbox registry and single-use governed evaluation."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from fx_backtester.experiment_pipeline import GitState, evaluate_lockbox, run_experiment
from fx_backtester.failures import FailureReason, TypedFailure
from fx_backtester.lockbox import LockboxRegistry

from test_experiment_pipeline import COMMIT, _manifest_dict, _write_prices

NOW = datetime(2026, 7, 12, 0, 0, tzinfo=UTC)


class TestRegistry:
    def test_register_and_untouched_state(self, tmp_path: Path) -> None:
        registry = LockboxRegistry(tmp_path)
        registry.register(
            experiment_id="exp-a",
            manifest_sha256="1" * 64,
            commitment_sha256="2" * 64,
            inputs_sha256="3" * 64,
            now=NOW,
        )
        state = registry.state("exp-a")
        assert state.registered is True
        assert state.evaluated_once is False
        assert state.reused_for_selection is False
        assert state.access_count == 0

    def test_identical_replay_allowed_content_change_rejected(self, tmp_path: Path) -> None:
        registry = LockboxRegistry(tmp_path)
        for _ in range(2):
            registry.register(
                experiment_id="exp-a",
                manifest_sha256="1" * 64,
                commitment_sha256="2" * 64,
                inputs_sha256="3" * 64,
                now=NOW,
            )
        with pytest.raises(TypedFailure) as excinfo:
            registry.register(
                experiment_id="exp-a",
                manifest_sha256="9" * 64,
                commitment_sha256="2" * 64,
                inputs_sha256="3" * 64,
                now=NOW,
            )
        assert excinfo.value.reason is FailureReason.LOCKBOX_VIOLATION

    def test_single_use_access(self, tmp_path: Path) -> None:
        registry = LockboxRegistry(tmp_path)
        registry.register(
            experiment_id="exp-a",
            manifest_sha256="1" * 64,
            commitment_sha256="2" * 64,
            inputs_sha256="3" * 64,
            now=NOW,
        )
        registry.claim_access(
            experiment_id="exp-a",
            manifest_sha256="1" * 64,
            purpose="final governance evaluation",
            actor="tester",
            now=NOW,
        )
        state = registry.state("exp-a")
        assert state.evaluated_once is True
        assert state.reused_for_selection is False
        with pytest.raises(TypedFailure) as excinfo:
            registry.claim_access(
                experiment_id="exp-a",
                manifest_sha256="1" * 64,
                purpose="second look",
                actor="tester",
                now=NOW,
            )
        assert excinfo.value.reason is FailureReason.LOCKBOX_VIOLATION
        outcomes = [entry["outcome"] for entry in registry.access_records("exp-a")]
        assert outcomes == ["claimed", "rejected_already_opened"]

    def test_manifest_mismatch_rejected_and_recorded(self, tmp_path: Path) -> None:
        registry = LockboxRegistry(tmp_path)
        registry.register(
            experiment_id="exp-a",
            manifest_sha256="1" * 64,
            commitment_sha256="2" * 64,
            inputs_sha256="3" * 64,
            now=NOW,
        )
        with pytest.raises(TypedFailure) as excinfo:
            registry.claim_access(
                experiment_id="exp-a",
                manifest_sha256="9" * 64,
                purpose="tampered",
                actor="tester",
                now=NOW,
            )
        assert excinfo.value.reason is FailureReason.LOCKBOX_VIOLATION
        outcomes = [entry["outcome"] for entry in registry.access_records("exp-a")]
        assert outcomes == ["rejected_manifest_mismatch"]
        assert registry.state("exp-a").evaluated_once is False

    def test_registration_purpose_and_actor_required(self, tmp_path: Path) -> None:
        registry = LockboxRegistry(tmp_path)
        registry.register(
            experiment_id="exp-a",
            manifest_sha256="1" * 64,
            commitment_sha256="2" * 64,
            inputs_sha256="3" * 64,
            now=NOW,
        )
        with pytest.raises(TypedFailure):
            registry.claim_access(
                experiment_id="exp-a",
                manifest_sha256="1" * 64,
                purpose=" ",
                actor="tester",
                now=NOW,
            )

    def test_missing_access_ledger_yields_unavailable_evidence(self, tmp_path: Path) -> None:
        registry = LockboxRegistry(tmp_path)
        registry.register(
            experiment_id="exp-a",
            manifest_sha256="1" * 64,
            commitment_sha256="2" * 64,
            inputs_sha256="3" * 64,
            now=NOW,
        )
        registry.claim_access(
            experiment_id="exp-a",
            manifest_sha256="1" * 64,
            purpose="final",
            actor="tester",
            now=NOW,
        )
        registry._access_ledger_path("exp-a").unlink()
        state = registry.state("exp-a")
        assert state.evaluated_once is None
        assert state.reused_for_selection is None


@pytest.fixture()
def pipeline_setup(tmp_path: Path) -> dict[str, Any]:
    csv_path = tmp_path / "USDJPY.csv"
    csv_sha = _write_prices(csv_path)
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(_manifest_dict(csv_path, csv_sha)), encoding="utf-8")
    return {
        "tmp_path": tmp_path,
        "csv_path": csv_path,
        "csv_sha": csv_sha,
        "manifest": manifest_path,
    }


def _run(setup: dict[str, Any], output_name: str = "out") -> Any:
    return run_experiment(
        setup["manifest"],
        output_root=setup["tmp_path"] / output_name,
        repository_root=setup["tmp_path"],
        git_state=GitState(commit=COMMIT, dirty=False),
    )


def _evaluate(setup: dict[str, Any], bundle_dir: Path) -> dict[str, Any]:
    return evaluate_lockbox(
        bundle_dir,
        purpose="final governance evaluation",
        actor="independent-verifier",
        repository_root=setup["tmp_path"],
        git_state=GitState(commit=COMMIT, dirty=False),
    )


class TestGovernedEvaluation:
    def test_single_use_end_to_end(self, pipeline_setup: dict[str, Any]) -> None:
        result = _run(pipeline_setup)
        assert "untouched_lockbox" in result.promotion_failures

        summary = _evaluate(pipeline_setup, result.output_dir)
        assert summary["promotion_passed"] is False
        assert (result.output_dir / "lockbox_result.json").is_file()
        post = json.loads(
            (result.output_dir / "promotion_decision_post_lockbox.json").read_text("utf-8")
        )
        assert "untouched_lockbox" not in post["failures"]
        assert "non_synthetic_data" in post["failures"]
        assert post["evidence"]["lockbox_evaluated_once"] is True

        with pytest.raises(TypedFailure) as excinfo:
            _evaluate(pipeline_setup, result.output_dir)
        assert excinfo.value.reason is FailureReason.LOCKBOX_VIOLATION

    def test_rerun_after_open_is_frozen(self, pipeline_setup: dict[str, Any]) -> None:
        result = _run(pipeline_setup)
        _evaluate(pipeline_setup, result.output_dir)
        with pytest.raises(TypedFailure) as excinfo:
            _run(pipeline_setup, "out-second")
        assert excinfo.value.reason is FailureReason.LOCKBOX_VIOLATION

    def test_bundle_tamper_blocks_access(self, pipeline_setup: dict[str, Any]) -> None:
        result = _run(pipeline_setup)
        evaluation_path = result.output_dir / "evaluation.json"
        payload = json.loads(evaluation_path.read_text("utf-8"))
        payload["test"]["net_expectancy_r"] = 9.99
        evaluation_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", "utf-8")
        with pytest.raises(TypedFailure) as excinfo:
            _evaluate(pipeline_setup, result.output_dir)
        assert excinfo.value.reason is FailureReason.HASH_MISMATCH
        registry = LockboxRegistry(pipeline_setup["tmp_path"] / "runs" / "lockbox_registry")
        assert registry.state(result.experiment_id).evaluated_once is False

    def test_changed_manifest_same_id_is_rejected(self, pipeline_setup: dict[str, Any]) -> None:
        _run(pipeline_setup)
        payload = json.loads(pipeline_setup["manifest"].read_text("utf-8"))
        payload["models"]["random_seed"] = 43
        pipeline_setup["manifest"].write_text(json.dumps(payload), encoding="utf-8")
        with pytest.raises(TypedFailure) as excinfo:
            _run(pipeline_setup, "out-changed")
        assert excinfo.value.reason is FailureReason.LOCKBOX_VIOLATION

    def test_rehashed_evidence_edit_is_still_detected(self, pipeline_setup: dict[str, Any]) -> None:
        """Editing recorded evidence AND regenerating artifact hashes must fail."""

        import hashlib

        result = _run(pipeline_setup)
        decision_path = result.output_dir / "promotion_decision.json"
        payload = json.loads(decision_path.read_text("utf-8"))
        payload["evidence"]["net_expectancy_r"] = 9.99
        decision_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", "utf-8")
        hashes_path = result.output_dir / "artifact_hashes.json"
        hashes = json.loads(hashes_path.read_text("utf-8"))
        hashes["artifacts"]["promotion_decision.json"] = hashlib.sha256(
            decision_path.read_bytes()
        ).hexdigest()
        hashes_path.write_text(json.dumps(hashes, indent=2, sort_keys=True) + "\n", "utf-8")
        with pytest.raises(TypedFailure) as excinfo:
            _evaluate(pipeline_setup, result.output_dir)
        assert excinfo.value.reason is FailureReason.LINEAGE_BROKEN
        registry = LockboxRegistry(pipeline_setup["tmp_path"] / "runs" / "lockbox_registry")
        assert registry.state(result.experiment_id).evaluated_once is False

    def test_rehashed_statistics_edit_is_still_detected(
        self, pipeline_setup: dict[str, Any]
    ) -> None:
        """DSR/PBO/CI/cost-stress values in the evidence are also replay-verified."""

        import hashlib

        result = _run(pipeline_setup)
        decision_path = result.output_dir / "promotion_decision.json"
        payload = json.loads(decision_path.read_text("utf-8"))
        payload["evidence"]["dsr_probability"] = 0.999
        decision_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", "utf-8")
        hashes_path = result.output_dir / "artifact_hashes.json"
        hashes = json.loads(hashes_path.read_text("utf-8"))
        hashes["artifacts"]["promotion_decision.json"] = hashlib.sha256(
            decision_path.read_bytes()
        ).hexdigest()
        hashes_path.write_text(json.dumps(hashes, indent=2, sort_keys=True) + "\n", "utf-8")
        with pytest.raises(TypedFailure) as excinfo:
            _evaluate(pipeline_setup, result.output_dir)
        assert excinfo.value.reason is FailureReason.LINEAGE_BROKEN
        assert any(
            item["field"] == "dsr_probability" for item in excinfo.value.context["mismatched"]
        )

    def test_registry_wipe_cannot_reopen_an_evaluated_bundle(
        self, pipeline_setup: dict[str, Any]
    ) -> None:
        import shutil

        result = _run(pipeline_setup)
        _evaluate(pipeline_setup, result.output_dir)
        shutil.rmtree(pipeline_setup["tmp_path"] / "runs" / "lockbox_registry")
        with pytest.raises(TypedFailure) as excinfo:
            _evaluate(pipeline_setup, result.output_dir)
        assert excinfo.value.reason is FailureReason.LOCKBOX_VIOLATION

    def test_ledger_records_all_candidates_and_failures(
        self, pipeline_setup: dict[str, Any]
    ) -> None:
        payload = json.loads(pipeline_setup["manifest"].read_text("utf-8"))
        payload["models"]["candidates"].append(
            {
                "candidate_id": "broken-hyper",
                "family": "logistic_ridge",
                "hyperparameters": {
                    "ridge": 1.0,
                    "bogus": 1.0,
                    "long_threshold": 0.52,
                    "short_threshold": 0.48,
                },
            }
        )
        pipeline_setup["manifest"].write_text(json.dumps(payload), encoding="utf-8")
        result = _run(pipeline_setup)
        decision = json.loads((result.output_dir / "promotion_decision.json").read_text("utf-8"))
        assert decision["evidence"]["trial_count"] == 4

        from fx_backtester.trial_ledger import TrialLedger

        ledger = TrialLedger(pipeline_setup["tmp_path"] / "runs" / "trial_ledger.jsonl")
        entries = ledger.entries_for_experiment(result.experiment_id)
        statuses = {entry["extra"]["candidate_id"]: entry["status"] for entry in entries}
        assert statuses["broken-hyper"] == "failed"
        assert sum(1 for entry in entries if entry["selected"]) == 1
