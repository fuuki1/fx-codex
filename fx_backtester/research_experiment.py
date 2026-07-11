"""Two-phase, research-only experiment evidence and durable lockbox claims.

This module binds precomputed candidate predictions, an expected tune-window
trial list, calibrated descriptive test diagnostics, and declared cost-stress
rows into one content-addressed manifest. It deliberately does not claim that
these inputs were produced by an authoritative trainer or engine, so their
performance, point-in-time lineage, and test separation remain unavailable
promotion evidence.

Lockbox outcomes are absent from the prepared artifact. A second phase persists
a create-exclusive, fsynced experiment-ID claim in a configured shared local
store before reading outcomes or opening the in-memory guard. A crash consumes
that local claim. This is not global custody, a secure enclave, an external
signature, or an object lock.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from fx_backtester.calibration import (
    CalibrationMethod,
    ProbabilityCalibrator,
    calibration_metrics,
    fit_calibrator,
)
from fx_backtester.governance import (
    PromotionEvidence,
    PromotionPolicy,
    evaluate_promotion,
)
from fx_backtester.overfitting import (
    deflated_sharpe_ratio,
    per_period_sharpe,
    probability_of_backtest_overfitting,
)
from fx_backtester.pit_dataset import audit_pit_dataset
from fx_backtester.point_in_time import PointInTimeError, utc_datetime
from fx_backtester.statistical_validation import (
    block_sign_permutation_test,
    circular_block_bootstrap_mean_ci,
    minimum_track_record_length,
    probabilistic_sharpe_ratio,
)
from fx_backtester.time_series_validation import (
    ModelPartitionConfig,
    ModelPartitions,
    chronological_model_partitions,
)
from fx_backtester.trial_log import (
    RETURNS_MATRIX_FILENAME,
    RUN_FILENAME,
    TRIALS_FILENAME,
    read_returns_matrix,
    read_trials,
)


class ResearchExperimentError(ValueError):
    """Raised when research evidence is incomplete, inconsistent, or reused."""


_SHA256 = re.compile(r"[0-9a-f]{64}")
_GIT_COMMIT = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
_CALIBRATORS = frozenset({"platt", "isotonic", "beta"})
_EVALUATION_COLUMNS = (
    "sample_id",
    "dataset_id",
    "candidate_id",
    "model_artifact_sha256",
    "prediction_time",
    "label_end_time",
    "max_feature_available_time",
    "pair",
    "horizon",
    "regime",
    "raw_probability",
    "label",
    "net_r",
)
_STRESS_COLUMNS = (
    "scenario",
    "cost_multiplier",
    "method",
    "dataset_id",
    "candidate_id",
    "model_artifact_sha256",
    "trade_count",
    "expectancy_r",
    "max_drawdown",
    "execution_config",
)
_LOCKBOX_OUTCOME_COLUMNS = ("sample_id", "outcome_available_time", "label", "net_r")
_STRESS_SCENARIOS = {
    1.0: "observed",
    1.5: "cost_1_5x",
    2.0: "cost_2x",
    3.0: "cost_3x",
}
_BASE_FILES = frozenset(
    {
        "evaluation.jsonl",
        "cost_stress.json",
        "trial_bundle",
        "manifest.json",
        "manifest.sha256",
    }
)
_LOCKBOX_FILES = frozenset({"lockbox.open.json", "lockbox.result.json", "lockbox.result.sha256"})


@dataclass(frozen=True)
class ResearchExperimentConfig:
    """Fixed choices supplied to the binder; independent pre-registration is unattested."""

    hypothesis: str
    pair: str
    horizon: str
    lockbox_purpose: str
    expected_trial_ids: tuple[str, ...]
    calibrator_method: CalibrationMethod = "platt"
    partition_config: ModelPartitionConfig = field(default_factory=ModelPartitionConfig)
    bootstrap_block_size: int = 4
    bootstrap_resamples: int = 2_000
    permutations: int = 5_000
    pbo_blocks: int = 8
    seed: int = 42
    promotion_policy: PromotionPolicy = field(default_factory=PromotionPolicy)

    def __post_init__(self) -> None:
        for name in ("hypothesis", "pair", "horizon", "lockbox_purpose"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ResearchExperimentError(f"{name} must be a non-empty string")
            object.__setattr__(self, name, value.strip())
        if self.calibrator_method not in _CALIBRATORS:
            raise ResearchExperimentError("unsupported calibrator_method")
        if (
            not isinstance(self.expected_trial_ids, tuple)
            or len(self.expected_trial_ids) < 2
            or any(
                not isinstance(trial_id, str) or not trial_id.strip()
                for trial_id in self.expected_trial_ids
            )
            or len(self.expected_trial_ids) != len(set(self.expected_trial_ids))
        ):
            raise ResearchExperimentError(
                "expected_trial_ids must contain at least two unique non-empty IDs"
            )
        object.__setattr__(
            self,
            "expected_trial_ids",
            tuple(trial_id.strip() for trial_id in self.expected_trial_ids),
        )
        if not isinstance(self.partition_config, ModelPartitionConfig):
            raise ResearchExperimentError("partition_config must be ModelPartitionConfig")
        if self.bootstrap_block_size < 1:
            raise ResearchExperimentError("bootstrap_block_size must be positive")
        if self.bootstrap_resamples < 100 or self.permutations < 100:
            raise ResearchExperimentError("resampling counts must be at least 100")
        if self.pbo_blocks < 4 or self.pbo_blocks % 2:
            raise ResearchExperimentError("pbo_blocks must be an even integer >= 4")
        if not isinstance(self.seed, int) or isinstance(self.seed, bool):
            raise ResearchExperimentError("seed must be an integer")
        if not isinstance(self.promotion_policy, PromotionPolicy):
            raise ResearchExperimentError("promotion_policy must be PromotionPolicy")


@dataclass(frozen=True)
class ResearchExperimentArtifact:
    experiment_id: str
    directory: Path
    manifest_path: Path


@dataclass(frozen=True)
class ResearchExperimentAudit:
    passed: bool
    errors: tuple[str, ...]
    warnings: tuple[str, ...]
    manifest: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class LockboxEvaluation:
    experiment_id: str
    result_path: Path
    result: Mapping[str, Any]


@dataclass(frozen=True)
class _TrialBundle:
    run: Mapping[str, Any]
    trials: tuple[Mapping[str, Any], ...]
    matrix: pd.DataFrame
    selected_trial_id: str
    raw_files: Mapping[str, bytes]
    fingerprints: tuple[Mapping[str, Any], ...]


def prepare_research_experiment(
    root: str | Path,
    *,
    dataset_dir: str | Path,
    lockbox_claim_store: str | Path,
    evaluation: pd.DataFrame,
    trial_run_dir: str | Path,
    cost_stress: pd.DataFrame,
    config: ResearchExperimentConfig,
    created_at: datetime,
    code_commit: str,
    dirty_worktree: bool,
) -> ResearchExperimentArtifact:
    """Bind descriptive test evidence while excluding lockbox outcomes."""

    if not isinstance(config, ResearchExperimentConfig):
        raise ResearchExperimentError("config must be ResearchExperimentConfig")
    created = _creation_time(created_at, "created_at")
    _validate_code_provenance(code_commit, dirty_worktree)

    dataset_path, dataset_manifest = _audited_dataset(dataset_dir)
    dataset_id = str(dataset_manifest["dataset_id"])
    normalized, partitions, evaluation_bytes = _prepare_evaluation(evaluation, dataset_id, config)
    _validate_block_sizes(config, partitions)
    tune_times = _prediction_times(normalized, partitions.tune)
    bundle = _load_trial_bundle(
        Path(trial_run_dir),
        dataset_id=dataset_id,
        pair=config.pair,
        horizon=config.horizon,
        tune_times=tune_times,
        expected_trial_ids=config.expected_trial_ids,
    )
    _validate_candidate_binding(normalized, bundle)
    _validate_input_chronology(created, dataset_manifest, bundle, normalized, partitions)
    claim_store = _claim_store(lockbox_claim_store, create=True)
    stress_rows, stress_bytes = _prepare_cost_stress(
        cost_stress,
        len(partitions.test),
        dataset_id=dataset_id,
        candidate_id=bundle.selected_trial_id,
        model_artifact_sha256=_selected_model_hash(bundle),
    )
    analysis = _prepared_analysis(
        normalized,
        partitions,
        bundle,
        stress_rows,
        config,
        dataset_manifest,
        code_commit,
        dirty_worktree,
    )

    identity = _experiment_identity(
        created=created,
        code_commit=code_commit,
        dirty_worktree=dirty_worktree,
        claim_store=claim_store,
        dataset_manifest=dataset_manifest,
        config=config,
        evaluation_bytes=evaluation_bytes,
        evaluation_rows=len(normalized),
        bundle=bundle,
        stress_bytes=stress_bytes,
    )
    experiment_id = _sha256(_canonical_bytes(identity))
    manifest = _experiment_manifest(
        experiment_id,
        identity,
        analysis,
        dataset_manifest,
        dataset_path=dataset_path,
    )
    manifest_bytes = _canonical_bytes(manifest) + b"\n"

    root_path = Path(root).expanduser().resolve()
    root_path.mkdir(parents=True, exist_ok=True)
    if not root_path.is_dir():
        raise ResearchExperimentError(f"experiment root is not a directory: {root_path}")
    destination = root_path / experiment_id
    artifact = ResearchExperimentArtifact(experiment_id, destination, destination / "manifest.json")
    if destination.exists():
        return _existing_experiment_or_raise(artifact)
    try:
        destination.mkdir()
    except FileExistsError:
        return _existing_experiment_or_raise(artifact)

    incomplete = destination / ".incomplete"
    _exclusive_write(incomplete, b"experiment materialization in progress\n")
    trial_destination = destination / "trial_bundle"
    trial_destination.mkdir()
    for name, content in bundle.raw_files.items():
        _exclusive_write(trial_destination / name, content)
    _fsync_directory(trial_destination)
    _exclusive_write(destination / "evaluation.jsonl", evaluation_bytes)
    _exclusive_write(destination / "cost_stress.json", stress_bytes)
    _exclusive_write(artifact.manifest_path, manifest_bytes)
    _exclusive_write(
        destination / "manifest.sha256",
        f"{_sha256(manifest_bytes)}\n".encode("ascii"),
    )
    incomplete.unlink()
    _fsync_directory(destination)
    _fsync_directory(root_path)

    audit = audit_research_experiment(destination)
    if not audit.passed:
        _exclusive_write(incomplete, b"experiment failed self-audit\n")
        raise ResearchExperimentError(
            f"materialized experiment failed self-audit: {'; '.join(audit.errors)}"
        )
    return artifact


def evaluate_lockbox_once(
    experiment_dir: str | Path,
    lockbox_outcomes: pd.DataFrame,
    *,
    actor: str,
    opened_at: datetime,
) -> LockboxEvaluation:
    """Claim externally held outcomes in the configured local claim namespace.

    The shared marker is created and fsynced before this function reads any
    outcome value or opens ``ModelPartitions``. Any later failure consumes the
    local claim and cannot be retried from another artifact copy using the same
    configured store. This remains local procedural evidence, not global custody.
    """

    directory = Path(experiment_dir).expanduser().resolve()
    audit = audit_research_experiment(directory)
    if not audit.passed:
        raise ResearchExperimentError(f"experiment audit failed: {'; '.join(audit.errors)}")
    marker_path = directory / "lockbox.open.json"
    if marker_path.exists():
        raise ResearchExperimentError("lockbox has already been consumed")
    actor = _required_text(actor, "actor")
    opened = _creation_time(opened_at, "opened_at")
    manifest = dict(audit.manifest)
    identity = manifest["identity"]
    config = _config_from_dict(identity["config"])
    if opened < _utc(identity["created_at"], "identity.created_at"):
        raise ResearchExperimentError("opened_at cannot precede experiment creation")
    manifest_bytes = (directory / "manifest.json").read_bytes()
    claim_store = _claim_store(identity["lockbox_claim_store"], create=False)
    shared_marker_path = claim_store / f"{manifest['experiment_id']}.open.json"
    if shared_marker_path.exists():
        raise ResearchExperimentError("lockbox has already been consumed in the claim store")
    marker = {
        "schema_version": 1,
        "state": "consumed_local_claim_scope",
        "experiment_id": manifest["experiment_id"],
        "manifest_sha256": _sha256(manifest_bytes),
        "lockbox_commitment_sha256": manifest["analysis"]["partitions"]["lockbox"][
            "commitment_sha256"
        ],
        "purpose": config.lockbox_purpose,
        "claim_store": str(claim_store),
        "actor": actor,
        "opened_at": opened.isoformat(),
    }
    marker_bytes = _canonical_bytes(marker) + b"\n"
    try:
        _exclusive_write(shared_marker_path, marker_bytes)
    except ResearchExperimentError as error:
        if shared_marker_path.exists():
            raise ResearchExperimentError(
                "lockbox has already been consumed in the claim store"
            ) from error
        raise
    _fsync_directory(claim_store)
    _exclusive_write(marker_path, marker_bytes)
    _fsync_directory(directory)

    # No operation above this line reads label/net_r outcomes or opens the lockbox.
    try:
        evaluation = _evaluation_from_bytes((directory / "evaluation.jsonl").read_bytes())
        evaluation, partitions, _content = _prepare_evaluation(
            evaluation,
            str(identity["dataset"]["dataset_id"]),
            config,
        )
        indices = partitions.open_lockbox(
            selection_complete=True,
            purpose=config.lockbox_purpose,
        )
        if partitions.lockbox_commitment != marker["lockbox_commitment_sha256"]:
            raise ResearchExperimentError("reconstructed lockbox commitment changed")
        outcomes, outcomes_bytes = _prepare_lockbox_outcomes(
            lockbox_outcomes,
            evaluation,
            indices,
        )
        if pd.DatetimeIndex(outcomes["outcome_available_time"]).max().to_pydatetime() > opened:
            raise ResearchExperimentError("opened_at precedes lockbox outcome availability")
        shared_outcomes_path = claim_store / f"{manifest['experiment_id']}.outcomes.jsonl"
        _exclusive_write(shared_outcomes_path, outcomes_bytes)
        _fsync_directory(claim_store)
        result = _lockbox_result(
            evaluation,
            partitions,
            indices,
            outcomes,
            outcomes_bytes,
            config,
            manifest,
            marker,
            marker_bytes,
        )
        result_bytes = _canonical_bytes(result) + b"\n"
        result_path = directory / "lockbox.result.json"
        _exclusive_write(result_path, result_bytes)
        _exclusive_write(
            directory / "lockbox.result.sha256",
            f"{_sha256(result_bytes)}\n".encode("ascii"),
        )
        _fsync_directory(directory)
    except Exception:
        # The marker intentionally remains. A failed evaluation is a consumed
        # lockbox, not permission to peek again.
        raise

    final_audit = audit_research_experiment(directory)
    if not final_audit.passed:
        raise ResearchExperimentError(
            f"lockbox result failed self-audit: {'; '.join(final_audit.errors)}"
        )
    return LockboxEvaluation(str(manifest["experiment_id"]), result_path, result)


def audit_research_experiment(experiment_dir: str | Path) -> ResearchExperimentAudit:
    """Recompute the prepared manifest and any completed lockbox result."""

    errors: list[str] = []
    warnings = [
        "precomputed predictions do not prove trainer or test-selection isolation",
        "the PIT dataset envelope does not prove a completed feature as-of join",
        "the local lockbox marker is procedural, not a security boundary",
        "research experiment is never promotion eligible",
    ]
    manifest: dict[str, Any] = {}
    try:
        requested = Path(experiment_dir).expanduser().absolute()
        if requested.is_symlink():
            errors.append("experiment directory must not be a symbolic link")
        directory = requested.resolve()
        if not directory.is_dir():
            raise ResearchExperimentError(f"experiment directory not found: {directory}")
        entries = tuple(directory.iterdir())
    except (OSError, ResearchExperimentError) as error:
        return ResearchExperimentAudit(False, (str(error),), tuple(warnings), {})

    names = {entry.name for entry in entries}
    if ".incomplete" in names:
        errors.append("experiment has an incomplete materialization marker")
    allowed = _BASE_FILES | _LOCKBOX_FILES | {".incomplete"}
    missing = sorted(_BASE_FILES - names)
    unexpected = sorted(names - allowed)
    if missing:
        errors.append(f"missing experiment entries: {missing}")
    if unexpected:
        errors.append(f"unexpected experiment entries: {unexpected}")
    symlinks = sorted(entry.name for entry in entries if entry.is_symlink())
    if symlinks:
        errors.append(f"experiment entries must not be symbolic links: {symlinks}")
    for name in _BASE_FILES - {"trial_bundle"}:
        path = directory / name
        if path.exists() and (path.is_symlink() or not path.is_file()):
            errors.append(f"experiment entry must be a regular file: {name}")
    trial_dir = directory / "trial_bundle"
    if trial_dir.exists() and (trial_dir.is_symlink() or not trial_dir.is_dir()):
        errors.append("trial_bundle must be a directory")

    manifest_path = directory / "manifest.json"
    manifest_bytes = b""
    if manifest_path.is_file():
        try:
            manifest_bytes = manifest_path.read_bytes()
            parsed = json.loads(manifest_bytes.decode("utf-8"))
            if not isinstance(parsed, dict):
                raise ResearchExperimentError("manifest must be a JSON object")
            manifest = parsed
            if _canonical_bytes(manifest) + b"\n" != manifest_bytes:
                errors.append("manifest.json is not canonical")
        except (
            OSError,
            UnicodeDecodeError,
            ValueError,
            RecursionError,
            ResearchExperimentError,
        ) as error:
            errors.append(f"cannot parse experiment manifest: {error}")
    digest_path = directory / "manifest.sha256"
    if digest_path.is_file() and manifest_bytes:
        try:
            if digest_path.read_bytes() != f"{_sha256(manifest_bytes)}\n".encode("ascii"):
                errors.append("manifest.sha256 does not match manifest.json")
        except OSError as error:
            errors.append(f"cannot read manifest.sha256: {error}")

    identity = manifest.get("identity") if isinstance(manifest, dict) else None
    if not isinstance(identity, dict):
        errors.append("experiment identity is missing or invalid")
        return ResearchExperimentAudit(
            False, tuple(dict.fromkeys(errors)), tuple(warnings), manifest
        )

    rebuilt_manifest: dict[str, Any] | None = None
    evaluation: pd.DataFrame | None = None
    partitions: ModelPartitions | None = None
    config: ResearchExperimentConfig | None = None
    claim_store: Path | None = None
    try:
        created = _utc(identity["created_at"], "identity.created_at")
        code = _exact_mapping(identity["code"], {"commit", "dirty_worktree"}, "identity.code")
        code_commit = str(code["commit"])
        dirty_worktree = code["dirty_worktree"]
        _validate_code_provenance(code_commit, dirty_worktree)
        config = _config_from_dict(identity["config"])
        locators = _exact_mapping(manifest["locators"], {"dataset_directory"}, "manifest.locators")
        dataset_reference = _exact_mapping(
            identity["dataset"],
            {"dataset_id", "manifest_sha256"},
            "identity.dataset",
        )
        dataset_path, dataset_manifest = _audited_dataset(locators["dataset_directory"])
        if dataset_manifest["dataset_id"] != dataset_reference["dataset_id"]:
            raise ResearchExperimentError("referenced PIT dataset_id changed")
        dataset_manifest_bytes = (dataset_path / "manifest.json").read_bytes()
        if _sha256(dataset_manifest_bytes) != dataset_reference["manifest_sha256"]:
            raise ResearchExperimentError("referenced PIT manifest changed")

        evaluation_bytes = (directory / "evaluation.jsonl").read_bytes()
        evaluation = _evaluation_from_bytes(evaluation_bytes)
        normalized, partitions, expected_evaluation = _prepare_evaluation(
            evaluation,
            str(dataset_manifest["dataset_id"]),
            config,
        )
        if evaluation_bytes != expected_evaluation:
            raise ResearchExperimentError("evaluation.jsonl is not canonical")
        evaluation = normalized
        _validate_block_sizes(config, partitions)
        bundle = _load_trial_bundle(
            trial_dir,
            dataset_id=str(dataset_manifest["dataset_id"]),
            pair=config.pair,
            horizon=config.horizon,
            tune_times=_prediction_times(evaluation, partitions.tune),
            expected_trial_ids=config.expected_trial_ids,
        )
        _validate_candidate_binding(evaluation, bundle)
        _validate_input_chronology(
            created,
            dataset_manifest,
            bundle,
            evaluation,
            partitions,
        )
        stress_file_bytes = (directory / "cost_stress.json").read_bytes()
        stress_frame = _stress_from_bytes(stress_file_bytes)
        stress_rows, expected_stress = _prepare_cost_stress(
            stress_frame,
            len(partitions.test),
            dataset_id=str(dataset_manifest["dataset_id"]),
            candidate_id=bundle.selected_trial_id,
            model_artifact_sha256=_selected_model_hash(bundle),
        )
        if stress_file_bytes != expected_stress:
            raise ResearchExperimentError("cost_stress.json is not canonical")
        analysis = _prepared_analysis(
            evaluation,
            partitions,
            bundle,
            stress_rows,
            config,
            dataset_manifest,
            code_commit,
            dirty_worktree,
        )
        claim_store = _claim_store(identity["lockbox_claim_store"], create=False)
        rebuilt_identity = _experiment_identity(
            created=created,
            code_commit=code_commit,
            dirty_worktree=dirty_worktree,
            claim_store=claim_store,
            dataset_manifest=dataset_manifest,
            config=config,
            evaluation_bytes=evaluation_bytes,
            evaluation_rows=len(evaluation),
            bundle=bundle,
            stress_bytes=stress_file_bytes,
        )
        rebuilt_id = _sha256(_canonical_bytes(rebuilt_identity))
        rebuilt_manifest = _experiment_manifest(
            rebuilt_id,
            rebuilt_identity,
            analysis,
            dataset_manifest,
            dataset_path=dataset_path,
        )
        if identity != rebuilt_identity:
            errors.append("experiment identity does not match preserved inputs")
        if manifest != rebuilt_manifest:
            errors.append("experiment manifest claims do not match recomputed evidence")
        if directory.name != rebuilt_id or manifest.get("experiment_id") != rebuilt_id:
            errors.append("experiment_id or directory name does not match identity")
    except (
        KeyError,
        TypeError,
        ValueError,
        OSError,
        RecursionError,
        OverflowError,
        PointInTimeError,
        ResearchExperimentError,
    ) as error:
        errors.append(f"experiment evidence cannot be reconstructed: {error}")

    marker_path = directory / "lockbox.open.json"
    result_path = directory / "lockbox.result.json"
    result_digest_path = directory / "lockbox.result.sha256"
    local_lockbox_names = {
        path.name for path in (marker_path, result_path, result_digest_path) if path.exists()
    }
    shared_marker_path: Path | None = None
    shared_outcomes_path: Path | None = None
    if claim_store is not None and rebuilt_manifest is not None:
        shared_marker_path = claim_store / f"{rebuilt_manifest['experiment_id']}.open.json"
        shared_outcomes_path = claim_store / f"{rebuilt_manifest['experiment_id']}.outcomes.jsonl"
    shared_names = {
        path.name
        for path in (shared_marker_path, shared_outcomes_path)
        if path is not None and path.exists()
    }
    if not local_lockbox_names and not shared_names:
        pass
    elif (
        local_lockbox_names == _LOCKBOX_FILES
        and shared_marker_path is not None
        and shared_outcomes_path is not None
        and shared_marker_path.exists()
        and shared_outcomes_path.exists()
        and all(
            path.is_file() and not path.is_symlink()
            for path in (
                marker_path,
                result_path,
                result_digest_path,
                shared_marker_path,
                shared_outcomes_path,
            )
        )
    ):
        try:
            if (
                rebuilt_manifest is None
                or evaluation is None
                or partitions is None
                or config is None
            ):
                raise ResearchExperimentError("prepared evidence unavailable for lockbox audit")
            marker_bytes = marker_path.read_bytes()
            if shared_marker_path.read_bytes() != marker_bytes:
                raise ResearchExperimentError("local/shared lockbox markers differ")
            marker = json.loads(marker_bytes.decode("utf-8"))
            if not isinstance(marker, dict) or _canonical_bytes(marker) + b"\n" != marker_bytes:
                raise ResearchExperimentError("lockbox marker is not canonical")
            expected_marker_keys = {
                "schema_version",
                "state",
                "experiment_id",
                "manifest_sha256",
                "lockbox_commitment_sha256",
                "purpose",
                "claim_store",
                "actor",
                "opened_at",
            }
            _exact_mapping(marker, expected_marker_keys, "lockbox marker")
            if marker["schema_version"] != 1 or marker["state"] != "consumed_local_claim_scope":
                raise ResearchExperimentError("lockbox marker state/schema is invalid")
            _required_text(marker["actor"], "lockbox actor")
            marker_opened = _utc(marker["opened_at"], "lockbox opened_at")
            if marker_opened < _utc(
                rebuilt_manifest["identity"]["created_at"], "identity.created_at"
            ):
                raise ResearchExperimentError("lockbox marker predates experiment creation")
            if marker["experiment_id"] != rebuilt_manifest["experiment_id"]:
                raise ResearchExperimentError("lockbox marker experiment_id mismatch")
            if marker["manifest_sha256"] != _sha256(manifest_bytes):
                raise ResearchExperimentError("lockbox marker manifest digest mismatch")
            if marker["purpose"] != config.lockbox_purpose:
                raise ResearchExperimentError("lockbox marker purpose mismatch")
            if marker["claim_store"] != str(claim_store):
                raise ResearchExperimentError("lockbox marker claim store mismatch")
            indices = partitions.open_lockbox(
                selection_complete=True,
                purpose=config.lockbox_purpose,
            )
            if partitions.lockbox_commitment != marker["lockbox_commitment_sha256"]:
                raise ResearchExperimentError("lockbox marker commitment mismatch")
            outcomes_bytes = shared_outcomes_path.read_bytes()
            outcomes = _lockbox_outcomes_from_bytes(outcomes_bytes)
            outcomes, expected_outcomes = _prepare_lockbox_outcomes(
                outcomes,
                evaluation,
                indices,
            )
            if outcomes_bytes != expected_outcomes:
                raise ResearchExperimentError("shared lockbox outcomes are not canonical")
            if (
                pd.DatetimeIndex(outcomes["outcome_available_time"]).max().to_pydatetime()
                > marker_opened
            ):
                raise ResearchExperimentError("lockbox marker predates outcome availability")
            expected_result = _lockbox_result(
                evaluation,
                partitions,
                indices,
                outcomes,
                outcomes_bytes,
                config,
                rebuilt_manifest,
                marker,
                marker_bytes,
            )
            result_bytes = result_path.read_bytes()
            result = json.loads(result_bytes.decode("utf-8"))
            if result != expected_result or _canonical_bytes(result) + b"\n" != result_bytes:
                raise ResearchExperimentError("lockbox result does not match recomputation")
            if result_digest_path.read_bytes() != f"{_sha256(result_bytes)}\n".encode("ascii"):
                raise ResearchExperimentError("lockbox result digest mismatch")
        except (
            OSError,
            UnicodeDecodeError,
            TypeError,
            ValueError,
            RecursionError,
            OverflowError,
            ResearchExperimentError,
        ) as error:
            errors.append(f"lockbox evidence cannot be reconstructed: {error}")
    else:
        errors.append("lockbox claim is consumed but local/shared result evidence is incomplete")

    return ResearchExperimentAudit(
        not errors,
        tuple(dict.fromkeys(errors)),
        tuple(warnings),
        manifest,
    )


def _prepare_evaluation(
    frame: pd.DataFrame,
    dataset_id: str,
    config: ResearchExperimentConfig,
) -> tuple[pd.DataFrame, ModelPartitions, bytes]:
    if not isinstance(frame, pd.DataFrame) or frame.empty:
        raise ResearchExperimentError("evaluation must be a non-empty DataFrame")
    if set(frame.columns) != set(_EVALUATION_COLUMNS):
        missing = sorted(set(_EVALUATION_COLUMNS) - set(frame.columns))
        extra = sorted(set(frame.columns) - set(_EVALUATION_COLUMNS))
        raise ResearchExperimentError(
            f"evaluation columns mismatch: missing={missing} extra={extra}"
        )
    values = frame.loc[:, _EVALUATION_COLUMNS].copy()
    for column in ("prediction_time", "label_end_time", "max_feature_available_time"):
        values[column] = _aware_series(values[column], column)
    prediction = pd.DatetimeIndex(values["prediction_time"])
    if not prediction.is_monotonic_increasing or prediction.has_duplicates:
        raise ResearchExperimentError("prediction_time must be unique and monotonic")
    if bool((values["label_end_time"] < values["prediction_time"]).any()):
        raise ResearchExperimentError("label_end_time cannot precede prediction_time")
    if bool((values["max_feature_available_time"] > values["prediction_time"]).any()):
        raise ResearchExperimentError("future feature availability detected")

    for column in (
        "sample_id",
        "dataset_id",
        "candidate_id",
        "model_artifact_sha256",
        "pair",
        "horizon",
        "regime",
    ):
        if bool(values[column].isna().any()) or any(
            not isinstance(item, str) or not item.strip() for item in values[column]
        ):
            raise ResearchExperimentError(f"{column} must contain non-empty strings")
        values[column] = values[column].map(lambda item: item.strip())
    if bool(values["sample_id"].duplicated().any()):
        raise ResearchExperimentError("sample_id must be unique")
    if set(values["dataset_id"]) != {dataset_id}:
        raise ResearchExperimentError("evaluation dataset_id does not match PIT artifact")
    if set(values["pair"]) != {config.pair}:
        raise ResearchExperimentError("V1 requires exactly the configured single pair")
    if set(values["horizon"]) != {config.horizon}:
        raise ResearchExperimentError("V1 requires exactly the configured single horizon")
    if any(not _SHA256.fullmatch(value) for value in values["model_artifact_sha256"]):
        raise ResearchExperimentError("model_artifact_sha256 must contain full SHA-256 IDs")

    probabilities = pd.to_numeric(values["raw_probability"], errors="raise").astype(float)
    if not bool(np.isfinite(probabilities).all()) or bool(
        ((probabilities < 0.0) | (probabilities > 1.0)).any()
    ):
        raise ResearchExperimentError("raw_probability must be finite and in [0, 1]")
    values["raw_probability"] = probabilities
    partitions = _partitions(values, config.partition_config)
    withheld = set(partitions.withheld_lockbox_positions)
    original_labels = values["label"].copy()
    original_returns = values["net_r"].copy()
    labels = pd.to_numeric(values["label"], errors="coerce")
    net_r = pd.to_numeric(values["net_r"], errors="coerce")
    normalized_labels: list[int | None] = []
    normalized_returns: list[float | None] = []
    for position, (label, outcome) in enumerate(zip(labels, net_r, strict=True)):
        if position in withheld:
            if not pd.isna(original_labels.iloc[position]) or not pd.isna(
                original_returns.iloc[position]
            ):
                raise ResearchExperimentError(
                    "lockbox label/net_r outcomes must be withheld during preparation"
                )
            normalized_labels.append(None)
            normalized_returns.append(None)
            continue
        if pd.isna(label) or float(label) not in {0.0, 1.0} or float(label) != int(label):
            raise ResearchExperimentError("development label must contain integer 0/1")
        if pd.isna(outcome) or not math.isfinite(float(outcome)):
            raise ResearchExperimentError("development net_r must be finite")
        normalized_labels.append(int(label))
        normalized_returns.append(float(outcome))
    values["label"] = pd.Series(normalized_labels, dtype=object)
    values["net_r"] = pd.Series(normalized_returns, dtype=object)

    rows: list[dict[str, Any]] = []
    for row in values.to_dict(orient="records"):
        rows.append(
            {
                "sample_id": row["sample_id"],
                "dataset_id": row["dataset_id"],
                "candidate_id": row["candidate_id"],
                "model_artifact_sha256": row["model_artifact_sha256"],
                "prediction_time": pd.Timestamp(row["prediction_time"]).isoformat(),
                "label_end_time": pd.Timestamp(row["label_end_time"]).isoformat(),
                "max_feature_available_time": pd.Timestamp(
                    row["max_feature_available_time"]
                ).isoformat(),
                "pair": row["pair"],
                "horizon": row["horizon"],
                "regime": row["regime"],
                "raw_probability": float(row["raw_probability"]),
                "label": None if row["label"] is None else int(row["label"]),
                "net_r": None if row["net_r"] is None else float(row["net_r"]),
            }
        )
    content = b"".join(_canonical_bytes(row) + b"\n" for row in rows)
    return values.reset_index(drop=True), partitions, content


def _evaluation_from_bytes(content: bytes) -> pd.DataFrame:
    if not content or not content.endswith(b"\n"):
        raise ResearchExperimentError("evaluation.jsonl must be non-empty and newline terminated")
    rows: list[dict[str, Any]] = []
    for line_number, line in enumerate(content.splitlines(), start=1):
        if not line:
            raise ResearchExperimentError(f"blank evaluation row at line {line_number}")
        try:
            row = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, ValueError, RecursionError) as error:
            raise ResearchExperimentError(
                f"invalid evaluation JSON at line {line_number}: {error}"
            ) from error
        if not isinstance(row, dict) or set(row) != set(_EVALUATION_COLUMNS):
            raise ResearchExperimentError(f"evaluation row schema mismatch at line {line_number}")
        if _canonical_bytes(row) != line:
            raise ResearchExperimentError(f"evaluation row is not canonical at line {line_number}")
        rows.append(row)
    return pd.DataFrame(rows, columns=_EVALUATION_COLUMNS)


def _prepare_lockbox_outcomes(
    frame: pd.DataFrame,
    evaluation: pd.DataFrame,
    indices: Sequence[int],
) -> tuple[pd.DataFrame, bytes]:
    if not isinstance(frame, pd.DataFrame) or frame.empty:
        raise ResearchExperimentError("lockbox_outcomes must be a non-empty DataFrame")
    if set(frame.columns) != set(_LOCKBOX_OUTCOME_COLUMNS):
        raise ResearchExperimentError("lockbox_outcomes columns do not match the outcome contract")
    values = frame.loc[:, _LOCKBOX_OUTCOME_COLUMNS].copy()
    expected = evaluation.iloc[list(indices)]
    expected_ids = expected["sample_id"].tolist()
    if values["sample_id"].tolist() != expected_ids:
        raise ResearchExperimentError("lockbox outcome sample IDs/order do not match commitment")
    available = _aware_series(values["outcome_available_time"], "outcome_available_time")
    label_ends = pd.DatetimeIndex(expected["label_end_time"])
    if bool((pd.DatetimeIndex(available) < label_ends).any()):
        raise ResearchExperimentError("lockbox outcome became available before label completion")
    labels = pd.to_numeric(values["label"], errors="raise")
    if not bool(labels.isin([0, 1]).all()) or any(float(item) != int(item) for item in labels):
        raise ResearchExperimentError("lockbox label must contain integer 0/1")
    returns = pd.to_numeric(values["net_r"], errors="raise").astype(float)
    if not bool(np.isfinite(returns).all()):
        raise ResearchExperimentError("lockbox net_r must be finite")
    values["outcome_available_time"] = available
    values["label"] = labels.astype(int)
    values["net_r"] = returns
    rows = [
        {
            "sample_id": row["sample_id"],
            "outcome_available_time": pd.Timestamp(row["outcome_available_time"]).isoformat(),
            "label": int(row["label"]),
            "net_r": float(row["net_r"]),
        }
        for row in values.to_dict(orient="records")
    ]
    content = b"".join(_canonical_bytes(row) + b"\n" for row in rows)
    return values.reset_index(drop=True), content


def _lockbox_outcomes_from_bytes(content: bytes) -> pd.DataFrame:
    if not content or not content.endswith(b"\n"):
        raise ResearchExperimentError("shared lockbox outcomes must be newline terminated")
    rows: list[dict[str, Any]] = []
    for line_number, line in enumerate(content.splitlines(), start=1):
        try:
            row = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, ValueError, RecursionError) as error:
            raise ResearchExperimentError(
                f"invalid shared lockbox outcome at line {line_number}: {error}"
            ) from error
        if not isinstance(row, dict) or set(row) != set(_LOCKBOX_OUTCOME_COLUMNS):
            raise ResearchExperimentError("shared lockbox outcome schema mismatch")
        if _canonical_bytes(row) != line:
            raise ResearchExperimentError("shared lockbox outcome is not canonical")
        rows.append(row)
    return pd.DataFrame(rows, columns=_LOCKBOX_OUTCOME_COLUMNS)


def _prepare_cost_stress(
    frame: pd.DataFrame,
    expected_observed_trades: int,
    *,
    dataset_id: str,
    candidate_id: str,
    model_artifact_sha256: str,
) -> tuple[list[dict[str, Any]], bytes]:
    if not isinstance(frame, pd.DataFrame) or frame.empty:
        raise ResearchExperimentError("cost_stress must be a non-empty DataFrame")
    missing = sorted(set(_STRESS_COLUMNS) - set(frame.columns))
    if missing:
        raise ResearchExperimentError(f"cost_stress missing columns: {missing}")
    rows: list[dict[str, Any]] = []
    seen: set[float] = set()
    for raw in frame.loc[:, _STRESS_COLUMNS].to_dict(orient="records"):
        multiplier = _finite_float(raw["cost_multiplier"], "cost_multiplier")
        if multiplier in seen or multiplier not in _STRESS_SCENARIOS:
            raise ResearchExperimentError(
                "cost stress multipliers must be unique default scenarios"
            )
        seen.add(multiplier)
        scenario = _required_text(raw["scenario"], "stress.scenario")
        if scenario != _STRESS_SCENARIOS[multiplier]:
            raise ResearchExperimentError("cost stress scenario name/multiplier mismatch")
        if raw["method"] != "full_engine_rerun":
            raise ResearchExperimentError("cost stress must use full_engine_rerun")
        expected_binding = {
            "dataset_id": dataset_id,
            "candidate_id": candidate_id,
            "model_artifact_sha256": model_artifact_sha256,
        }
        if any(raw[key] != value for key, value in expected_binding.items()):
            raise ResearchExperimentError(
                "cost stress candidate/dataset/evaluation binding mismatch"
            )
        trade_count = raw["trade_count"]
        if (
            not isinstance(trade_count, (int, np.integer))
            or isinstance(trade_count, (bool, np.bool_))
            or int(trade_count) < 0
        ):
            raise ResearchExperimentError("stress trade_count must be a non-negative integer")
        execution = _json_value(raw["execution_config"], "execution_config")
        if not isinstance(execution, dict) or not execution:
            raise ResearchExperimentError("execution_config must be a non-empty JSON object")
        _require_nonnegative_json_numbers(execution, "execution_config")
        rows.append(
            {
                "scenario": scenario,
                "cost_multiplier": multiplier,
                "method": "full_engine_rerun",
                **expected_binding,
                "trade_count": int(trade_count),
                "expectancy_r": _finite_float(raw["expectancy_r"], "expectancy_r"),
                "max_drawdown": _finite_float(raw["max_drawdown"], "max_drawdown"),
                "execution_config": execution,
            }
        )
    if seen != set(_STRESS_SCENARIOS):
        raise ResearchExperimentError("observed, 1.5x, 2x, and 3x cost reruns are required")
    rows.sort(key=lambda row: row["cost_multiplier"])
    if rows[0]["trade_count"] != expected_observed_trades:
        raise ResearchExperimentError(
            "observed cost-stress trade_count must equal completed test outcomes"
        )
    if any(row["max_drawdown"] < 0 for row in rows):
        raise ResearchExperimentError("stress max_drawdown must be non-negative")
    content = _canonical_bytes(rows) + b"\n"
    return rows, content


def _stress_from_bytes(content: bytes) -> pd.DataFrame:
    if not content or not content.endswith(b"\n"):
        raise ResearchExperimentError("cost_stress.json must be newline terminated")
    try:
        value = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError) as error:
        raise ResearchExperimentError(f"cannot parse cost_stress.json: {error}") from error
    if not isinstance(value, list) or not value:
        raise ResearchExperimentError("cost_stress.json must contain a non-empty list")
    if _canonical_bytes(value) + b"\n" != content:
        raise ResearchExperimentError("cost_stress.json is not canonical")
    return pd.DataFrame(value)


def _load_trial_bundle(
    directory: Path,
    *,
    dataset_id: str,
    pair: str,
    horizon: str,
    tune_times: pd.DatetimeIndex,
    expected_trial_ids: Sequence[str],
) -> _TrialBundle:
    directory = directory.expanduser().resolve()
    if directory.is_symlink() or not directory.is_dir():
        raise ResearchExperimentError(f"trial bundle directory is invalid: {directory}")
    expected = {RUN_FILENAME, TRIALS_FILENAME, RETURNS_MATRIX_FILENAME}
    entries = tuple(directory.iterdir())
    names = {entry.name for entry in entries}
    if names != expected or any(entry.is_symlink() or not entry.is_file() for entry in entries):
        raise ResearchExperimentError(
            f"trial bundle must contain exactly regular files: {sorted(expected)}"
        )
    raw_files = {name: (directory / name).read_bytes() for name in sorted(expected)}
    try:
        run = json.loads(raw_files[RUN_FILENAME].decode("utf-8"))
        trials = read_trials(directory / TRIALS_FILENAME)
        matrix = read_returns_matrix(directory / RETURNS_MATRIX_FILENAME)
    except (OSError, UnicodeDecodeError, ValueError, RecursionError) as error:
        raise ResearchExperimentError(f"cannot parse trial bundle: {error}") from error
    if not isinstance(run, dict):
        raise ResearchExperimentError("trial run.json must be a JSON object")
    trial_count = run.get("trial_count")
    if not isinstance(trial_count, int) or isinstance(trial_count, bool):
        raise ResearchExperimentError("run trial_count must be an integer")
    if trial_count != len(trials) or trial_count < 2:
        raise ResearchExperimentError("trial ledger count mismatch or fewer than two trials")
    trial_ids: list[str] = []
    selected: list[str] = []
    for position, trial in enumerate(trials):
        if not isinstance(trial, dict):
            raise ResearchExperimentError(f"trial {position} is not an object")
        trial_id = _required_text(trial.get("trial_id"), f"trial[{position}].trial_id")
        if trial.get("phase") != "tune":
            raise ResearchExperimentError("every disclosed trial must have phase=tune")
        trial_ids.append(trial_id)
        if trial.get("selected") is True:
            selected.append(trial_id)
    if len(trial_ids) != len(set(trial_ids)):
        raise ResearchExperimentError("trial IDs must be unique")
    if trial_ids != list(expected_trial_ids):
        raise ResearchExperimentError("trial ledger does not match expected_trial_ids")
    if len(selected) != 1 or run.get("selected_trial_id") != selected[0]:
        raise ResearchExperimentError("exactly one selected trial must match run.json")
    context = run.get("context")
    if not isinstance(context, dict):
        raise ResearchExperimentError("trial run context is missing")
    required_context = {
        "declared_complete_trial_family": True,
        "selection_partition": "tune",
        "dataset_id": dataset_id,
        "pair": pair,
        "horizon": horizon,
        "expected_trial_ids": list(expected_trial_ids),
    }
    if any(context.get(key) != value for key, value in required_context.items()):
        raise ResearchExperimentError("trial run context does not match the declared tune family")
    model_hash = context.get("selected_model_artifact_sha256")
    if not isinstance(model_hash, str) or not _SHA256.fullmatch(model_hash):
        raise ResearchExperimentError(
            "selected model artifact SHA-256 is missing from trial context"
        )

    index = pd.DatetimeIndex(matrix.index)
    if (
        index.tz is None
        or index.hasnans
        or index.has_duplicates
        or not index.is_monotonic_increasing
    ):
        raise ResearchExperimentError(
            "trial returns timestamps must be aware, unique, and monotonic"
        )
    index = index.tz_convert("UTC")
    matrix.index = index
    if not index.equals(tune_times):
        raise ResearchExperimentError("trial returns matrix must exactly match tune timestamps")
    if list(matrix.columns) != trial_ids:
        raise ResearchExperimentError("trial returns columns must exactly match ledger order")
    if not bool(np.isfinite(matrix.to_numpy(dtype=float)).all()):
        raise ResearchExperimentError("trial returns matrix contains missing or non-finite values")
    fingerprints = tuple(
        {
            "path": f"trial_bundle/{name}",
            "sha256": _sha256(content),
            "bytes": len(content),
        }
        for name, content in sorted(raw_files.items())
    )
    return _TrialBundle(
        run=run,
        trials=tuple(trials),
        matrix=matrix,
        selected_trial_id=selected[0],
        raw_files=raw_files,
        fingerprints=fingerprints,
    )


def _prepared_analysis(
    evaluation: pd.DataFrame,
    partitions: ModelPartitions,
    bundle: _TrialBundle,
    stress_rows: Sequence[Mapping[str, Any]],
    config: ResearchExperimentConfig,
    dataset_manifest: Mapping[str, Any],
    code_commit: str,
    dirty_worktree: bool,
) -> dict[str, Any]:
    calibrator = _fit_registered_calibrator(evaluation, partitions, config)
    test = evaluation.iloc[list(partitions.test)]
    raw = test["raw_probability"].to_numpy(dtype=float)
    labels = test["label"].to_numpy(dtype=int)
    calibrated = calibrator.predict(raw)
    raw_metrics = calibration_metrics(labels.tolist(), raw.tolist()).to_dict()
    calibrated_metrics = calibration_metrics(labels.tolist(), calibrated.tolist()).to_dict()
    returns = test["net_r"].to_numpy(dtype=float)
    confidence = circular_block_bootstrap_mean_ci(
        returns,
        block_size=config.bootstrap_block_size,
        resamples=config.bootstrap_resamples,
        seed=config.seed,
    ).to_dict()
    permutation = block_sign_permutation_test(
        returns,
        block_size=config.bootstrap_block_size,
        permutations=config.permutations,
        seed=config.seed,
    )
    psr = _safe_statistic(lambda: probabilistic_sharpe_ratio(returns))
    mtrl = _safe_statistic(lambda: minimum_track_record_length(returns))

    trial_sharpes = [per_period_sharpe(bundle.matrix[column]) for column in bundle.matrix.columns]
    dsr = _safe_statistic(
        lambda: deflated_sharpe_ratio(
            bundle.matrix[bundle.selected_trial_id],
            trial_sharpes,
        )
    )
    pbo = _safe_statistic(
        lambda: probability_of_backtest_overfitting(
            bundle.matrix,
            n_blocks=config.pbo_blocks,
        )
    )
    evidence = PromotionEvidence(
        dataset_hash=str(dataset_manifest["dataset_id"]),
        git_commit=code_commit,
        dirty_worktree=dirty_worktree,
        synthetic_data=dataset_manifest["identity"]["dataset_class"] == "synthetic",
        # The PIT artifact validates envelopes only. It deliberately does not
        # claim that every model feature completed an audited as-of join.
        point_in_time_violations=None,
        future_feature_violations=None,
        # The trial space, predictions and engine reruns are declarations supplied
        # after the fact, not independently timestamped call-graph evidence.
        trial_count=None,
        sample_count=None,
        net_expectancy_r=None,
        expectancy_ci_lower_r=None,
        dsr_probability=None,
        pbo_probability=None,
        max_drawdown_pct=None,
        brier_improvement=None,
        cost_stress_2x_expectancy_r=None,
        regime_count=None,
        pair_count=1,
        lockbox_evaluated_once=None,
        lockbox_reused_for_selection=None,
        major_operational_incidents=None,
        data_quality_incidents=None,
        calibration_window_separate=True,
        # Predictions and stress results enter this API precomputed, so their
        # selection-time isolation cannot be proven by this binder.
        test_window_separate=None,
        live_like_execution_validated=False,
    )
    report = evaluate_promotion(
        evidence,
        target_stage="validated",
        policy=config.promotion_policy,
    )
    return {
        "partitions": _partition_manifest(evaluation, partitions),
        "selection": {
            "partition": "tune",
            "selected_trial_id": bundle.selected_trial_id,
            "selected_model_artifact_sha256": _selected_model_hash(bundle),
            "trial_count": len(bundle.trials),
            "declared_complete_trial_family": True,
            "independent_preregistration_attested": False,
        },
        "calibrator": {
            "method": config.calibrator_method,
            "fit_partition": "calibration",
            "state": _json_value(calibrator.to_dict(), "calibrator state"),
        },
        "test": {
            "state": "descriptive_recomputation_in_this_artifact",
            "rows": len(test),
            "raw_calibration": raw_metrics,
            "calibrated_calibration": calibrated_metrics,
            "net_expectancy_r": float(np.mean(returns)),
            "expectancy_confidence_interval": confidence,
            "block_sign_permutation": permutation,
            "probabilistic_sharpe": psr,
            "minimum_track_record_length": mtrl,
        },
        "overfitting": {
            "scope": "aligned declared tune trial family; independent completeness unattested",
            "trial_sharpes": trial_sharpes,
            "deflated_sharpe": dsr,
            "pbo": pbo,
        },
        "cost_stress": {
            "scope": "precomputed rows declaring full-engine reruns; engine artifacts unattested",
            "scenarios": [dict(row) for row in stress_rows],
        },
        "promotion": {
            "evidence": asdict(evidence),
            "report": report.to_dict(),
        },
    }


def _lockbox_result(
    evaluation: pd.DataFrame,
    partitions: ModelPartitions,
    indices: Sequence[int],
    outcomes: pd.DataFrame,
    outcomes_bytes: bytes,
    config: ResearchExperimentConfig,
    manifest: Mapping[str, Any],
    marker: Mapping[str, Any],
    marker_bytes: bytes,
) -> dict[str, Any]:
    calibrator = _fit_registered_calibrator(evaluation, partitions, config)
    expected_state = manifest["analysis"]["calibrator"]["state"]
    actual_state = _json_value(calibrator.to_dict(), "calibrator state")
    if actual_state != expected_state:
        raise ResearchExperimentError("frozen calibrator state changed before lockbox")
    lockbox = evaluation.iloc[list(indices)].copy()
    if lockbox["sample_id"].tolist() != outcomes["sample_id"].tolist():
        raise ResearchExperimentError("lockbox outcomes changed order or sample identity")
    lockbox["label"] = outcomes["label"].to_numpy(dtype=int)
    lockbox["net_r"] = outcomes["net_r"].to_numpy(dtype=float)
    raw = lockbox["raw_probability"].to_numpy(dtype=float)
    labels = lockbox["label"].to_numpy(dtype=int)
    calibrated = calibrator.predict(raw)
    raw_metrics = calibration_metrics(labels.tolist(), raw.tolist()).to_dict()
    calibrated_metrics = calibration_metrics(labels.tolist(), calibrated.tolist()).to_dict()
    returns = lockbox["net_r"].to_numpy(dtype=float)
    confidence = circular_block_bootstrap_mean_ci(
        returns,
        block_size=config.bootstrap_block_size,
        resamples=config.bootstrap_resamples,
        seed=config.seed,
    ).to_dict()
    permutation = block_sign_permutation_test(
        returns,
        block_size=config.bootstrap_block_size,
        permutations=config.permutations,
        seed=config.seed,
    )
    base = PromotionEvidence(**manifest["analysis"]["promotion"]["evidence"])
    evidence = replace(
        base,
        sample_count=None,
        net_expectancy_r=None,
        expectancy_ci_lower_r=None,
        max_drawdown_pct=None,
        brier_improvement=None,
        cost_stress_2x_expectancy_r=None,
        regime_count=None,
        lockbox_evaluated_once=None,
        # A local precomputed bundle cannot prove that an external researcher
        # never saw these outcomes while selecting the candidate.
        lockbox_reused_for_selection=None,
        test_window_separate=None,
    )
    report = evaluate_promotion(
        evidence,
        target_stage="validated",
        policy=config.promotion_policy,
    )
    return {
        "schema_version": 1,
        "artifact_kind": "research_lockbox_result",
        "experiment_id": manifest["experiment_id"],
        "state": "completed_under_configured_local_claim_store",
        "marker_sha256": _sha256(marker_bytes),
        "lockbox_outcomes_sha256": _sha256(outcomes_bytes),
        "manifest_sha256": marker["manifest_sha256"],
        "actor": marker["actor"],
        "opened_at": marker["opened_at"],
        "purpose": marker["purpose"],
        "lockbox_commitment_sha256": partitions.lockbox_commitment,
        "rows": len(lockbox),
        "raw_calibration": raw_metrics,
        "calibrated_calibration": calibrated_metrics,
        "net_expectancy_r": float(np.mean(returns)),
        "expectancy_confidence_interval": confidence,
        "block_sign_permutation": permutation,
        "probabilistic_sharpe": _safe_statistic(lambda: probabilistic_sharpe_ratio(returns)),
        "minimum_track_record_length": _safe_statistic(
            lambda: minimum_track_record_length(returns)
        ),
        "promotion": {
            "evidence": asdict(evidence),
            "report": report.to_dict(),
            "promotion_eligible": False,
            "blocker": (
                "local claim completion cannot prove global one-time evaluation or non-reuse "
                "for selection"
            ),
        },
    }


def _fit_registered_calibrator(
    evaluation: pd.DataFrame,
    partitions: ModelPartitions,
    config: ResearchExperimentConfig,
) -> ProbabilityCalibrator:
    calibration = evaluation.iloc[list(partitions.calibration)]
    return fit_calibrator(
        config.calibrator_method,
        calibration["raw_probability"].to_numpy(dtype=float),
        calibration["label"].to_numpy(dtype=int),
    )


def _partition_manifest(
    evaluation: pd.DataFrame,
    partitions: ModelPartitions,
) -> dict[str, Any]:
    output: dict[str, Any] = {}
    for name in ("train", "tune", "calibration", "test"):
        positions = tuple(getattr(partitions, name))
        timestamps = _prediction_times(evaluation, positions)
        output[name] = {
            "rows": len(positions),
            "positions": list(positions),
            "prediction_time_start": timestamps.min().isoformat(),
            "prediction_time_end": timestamps.max().isoformat(),
        }
    output["lockbox"] = {
        "state": "outcomes_withheld",
        "rows": partitions.lockbox_size,
        "commitment_sha256": partitions.lockbox_commitment,
        "commitment_scope": "row positions only; outcome hash is created after local claim",
        "purpose": "outcomes supplied only after configured shared local claim",
    }
    return output


def _safe_statistic(compute: Any) -> dict[str, Any]:
    try:
        value = compute()
        if isinstance(value, Mapping):
            return {"status": "available", "value": _json_value(value, "statistic")}
        return {"status": "available", "value": _json_value(value, "statistic")}
    except (TypeError, ValueError, FloatingPointError) as error:
        return {"status": "unavailable", "reason": str(error)}


def _available_float(report: Mapping[str, Any], key: str) -> float | None:
    if report.get("status") != "available":
        return None
    value = report.get("value")
    if not isinstance(value, Mapping):
        return None
    item = value.get(key)
    if isinstance(item, (int, float)) and not isinstance(item, bool) and math.isfinite(float(item)):
        return float(item)
    return None


def _experiment_identity(
    *,
    created: datetime,
    code_commit: str,
    dirty_worktree: bool,
    claim_store: Path,
    dataset_manifest: Mapping[str, Any],
    config: ResearchExperimentConfig,
    evaluation_bytes: bytes,
    evaluation_rows: int,
    bundle: _TrialBundle,
    stress_bytes: bytes,
) -> dict[str, Any]:
    dataset_manifest_bytes = _canonical_bytes(dataset_manifest) + b"\n"
    return {
        "schema_version": 1,
        "artifact_kind": "precomputed_research_experiment",
        "created_at": created.isoformat(),
        "lockbox_claim_store": str(claim_store),
        "code": {"commit": code_commit, "dirty_worktree": dirty_worktree},
        "dataset": {
            "dataset_id": dataset_manifest["dataset_id"],
            "manifest_sha256": _sha256(dataset_manifest_bytes),
        },
        "config": _config_to_dict(config),
        "inputs": {
            "evaluation": {
                "path": "evaluation.jsonl",
                "sha256": _sha256(evaluation_bytes),
                "bytes": len(evaluation_bytes),
                "rows": evaluation_rows,
            },
            "trial_bundle": [dict(item) for item in bundle.fingerprints],
            "cost_stress": {
                "path": "cost_stress.json",
                "sha256": _sha256(stress_bytes),
                "bytes": len(stress_bytes),
            },
        },
    }


def _experiment_manifest(
    experiment_id: str,
    identity: Mapping[str, Any],
    analysis: Mapping[str, Any],
    dataset_manifest: Mapping[str, Any],
    *,
    dataset_path: Path,
) -> dict[str, Any]:
    dataset_blockers = list(dataset_manifest.get("promotion_blockers", []))
    blockers = list(
        dict.fromkeys(
            [
                "precomputed_predictions_unverified",
                "trial_space_preregistration_unattested",
                "engine_artifacts_unattested",
                "performance_evidence_unavailable",
                "test_selection_isolation_unproven",
                "global_lockbox_once_and_non_reuse_unproven",
                "single_pair_v1",
                "local_lockbox_not_security_boundary",
                *dataset_blockers,
            ]
        )
    )
    return {
        "schema_version": 1,
        "artifact_kind": "research_experiment_manifest",
        "experiment_id": experiment_id,
        "state": "descriptive_test_recomputed_lockbox_outcomes_withheld",
        "identity": dict(identity),
        "locators": {"dataset_directory": str(dataset_path)},
        "analysis": dict(analysis),
        "evidence_scope": {
            "status": "research_only",
            "predictions": "precomputed; trainer call graph not attested",
            "point_in_time": "dataset envelopes audited; feature as-of join not attested",
            "test": "metrics recomputed here; precomputation isolation not attested",
            "lockbox": (
                "outcomes absent from artifact; configured shared local claim store is "
                "procedural only"
            ),
        },
        "promotion_eligible": False,
        "promotion_blockers": blockers,
        "registry_mutation": "not_performed",
        "live_path": "absent",
    }


def _config_to_dict(config: ResearchExperimentConfig) -> dict[str, Any]:
    partition = config.partition_config
    return {
        "hypothesis": config.hypothesis,
        "pair": config.pair,
        "horizon": config.horizon,
        "lockbox_purpose": config.lockbox_purpose,
        "expected_trial_ids": list(config.expected_trial_ids),
        "calibrator_method": config.calibrator_method,
        "partition": {
            "train_fraction": partition.train_fraction,
            "tune_fraction": partition.tune_fraction,
            "calibration_fraction": partition.calibration_fraction,
            "test_fraction": partition.test_fraction,
            "lockbox_fraction": partition.lockbox_fraction,
            "purge_seconds": partition.purge.total_seconds(),
            "embargo_seconds": partition.embargo.total_seconds(),
            "min_rows_per_partition": partition.min_rows_per_partition,
        },
        "bootstrap_block_size": config.bootstrap_block_size,
        "bootstrap_resamples": config.bootstrap_resamples,
        "permutations": config.permutations,
        "pbo_blocks": config.pbo_blocks,
        "seed": config.seed,
        "promotion_policy": asdict(config.promotion_policy),
    }


def _config_from_dict(value: Any) -> ResearchExperimentConfig:
    expected = {
        "hypothesis",
        "pair",
        "horizon",
        "lockbox_purpose",
        "expected_trial_ids",
        "calibrator_method",
        "partition",
        "bootstrap_block_size",
        "bootstrap_resamples",
        "permutations",
        "pbo_blocks",
        "seed",
        "promotion_policy",
    }
    mapping = _exact_mapping(value, expected, "identity.config")
    partition = _exact_mapping(
        mapping["partition"],
        {
            "train_fraction",
            "tune_fraction",
            "calibration_fraction",
            "test_fraction",
            "lockbox_fraction",
            "purge_seconds",
            "embargo_seconds",
            "min_rows_per_partition",
        },
        "identity.config.partition",
    )
    policy = mapping["promotion_policy"]
    if not isinstance(policy, dict):
        raise ResearchExperimentError("promotion_policy must be an object")
    try:
        return ResearchExperimentConfig(
            hypothesis=mapping["hypothesis"],
            pair=mapping["pair"],
            horizon=mapping["horizon"],
            lockbox_purpose=mapping["lockbox_purpose"],
            expected_trial_ids=tuple(mapping["expected_trial_ids"]),
            calibrator_method=mapping["calibrator_method"],
            partition_config=ModelPartitionConfig(
                train_fraction=float(partition["train_fraction"]),
                tune_fraction=float(partition["tune_fraction"]),
                calibration_fraction=float(partition["calibration_fraction"]),
                test_fraction=float(partition["test_fraction"]),
                lockbox_fraction=float(partition["lockbox_fraction"]),
                purge=timedelta(seconds=float(partition["purge_seconds"])),
                embargo=timedelta(seconds=float(partition["embargo_seconds"])),
                min_rows_per_partition=int(partition["min_rows_per_partition"]),
            ),
            bootstrap_block_size=int(mapping["bootstrap_block_size"]),
            bootstrap_resamples=int(mapping["bootstrap_resamples"]),
            permutations=int(mapping["permutations"]),
            pbo_blocks=int(mapping["pbo_blocks"]),
            seed=int(mapping["seed"]),
            promotion_policy=PromotionPolicy(**policy),
        )
    except (TypeError, ValueError, OverflowError) as error:
        raise ResearchExperimentError(f"invalid experiment config: {error}") from error


def _audited_dataset(path: str | Path) -> tuple[Path, dict[str, Any]]:
    directory = Path(path).expanduser().resolve()
    audit = audit_pit_dataset(directory)
    if not audit.passed:
        raise ResearchExperimentError(f"PIT dataset audit failed: {'; '.join(audit.errors)}")
    manifest = dict(audit.manifest)
    dataset_id = manifest.get("dataset_id")
    if not isinstance(dataset_id, str) or not _SHA256.fullmatch(dataset_id):
        raise ResearchExperimentError("PIT dataset_id is invalid")
    return directory, manifest


def _selected_model_hash(bundle: _TrialBundle) -> str:
    context = bundle.run.get("context")
    if not isinstance(context, Mapping):
        raise ResearchExperimentError("trial context is unavailable")
    value = context.get("selected_model_artifact_sha256")
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise ResearchExperimentError("selected model artifact SHA-256 is invalid")
    return value


def _validate_candidate_binding(evaluation: pd.DataFrame, bundle: _TrialBundle) -> None:
    if set(evaluation["candidate_id"]) != {bundle.selected_trial_id}:
        raise ResearchExperimentError("evaluation is not bound to the selected trial")
    if set(evaluation["model_artifact_sha256"]) != {_selected_model_hash(bundle)}:
        raise ResearchExperimentError("evaluation model artifact does not match trial selection")


def _validate_input_chronology(
    created: datetime,
    dataset_manifest: Mapping[str, Any],
    bundle: _TrialBundle,
    evaluation: pd.DataFrame,
    partitions: ModelPartitions,
) -> None:
    dataset_created = _utc(dataset_manifest["identity"]["created_at"], "dataset.created_at")
    started = _utc(bundle.run.get("started_at"), "trial.started_at")
    written = _utc(bundle.run.get("written_at"), "trial.written_at")
    if dataset_created > started:
        raise ResearchExperimentError("trial started before the bound PIT dataset existed")
    if started > written:
        raise ResearchExperimentError("trial written_at precedes started_at")
    if max(dataset_created, written) > created:
        raise ResearchExperimentError("experiment creation predates dataset or trial evidence")
    test_label_end = pd.DatetimeIndex(
        evaluation.iloc[list(partitions.test)]["label_end_time"]
    ).max()
    if test_label_end.to_pydatetime() > created:
        raise ResearchExperimentError("experiment creation predates test label completion")
    if pd.DatetimeIndex(evaluation["prediction_time"]).max().to_pydatetime() > created:
        raise ResearchExperimentError("evaluation contains a prediction created in the future")


def _claim_store(path: Any, *, create: bool) -> Path:
    if not isinstance(path, (str, Path)):
        raise ResearchExperimentError("lockbox_claim_store must be a path")
    requested = Path(path).expanduser().absolute()
    if requested.is_symlink():
        raise ResearchExperimentError("lockbox_claim_store must not be a symbolic link")
    if create:
        requested.mkdir(parents=True, exist_ok=True)
    directory = requested.resolve()
    if not directory.is_dir():
        raise ResearchExperimentError(f"lockbox_claim_store is not a directory: {directory}")
    return directory


def _require_nonnegative_json_numbers(value: Any, name: str) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            _require_nonnegative_json_numbers(item, f"{name}.{key}")
        return
    if isinstance(value, (list, tuple)):
        for position, item in enumerate(value):
            _require_nonnegative_json_numbers(item, f"{name}[{position}]")
        return
    if isinstance(value, (int, float)) and not isinstance(value, bool) and float(value) < 0:
        raise ResearchExperimentError(f"{name} contains a negative cost value")


def _partitions(frame: pd.DataFrame, config: ModelPartitionConfig) -> ModelPartitions:
    return chronological_model_partitions(
        pd.DatetimeIndex(frame["prediction_time"]),
        pd.DatetimeIndex(frame["label_end_time"]),
        config,
    )


def _prediction_times(frame: pd.DataFrame, positions: Sequence[int]) -> pd.DatetimeIndex:
    return pd.DatetimeIndex(frame.iloc[list(positions)]["prediction_time"]).tz_convert("UTC")


def _validate_block_sizes(config: ResearchExperimentConfig, partitions: ModelPartitions) -> None:
    minimum = min(len(partitions.test), partitions.lockbox_size)
    if config.bootstrap_block_size > minimum:
        raise ResearchExperimentError(
            "bootstrap_block_size exceeds test or withheld lockbox row count"
        )


def _existing_experiment_or_raise(
    artifact: ResearchExperimentArtifact,
) -> ResearchExperimentArtifact:
    audit = audit_research_experiment(artifact.directory)
    if audit.passed and audit.manifest.get("experiment_id") == artifact.experiment_id:
        return artifact
    raise ResearchExperimentError(
        "existing experiment is not a valid identical artifact: "
        + ("; ".join(audit.errors) or "identity collision")
    )


def _validate_code_provenance(code_commit: Any, dirty_worktree: Any) -> None:
    if not isinstance(code_commit, str) or not _GIT_COMMIT.fullmatch(code_commit):
        raise ResearchExperimentError("code_commit must be a full lowercase Git object ID")
    if not isinstance(dirty_worktree, bool):
        raise ResearchExperimentError("dirty_worktree must be a bool")


def _aware_series(series: pd.Series, name: str) -> pd.Series:
    try:
        parsed = pd.to_datetime(series, errors="raise", utc=False, format="mixed")
    except (TypeError, ValueError) as error:
        raise ResearchExperimentError(f"{name} cannot be parsed") from error
    if not isinstance(parsed.dtype, pd.DatetimeTZDtype):
        raise ResearchExperimentError(f"{name} must be timezone-aware")
    if bool(parsed.isna().any()):
        raise ResearchExperimentError(f"{name} cannot contain null timestamps")
    return parsed.dt.tz_convert("UTC")


def _creation_time(value: object, name: str) -> datetime:
    timestamp = _utc(value, name)
    if timestamp > datetime.now(UTC):
        raise ResearchExperimentError(f"{name} must not be in the future")
    return timestamp


def _utc(value: object, name: str) -> datetime:
    try:
        return utc_datetime(value, field_name=name)
    except PointInTimeError as error:
        raise ResearchExperimentError(str(error)) from error


def _required_text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ResearchExperimentError(f"{name} must be a non-empty string")
    return value.strip()


def _finite_float(value: Any, name: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as error:
        raise ResearchExperimentError(f"{name} must be numeric") from error
    if not math.isfinite(number):
        raise ResearchExperimentError(f"{name} must be finite")
    return number


def _json_value(value: Any, name: str) -> Any:
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise ResearchExperimentError(f"{name} object keys must be strings")
        return {key: _json_value(item, name) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item, name) for item in value]
    if isinstance(value, set):
        raise ResearchExperimentError(f"{name} cannot contain sets")
    if isinstance(value, np.bool_):
        return bool(value)
    if isinstance(value, np.integer):
        return int(value)
    if isinstance(value, np.floating):
        value = float(value)
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ResearchExperimentError(f"{name} contains a non-finite float")
        return value
    raise ResearchExperimentError(f"{name} contains unsupported type {type(value).__name__}")


def _exact_mapping(value: Any, keys: set[str], name: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        actual = sorted(value) if isinstance(value, dict) else type(value).__name__
        raise ResearchExperimentError(
            f"{name} keys mismatch: expected={sorted(keys)} actual={actual}"
        )
    return value


def _canonical_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, RecursionError) as error:
        raise ResearchExperimentError(f"value is not canonical JSON: {error}") from error


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _exclusive_write(path: Path, content: bytes) -> None:
    try:
        with path.open("xb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
    except OSError as error:
        raise ResearchExperimentError(f"create-only write failed for {path}: {error}") from error


def _fsync_directory(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError as error:
        raise ResearchExperimentError(f"cannot fsync directory {path}: {error}") from error


__all__ = [
    "LockboxEvaluation",
    "ResearchExperimentArtifact",
    "ResearchExperimentAudit",
    "ResearchExperimentConfig",
    "ResearchExperimentError",
    "audit_research_experiment",
    "evaluate_lockbox_once",
    "prepare_research_experiment",
]
