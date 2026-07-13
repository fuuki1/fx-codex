"""Fail-closed model promotion, immutable audit events, and hard risk vetoes."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
import hashlib
import json
import math
import os
from pathlib import Path
from typing import cast, Literal, TypeGuard

import pandas as pd

from .overfitting import (
    deflated_sharpe_ratio,
    per_period_sharpe,
    probability_of_backtest_overfitting,
)
from .statistical_validation import circular_block_bootstrap_mean_ci

ModelStage = Literal["research", "validated", "shadow", "paper", "limited_live", "live"]
STAGES: tuple[ModelStage, ...] = (
    "research",
    "validated",
    "shadow",
    "paper",
    "limited_live",
    "live",
)
EXECUTION_STAGES: frozenset[ModelStage] = frozenset({"limited_live", "live"})
CORE_PROMOTION_GATES: frozenset[str] = frozenset(
    {
        "model_id",
        "artifact_hash",
        "verified_provenance",
        "dataset_hash",
        "feature_version",
        "label_version",
        "selected_trial_id",
        "git_commit",
        "clean_worktree",
        "non_synthetic_data",
        "point_in_time_integrity",
        "future_feature_integrity",
        "all_trials_recorded",
        "sample_size",
        "net_expectancy",
        "expectancy_confidence_interval",
        "deflated_sharpe",
        "probability_of_backtest_overfitting",
        "drawdown",
        "calibration_improvement",
        "calibration_split",
        "test_split",
        "untouched_lockbox",
        "lockbox_sample_size",
        "lockbox_net_expectancy",
        "lockbox_drawdown",
        "lockbox_calibration_improvement",
        "lockbox_cost_stress_2x",
        "lockbox_point_in_time_integrity",
        "lockbox_future_feature_integrity",
        "durable_lockbox_consumption_seal",
        "authoritative_external_seal",
        "cost_stress_2x",
        "regime_coverage",
        "pair_coverage",
        "operational_incidents",
        "data_quality_incidents",
    }
)


class GovernanceError(RuntimeError):
    """Raised when registry state or a requested transition is unsafe."""


@dataclass(frozen=True)
class PromotionPolicy:
    """Configurable candidate thresholds, not universal statistical truths."""

    min_net_expectancy_r: float = 0.0
    min_expectancy_ci_lower_r: float = 0.0
    min_dsr_probability: float = 0.95
    max_pbo_probability: float = 0.20
    min_samples: int = 200
    min_regimes: int = 3
    min_pairs: int = 3
    max_drawdown_pct: float = 0.15
    min_brier_improvement: float = 0.0
    min_cost_stress_2x_expectancy_r: float = 0.0
    min_shadow_days_for_paper: int = 30
    min_paper_days_for_limited_live: int = 60
    allow_limited_live: bool = False
    allow_live: bool = False
    rationale: str = (
        "Initial conservative research gates; re-estimate from sample size, mandate, "
        "execution venue, and loss tolerance before use."
    )

    def __post_init__(self) -> None:
        finite_thresholds = {
            "min_net_expectancy_r": self.min_net_expectancy_r,
            "min_expectancy_ci_lower_r": self.min_expectancy_ci_lower_r,
            "min_brier_improvement": self.min_brier_improvement,
            "min_cost_stress_2x_expectancy_r": self.min_cost_stress_2x_expectancy_r,
        }
        for name, value in finite_thresholds.items():
            if not _is_finite_number(value):
                raise ValueError(f"{name} must be finite")
        for name, value in {
            "min_dsr_probability": self.min_dsr_probability,
            "max_pbo_probability": self.max_pbo_probability,
            "max_drawdown_pct": self.max_drawdown_pct,
        }.items():
            if not _is_finite_number(value) or not 0.0 <= float(value) <= 1.0:
                raise ValueError(f"{name} must be finite and in [0, 1]")
        if not -1.0 <= self.min_brier_improvement <= 1.0:
            raise ValueError("min_brier_improvement must be in [-1, 1]")
        integer_thresholds = {
            "min_samples": (self.min_samples, 1),
            "min_regimes": (self.min_regimes, 1),
            "min_pairs": (self.min_pairs, 1),
            "min_shadow_days_for_paper": (self.min_shadow_days_for_paper, 0),
            "min_paper_days_for_limited_live": (
                self.min_paper_days_for_limited_live,
                0,
            ),
        }
        for name, (value, minimum) in integer_thresholds.items():
            if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
                raise ValueError(f"{name} must be an integer >= {minimum}")
        if not isinstance(self.allow_limited_live, bool) or not isinstance(self.allow_live, bool):
            raise ValueError("live policy switches must be boolean")
        if not isinstance(self.rationale, str) or not self.rationale.strip():
            raise ValueError("policy rationale is required")


@dataclass(frozen=True)
class PromotionEvidence:
    dataset_hash: str | None = None
    feature_version: str | None = None
    label_version: str | None = None
    selected_trial_id: str | None = None
    git_commit: str | None = None
    dirty_worktree: bool | None = None
    synthetic_data: bool | None = None
    point_in_time_violations: int | None = None
    future_feature_violations: int | None = None
    trial_count: int | None = None
    sample_count: int | None = None
    net_expectancy_r: float | None = None
    expectancy_ci_lower_r: float | None = None
    dsr_probability: float | None = None
    pbo_probability: float | None = None
    max_drawdown_pct: float | None = None
    brier_improvement: float | None = None
    cost_stress_2x_expectancy_r: float | None = None
    regime_count: int | None = None
    pair_count: int | None = None
    lockbox_evaluated_once: bool | None = None
    lockbox_reused_for_selection: bool | None = None
    shadow_days: int | None = None
    paper_days: int | None = None
    major_operational_incidents: int | None = None
    data_quality_incidents: int | None = None
    calibration_window_separate: bool | None = None
    test_window_separate: bool | None = None
    live_like_execution_validated: bool | None = None
    model_id: str | None = None
    artifact_hash: str | None = None
    artifact_path: str | None = None
    evidence_manifest_path: str | None = None
    evidence_manifest_hash: str | None = None
    trial_ledger_path: str | None = None
    trial_ledger_hash: str | None = None
    evaluation_observations_path: str | None = None
    evaluation_observations_hash: str | None = None
    lockbox_observations_path: str | None = None
    lockbox_observations_hash: str | None = None


@dataclass(frozen=True)
class PromotionGate:
    name: str
    passed: bool
    observed: object
    requirement: str
    critical: bool = True

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class PromotionReport:
    target_stage: ModelStage
    gates: tuple[PromotionGate, ...]
    policy_rationale: str
    model_id: str | None = None
    artifact_hash: str | None = None
    evidence_manifest_path: str | None = None
    evidence_manifest_hash: str | None = None
    trial_ledger_path: str | None = None
    trial_ledger_hash: str | None = None
    evaluation_observations_path: str | None = None
    evaluation_observations_hash: str | None = None
    lockbox_observations_path: str | None = None
    lockbox_observations_hash: str | None = None
    evaluated_at: str = ""
    policy: dict[str, object] | None = None
    evidence: dict[str, object] | None = None
    report_id: str = ""

    @property
    def passed(self) -> bool:
        critical_gates = tuple(gate for gate in self.gates if gate.critical)
        return bool(critical_gates) and all(gate.passed for gate in critical_gates)

    @property
    def evidence_gates_passed(self) -> bool:
        external = {"authoritative_external_seal", "durable_lockbox_consumption_seal"}
        critical_gates = tuple(
            gate for gate in self.gates if gate.critical and gate.name not in external
        )
        return bool(critical_gates) and all(gate.passed for gate in critical_gates)

    @property
    def failures(self) -> tuple[str, ...]:
        return tuple(gate.name for gate in self.gates if gate.critical and not gate.passed)

    def to_dict(self) -> dict[str, object]:
        return {
            "report_id": self.report_id,
            "model_id": self.model_id,
            "artifact_hash": self.artifact_hash,
            "evidence_manifest_path": self.evidence_manifest_path,
            "evidence_manifest_hash": self.evidence_manifest_hash,
            "trial_ledger_path": self.trial_ledger_path,
            "trial_ledger_hash": self.trial_ledger_hash,
            "evaluation_observations_path": self.evaluation_observations_path,
            "evaluation_observations_hash": self.evaluation_observations_hash,
            "lockbox_observations_path": self.lockbox_observations_path,
            "lockbox_observations_hash": self.lockbox_observations_hash,
            "evaluated_at": self.evaluated_at,
            "policy": self.policy,
            "evidence": self.evidence,
            "target_stage": self.target_stage,
            "passed": self.passed,
            "evidence_gates_passed": self.evidence_gates_passed,
            "failures": list(self.failures),
            "policy_rationale": self.policy_rationale,
            "gates": [gate.to_dict() for gate in self.gates],
        }


def evaluate_promotion(
    evidence: PromotionEvidence,
    *,
    target_stage: ModelStage = "validated",
    policy: PromotionPolicy | None = None,
    as_of: datetime | None = None,
) -> PromotionReport:
    """Evaluate every required signal; missing evidence is a failed gate."""

    if target_stage not in STAGES:
        raise ValueError(f"unknown model stage: {target_stage}")
    evaluation_time = as_of or datetime.now(UTC)
    if evaluation_time.tzinfo is None:
        raise ValueError("promotion as_of must be timezone-aware")
    evaluation_time = evaluation_time.astimezone(UTC)
    settings = policy or PromotionPolicy()
    gates: list[PromotionGate] = []
    provenance_ok, provenance_detail = _verify_promotion_provenance(
        evidence,
        as_of=evaluation_time,
    )
    lockbox_statistics = (
        _verified_lockbox_gate_statistics(evidence, as_of=evaluation_time) if provenance_ok else {}
    )

    def require(name: str, observed: object, passed: bool, requirement: str) -> None:
        gates.append(PromotionGate(name, bool(passed), observed, requirement))

    require(
        "model_id",
        evidence.model_id,
        isinstance(evidence.model_id, str) and bool(evidence.model_id.strip()),
        "promotion evidence is bound to a model_id",
    )
    require(
        "artifact_hash",
        evidence.artifact_hash,
        _is_sha256(evidence.artifact_hash),
        "promotion evidence is bound to an immutable artifact SHA-256",
    )
    require(
        "verified_provenance",
        provenance_detail,
        provenance_ok,
        "artifact, evidence manifest, complete trial ledger, and independent windows verify",
    )
    require(
        "dataset_hash",
        evidence.dataset_hash,
        _is_sha256(evidence.dataset_hash),
        "immutable dataset SHA-256 is recorded",
    )
    require(
        "feature_version",
        evidence.feature_version,
        isinstance(evidence.feature_version, str) and bool(evidence.feature_version.strip()),
        "feature registry version is recorded",
    )
    require(
        "label_version",
        evidence.label_version,
        isinstance(evidence.label_version, str) and bool(evidence.label_version.strip()),
        "label definition version is recorded",
    )
    require(
        "selected_trial_id",
        evidence.selected_trial_id,
        isinstance(evidence.selected_trial_id, str) and bool(evidence.selected_trial_id.strip()),
        "selected trial is bound to the complete trial ledger",
    )
    require(
        "git_commit",
        evidence.git_commit,
        isinstance(evidence.git_commit, str) and len(evidence.git_commit.strip()) >= 7,
        "source commit is recorded",
    )
    require(
        "clean_worktree",
        evidence.dirty_worktree,
        evidence.dirty_worktree is False,
        "promotion artifact was built from a clean worktree",
    )
    require(
        "non_synthetic_data",
        evidence.synthetic_data,
        evidence.synthetic_data is False,
        "synthetic data cannot support promotion",
    )
    require(
        "point_in_time_integrity",
        evidence.point_in_time_violations,
        _is_integer_equal(evidence.point_in_time_violations, 0),
        "zero point-in-time violations",
    )
    require(
        "future_feature_integrity",
        evidence.future_feature_violations,
        _is_integer_equal(evidence.future_feature_violations, 0),
        "zero future-feature violations",
    )
    require(
        "all_trials_recorded",
        evidence.trial_count,
        _integer_at_least(evidence.trial_count, 1),
        "at least one trial and the complete trial ledger are recorded",
    )
    require(
        "sample_size",
        evidence.sample_count,
        _integer_at_least(evidence.sample_count, settings.min_samples),
        f"sample_count >= {settings.min_samples}",
    )
    require(
        "net_expectancy",
        evidence.net_expectancy_r,
        _greater_than(evidence.net_expectancy_r, settings.min_net_expectancy_r),
        f"net expectancy R > {settings.min_net_expectancy_r:.3f}",
    )
    require(
        "expectancy_confidence_interval",
        evidence.expectancy_ci_lower_r,
        _greater_than(evidence.expectancy_ci_lower_r, settings.min_expectancy_ci_lower_r),
        f"95% lower confidence bound > {settings.min_expectancy_ci_lower_r:.3f}R",
    )
    require(
        "deflated_sharpe",
        evidence.dsr_probability,
        _probability_at_least(evidence.dsr_probability, settings.min_dsr_probability),
        f"DSR probability >= {settings.min_dsr_probability:.2f}",
    )
    require(
        "probability_of_backtest_overfitting",
        evidence.pbo_probability,
        _probability_at_most(evidence.pbo_probability, settings.max_pbo_probability),
        f"PBO <= {settings.max_pbo_probability:.2f}",
    )
    require(
        "drawdown",
        evidence.max_drawdown_pct,
        _bounded_at_most(evidence.max_drawdown_pct, settings.max_drawdown_pct, 0.0, 1.0),
        f"max drawdown <= {settings.max_drawdown_pct:.1%}",
    )
    require(
        "calibration_improvement",
        evidence.brier_improvement,
        _bounded_greater_than(
            evidence.brier_improvement,
            settings.min_brier_improvement,
            -1.0,
            1.0,
        ),
        f"Brier improvement > {settings.min_brier_improvement:.4f}",
    )
    require(
        "calibration_split",
        evidence.calibration_window_separate,
        evidence.calibration_window_separate is True,
        "calibration window is separate from tune/test/lockbox",
    )
    require(
        "test_split",
        evidence.test_window_separate,
        evidence.test_window_separate is True,
        "test window is not used for model or threshold selection",
    )
    require(
        "untouched_lockbox",
        {
            "evaluated_once": evidence.lockbox_evaluated_once,
            "reused": evidence.lockbox_reused_for_selection,
        },
        evidence.lockbox_evaluated_once is True and evidence.lockbox_reused_for_selection is False,
        "lockbox is evaluated once after selection and never fed back",
    )
    require(
        "lockbox_sample_size",
        lockbox_statistics.get("sample_count"),
        _integer_at_least(lockbox_statistics.get("sample_count"), settings.min_samples),
        f"lockbox sample_count >= {settings.min_samples}",
    )
    require(
        "lockbox_net_expectancy",
        lockbox_statistics.get("net_expectancy_r"),
        _greater_than(
            cast(float | None, lockbox_statistics.get("net_expectancy_r")),
            settings.min_net_expectancy_r,
        ),
        f"lockbox net expectancy R > {settings.min_net_expectancy_r:.3f}",
    )
    require(
        "lockbox_drawdown",
        lockbox_statistics.get("max_drawdown_pct"),
        _bounded_at_most(
            cast(float | None, lockbox_statistics.get("max_drawdown_pct")),
            settings.max_drawdown_pct,
            0.0,
            1.0,
        ),
        f"lockbox max drawdown <= {settings.max_drawdown_pct:.1%}",
    )
    require(
        "lockbox_calibration_improvement",
        lockbox_statistics.get("brier_improvement"),
        _bounded_greater_than(
            cast(float | None, lockbox_statistics.get("brier_improvement")),
            settings.min_brier_improvement,
            -1.0,
            1.0,
        ),
        f"lockbox Brier improvement > {settings.min_brier_improvement:.4f}",
    )
    require(
        "lockbox_cost_stress_2x",
        lockbox_statistics.get("cost_stress_2x_expectancy_r"),
        _at_least(
            cast(float | None, lockbox_statistics.get("cost_stress_2x_expectancy_r")),
            settings.min_cost_stress_2x_expectancy_r,
        ),
        f"lockbox 2x cost expectancy >= {settings.min_cost_stress_2x_expectancy_r:.3f}R",
    )
    require(
        "lockbox_point_in_time_integrity",
        lockbox_statistics.get("point_in_time_violations"),
        _is_integer_equal(lockbox_statistics.get("point_in_time_violations"), 0),
        "zero lockbox point-in-time violations",
    )
    require(
        "lockbox_future_feature_integrity",
        lockbox_statistics.get("future_feature_violations"),
        _is_integer_equal(lockbox_statistics.get("future_feature_violations"), 0),
        "zero lockbox future-feature violations",
    )
    require(
        "cost_stress_2x",
        evidence.cost_stress_2x_expectancy_r,
        _at_least(
            evidence.cost_stress_2x_expectancy_r,
            settings.min_cost_stress_2x_expectancy_r,
        ),
        f"2x cost expectancy >= {settings.min_cost_stress_2x_expectancy_r:.3f}R",
    )
    require(
        "regime_coverage",
        evidence.regime_count,
        _integer_at_least(evidence.regime_count, settings.min_regimes),
        f"at least {settings.min_regimes} regimes",
    )
    require(
        "pair_coverage",
        evidence.pair_count,
        _integer_at_least(evidence.pair_count, settings.min_pairs),
        f"at least {settings.min_pairs} currency pairs",
    )
    require(
        "operational_incidents",
        evidence.major_operational_incidents,
        _is_integer_equal(evidence.major_operational_incidents, 0),
        "zero unresolved major operational incidents",
    )
    require(
        "data_quality_incidents",
        evidence.data_quality_incidents,
        _is_integer_equal(evidence.data_quality_incidents, 0),
        "zero unresolved data-quality incidents",
    )

    if target_stage in {"paper", "limited_live", "live"}:
        require(
            "shadow_duration",
            evidence.shadow_days,
            _integer_at_least(evidence.shadow_days, settings.min_shadow_days_for_paper),
            f"shadow duration >= {settings.min_shadow_days_for_paper} days",
        )
        require(
            "live_like_execution",
            evidence.live_like_execution_validated,
            evidence.live_like_execution_validated is True,
            "live-like execution and reconciliation are validated",
        )
    if target_stage in {"limited_live", "live"}:
        require(
            "paper_duration",
            evidence.paper_days,
            _integer_at_least(evidence.paper_days, settings.min_paper_days_for_limited_live),
            f"paper duration >= {settings.min_paper_days_for_limited_live} days",
        )
        require(
            "limited_live_disabled",
            settings.allow_limited_live,
            settings.allow_limited_live,
            "limited live requires a separately approved policy",
        )
    if target_stage == "live":
        require(
            "live_disabled",
            settings.allow_live,
            settings.allow_live,
            "live promotion requires an explicit separately approved policy",
        )
    require(
        "durable_lockbox_consumption_seal",
        None,
        False,
        "durable external one-time lockbox consumption seal is required",
    )
    require(
        "authoritative_external_seal",
        None,
        False,
        "authoritative external human approval seal is required",
    )
    draft = PromotionReport(
        target_stage,
        tuple(gates),
        settings.rationale,
        model_id=evidence.model_id,
        artifact_hash=evidence.artifact_hash,
        evidence_manifest_path=evidence.evidence_manifest_path,
        evidence_manifest_hash=evidence.evidence_manifest_hash,
        trial_ledger_path=evidence.trial_ledger_path,
        trial_ledger_hash=evidence.trial_ledger_hash,
        evaluation_observations_path=evidence.evaluation_observations_path,
        evaluation_observations_hash=evidence.evaluation_observations_hash,
        lockbox_observations_path=evidence.lockbox_observations_path,
        lockbox_observations_hash=evidence.lockbox_observations_hash,
        evaluated_at=evaluation_time.isoformat(),
        policy=cast(dict[str, object], asdict(settings)),
        evidence=cast(dict[str, object], asdict(evidence)),
    )
    return PromotionReport(
        target_stage,
        tuple(gates),
        settings.rationale,
        model_id=evidence.model_id,
        artifact_hash=evidence.artifact_hash,
        evidence_manifest_path=evidence.evidence_manifest_path,
        evidence_manifest_hash=evidence.evidence_manifest_hash,
        trial_ledger_path=evidence.trial_ledger_path,
        trial_ledger_hash=evidence.trial_ledger_hash,
        evaluation_observations_path=evidence.evaluation_observations_path,
        evaluation_observations_hash=evidence.evaluation_observations_hash,
        lockbox_observations_path=evidence.lockbox_observations_path,
        lockbox_observations_hash=evidence.lockbox_observations_hash,
        evaluated_at=evaluation_time.isoformat(),
        policy=cast(dict[str, object], asdict(settings)),
        evidence=cast(dict[str, object], asdict(evidence)),
        report_id=_promotion_report_id(draft),
    )


@dataclass
class ModelRecord:
    model_id: str
    artifact_hash: str
    trained_at: str
    data_cutoff: str
    metrics: dict[str, object]
    calibration_metrics: dict[str, object]
    limitations: list[str]
    artifact_path: str = ""
    stage: ModelStage = "research"
    approved_by: str = ""
    promotion_reason: str = ""
    demotion_reason: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.model_id, str) or not self.model_id.strip():
            raise ValueError("model_id is required")
        if not _is_sha256(self.artifact_hash):
            raise ValueError("artifact_hash must be a SHA-256")
        if not isinstance(self.artifact_path, str) or not self.artifact_path.strip():
            raise ValueError("artifact_path is required")
        if self.stage not in STAGES:
            raise ValueError("invalid model stage")
        trained_at = _parse_aware_time(self.trained_at)
        data_cutoff = _parse_aware_time(self.data_cutoff)
        if trained_at is None or data_cutoff is None or data_cutoff > trained_at:
            raise ValueError("model timestamps must be aware and data_cutoff <= trained_at")
        if not isinstance(self.metrics, dict) or not isinstance(self.calibration_metrics, dict):
            raise ValueError("model metrics must be dictionaries")
        _validate_finite_metric_tree(self.metrics, "metrics")
        _validate_finite_metric_tree(self.calibration_metrics, "calibration_metrics")
        if not isinstance(self.limitations, list) or not all(
            isinstance(value, str) and value.strip() for value in self.limitations
        ):
            raise ValueError("model limitations must be a list of non-empty strings")


class ModelRegistry:
    """Small auditable registry; transitions require adjacent stages and evidence."""

    schema_version = 1

    def __init__(self) -> None:
        self.models: dict[str, ModelRecord] = {}
        self.events: list[dict[str, object]] = []

    def register(self, record: ModelRecord, *, now: datetime | None = None) -> None:
        if record.model_id in self.models:
            raise GovernanceError(f"model already registered: {record.model_id}")
        if record.stage != "research":
            raise GovernanceError("new models must enter at research stage")
        if not _file_matches_hash(record.artifact_path, record.artifact_hash):
            raise GovernanceError("model artifact is missing or does not match artifact_hash")
        _validate_model_record_as_of(record, now or datetime.now(UTC))
        self.models[record.model_id] = record
        self._event(record.model_id, "registered", "", "research", now=now)

    def promote(
        self,
        model_id: str,
        report: PromotionReport,
        *,
        approved_by: str,
        reason: str,
        now: datetime | None = None,
    ) -> None:
        record = self._record(model_id)
        try:
            expected_report_id = _promotion_report_id(report)
        except (TypeError, ValueError) as error:
            raise GovernanceError("promotion report fingerprint is invalid") from error
        if not _is_sha256(report.report_id) or report.report_id != expected_report_id:
            raise GovernanceError("promotion report fingerprint is invalid")
        if report.target_stage in {"limited_live", "live"}:
            raise GovernanceError("this registry build cannot enable live trading")
        if not report.passed and not report.evidence_gates_passed:
            raise GovernanceError(f"promotion evidence failed: {', '.join(report.failures)}")
        if report.model_id != model_id or report.artifact_hash != record.artifact_hash:
            raise GovernanceError("promotion report is not bound to this model and artifact")
        if not _file_matches_hash(
            report.evidence_manifest_path or "", report.evidence_manifest_hash
        ):
            raise GovernanceError("promotion evidence manifest changed after evaluation")
        if not _file_matches_hash(report.trial_ledger_path or "", report.trial_ledger_hash):
            raise GovernanceError("promotion trial ledger changed after evaluation")
        if not _manifest_embedded_references_match(report.evidence_manifest_path or ""):
            raise GovernanceError("promotion evidence references changed after evaluation")
        if not _file_matches_hash(record.artifact_path, record.artifact_hash):
            raise GovernanceError("registered model artifact changed after registration")
        if self._report_consumed(report.report_id):
            raise GovernanceError("promotion report has already been consumed")
        current_index = STAGES.index(record.stage)
        if current_index + 1 >= len(STAGES) or STAGES[current_index + 1] != report.target_stage:
            raise GovernanceError("promotion must move exactly one stage")
        if not approved_by.strip() or not reason.strip():
            raise GovernanceError("human approver and promotion reason are required")
        verified = _promotion_report_from_mapping(
            report.to_dict(),
            model_id=model_id,
            artifact_hash=record.artifact_hash,
            target_stage=report.target_stage,
        )
        if verified.to_dict() != report.to_dict():
            raise GovernanceError("promotion report changed during semantic re-evaluation")
        raise GovernanceError(
            "authoritative external promotion seal is not implemented; registry is research-only"
        )

    def demote(
        self,
        model_id: str,
        target_stage: ModelStage,
        *,
        reason: str,
        actor: str,
        now: datetime | None = None,
    ) -> None:
        record = self._record(model_id)
        if STAGES.index(target_stage) >= STAGES.index(record.stage):
            raise GovernanceError("demotion target must be below the current stage")
        if not reason.strip() or not actor.strip():
            raise GovernanceError("demotion actor and reason are required")
        previous = record.stage
        record.stage = target_stage
        record.demotion_reason = reason.strip()
        self._event(
            model_id,
            "demoted",
            previous,
            target_stage,
            actor=actor.strip(),
            reason=reason.strip(),
            now=now,
        )

    def save(self, path: str | Path) -> None:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": self.schema_version,
            "models": {model_id: asdict(record) for model_id, record in self.models.items()},
            "events": self.events,
        }
        temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
        temporary.write_text(
            json.dumps(
                payload,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
                allow_nan=False,
            ),
            encoding="utf-8",
        )
        os.replace(temporary, target)

    @classmethod
    def load(cls, path: str | Path, *, as_of: datetime | None = None) -> ModelRegistry:
        evaluation_time = as_of or datetime.now(UTC)
        if evaluation_time.tzinfo is None:
            raise GovernanceError("model registry as_of must be timezone-aware")
        evaluation_time = evaluation_time.astimezone(UTC)
        try:
            payload = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise GovernanceError("model registry could not be read") from error
        if not isinstance(payload, Mapping):
            raise GovernanceError("model registry must be an object")
        if payload.get("schema_version") != cls.schema_version:
            raise GovernanceError("model registry schema mismatch")
        registry = cls()
        raw_models = payload.get("models")
        if not isinstance(raw_models, Mapping):
            raise GovernanceError("model registry models must be an object")
        loaded_models: dict[str, ModelRecord] = {}
        for model_id, raw in raw_models.items():
            if not isinstance(raw, Mapping):
                raise GovernanceError("model registry records must be objects")
            try:
                record = ModelRecord(**dict(raw))
            except (TypeError, ValueError) as error:
                raise GovernanceError(f"invalid model registry record: {model_id}") from error
            if record.model_id != str(model_id):
                raise GovernanceError("model registry key does not match record model_id")
            if record.stage in EXECUTION_STAGES:
                raise GovernanceError("research registry cannot load limited_live/live state")
            if not _file_matches_hash(record.artifact_path, record.artifact_hash):
                raise GovernanceError("registered model artifact is missing or changed")
            _validate_model_record_as_of(record, evaluation_time)
            loaded_models[record.model_id] = record
        registry.models = loaded_models
        raw_events = payload.get("events")
        if not isinstance(raw_events, list) or not all(
            isinstance(event, dict) for event in raw_events
        ):
            raise GovernanceError("model registry events must be a list of objects")
        for event in raw_events:
            from_stage = event.get("from_stage")
            to_stage = event.get("to_stage")
            if (
                not isinstance(from_stage, str)
                or from_stage not in ("", *STAGES)
                or not isinstance(to_stage, str)
                or to_stage not in STAGES
            ):
                raise GovernanceError("model registry event contains an invalid stage")
            report = event.get("report")
            if not isinstance(report, Mapping):
                raise GovernanceError("model registry event report must be an object")
            report_target = report.get("target_stage")
            if (
                from_stage in EXECUTION_STAGES
                or to_stage in EXECUTION_STAGES
                or isinstance(report_target, str)
                and report_target in EXECUTION_STAGES
            ):
                raise GovernanceError("research registry cannot load limited_live/live events")
            if event.get("event_type") == "promoted":
                model_id = event.get("model_id")
                if not isinstance(model_id, str) or model_id not in loaded_models:
                    raise GovernanceError("promoted event references an unknown model")
                _promotion_report_from_mapping(
                    report,
                    model_id=model_id,
                    artifact_hash=loaded_models[model_id].artifact_hash,
                    target_stage=str(to_stage),
                )
                raise GovernanceError(
                    "authoritative external promotion seal is unavailable; "
                    "registry cannot load promoted state"
                )
        _replay_registry_history(loaded_models, raw_events, as_of=evaluation_time)
        registry.events = [dict(event) for event in raw_events]
        return registry

    def _record(self, model_id: str) -> ModelRecord:
        try:
            return self.models[model_id]
        except KeyError as error:
            raise GovernanceError(f"unknown model: {model_id}") from error

    def _report_consumed(self, report_id: str) -> bool:
        for event in self.events:
            report = event.get("report")
            if isinstance(report, Mapping) and report.get("report_id") == report_id:
                return True
        return False

    def _event(
        self,
        model_id: str,
        event_type: str,
        from_stage: str,
        to_stage: str,
        *,
        actor: str = "system",
        reason: str = "",
        report: Mapping[str, object] | None = None,
        now: datetime | None = None,
    ) -> None:
        timestamp = now or datetime.now(UTC)
        if timestamp.tzinfo is None:
            raise ValueError("registry event timestamp must be timezone-aware")
        self.events.append(
            {
                "timestamp": timestamp.astimezone(UTC).isoformat(),
                "model_id": model_id,
                "event_type": event_type,
                "from_stage": from_stage,
                "to_stage": to_stage,
                "actor": actor,
                "reason": reason,
                "report": dict(report or {}),
            }
        )


def _replay_registry_history(
    models: Mapping[str, ModelRecord],
    events: list[dict[str, object]],
    *,
    as_of: datetime,
) -> None:
    """Rebuild every stage from append-only transitions and compare final state."""

    stages: dict[str, ModelStage] = {}
    previous_time: datetime | None = None
    for event in events:
        timestamp = _parse_aware_time(event.get("timestamp"))
        if (
            timestamp is None
            or timestamp > as_of
            or previous_time is not None
            and timestamp < previous_time
        ):
            raise GovernanceError("model registry event timestamps must be aware and ordered")
        previous_time = timestamp
        model_id = event.get("model_id")
        if not isinstance(model_id, str) or model_id not in models:
            raise GovernanceError("model registry event references an unknown model")
        event_type = event.get("event_type")
        from_stage = event.get("from_stage")
        to_stage = event.get("to_stage")
        if event_type == "registered":
            if model_id in stages or from_stage != "" or to_stage != "research":
                raise GovernanceError("model registry registration history is invalid")
            stages[model_id] = "research"
            continue
        current = stages.get(model_id)
        if current is None or from_stage != current or not isinstance(to_stage, str):
            raise GovernanceError("model registry transition does not follow replayed state")
        if event_type == "promoted":
            current_index = STAGES.index(current)
            if current_index + 1 >= len(STAGES) or STAGES[current_index + 1] != to_stage:
                raise GovernanceError("model registry promotion history is not adjacent")
            report = event.get("report")
            if not isinstance(report, Mapping) or report.get("target_stage") != to_stage:
                raise GovernanceError("model registry promotion report target mismatch")
            if not str(event.get("actor", "")).strip() or not str(event.get("reason", "")).strip():
                raise GovernanceError("model registry promotion lacks human audit fields")
        elif event_type == "demoted":
            if to_stage not in STAGES or STAGES.index(to_stage) >= STAGES.index(current):
                raise GovernanceError("model registry demotion history is invalid")
            if not str(event.get("actor", "")).strip() or not str(event.get("reason", "")).strip():
                raise GovernanceError("model registry demotion lacks audit fields")
        else:
            raise GovernanceError("model registry event_type is invalid")
        stages[model_id] = to_stage
    if set(stages) != set(models):
        raise GovernanceError("model registry is missing registration history")
    for model_id, record in models.items():
        if stages[model_id] != record.stage:
            raise GovernanceError("model registry record stage does not match replayed history")


def _promotion_report_from_mapping(
    value: Mapping[str, object],
    *,
    model_id: str,
    artifact_hash: str,
    target_stage: str,
) -> PromotionReport:
    if target_stage not in STAGES or target_stage in EXECUTION_STAGES:
        raise GovernanceError("promoted event report target is invalid")
    if value.get("model_id") != model_id or value.get("artifact_hash") != artifact_hash:
        raise GovernanceError("promoted event report is not bound to its model artifact")
    raw_policy = value.get("policy")
    raw_evidence = value.get("evidence")
    evaluated_at = _parse_aware_time(value.get("evaluated_at"))
    if not isinstance(raw_policy, Mapping) or not isinstance(raw_evidence, Mapping):
        raise GovernanceError("promoted event report must embed policy and evidence")
    if evaluated_at is None:
        raise GovernanceError("promoted event report evaluated_at is invalid")
    try:
        policy = PromotionPolicy(**dict(raw_policy))
        evidence = PromotionEvidence(**dict(raw_evidence))
    except (TypeError, ValueError) as error:
        raise GovernanceError("promoted event report policy/evidence is invalid") from error
    if evidence.model_id != model_id or evidence.artifact_hash != artifact_hash:
        raise GovernanceError("promoted event embedded evidence is not bound to the model")
    recomputed = evaluate_promotion(
        evidence,
        target_stage=target_stage,
        policy=policy,
        as_of=evaluated_at,
    )
    if not recomputed.evidence_gates_passed or recomputed.to_dict() != dict(value):
        raise GovernanceError("promoted event report does not match semantic re-evaluation")
    return recomputed


@dataclass(frozen=True)
class HardVetoDecision:
    requested_action: str
    final_action: str
    reasons: tuple[str, ...]

    @property
    def vetoed(self) -> bool:
        return bool(self.reasons)


def apply_hard_veto(
    requested_action: str,
    *,
    data_quality_reasons: Iterable[str] = (),
    risk_reasons: Iterable[str] = (),
    operational_reasons: Iterable[str] = (),
) -> HardVetoDecision:
    """A veto cannot be outvoted by model confidence or committee majority."""

    reasons = tuple(
        dict.fromkeys(
            str(reason)
            for reason in (*data_quality_reasons, *risk_reasons, *operational_reasons)
            if str(reason)
        )
    )
    return HardVetoDecision(requested_action, "no_trade" if reasons else requested_action, reasons)


def artifact_sha256(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _verify_promotion_provenance(
    evidence: PromotionEvidence,
    *,
    as_of: datetime,
) -> tuple[bool, str]:
    """Verify immutable evidence and recompute selection statistics from returns."""

    try:
        artifact_path = _required_regular_file(evidence.artifact_path, "artifact")
        manifest_path = _required_regular_file(evidence.evidence_manifest_path, "manifest")
        ledger_path = _required_regular_file(evidence.trial_ledger_path, "trial ledger")
        observations_path = _required_regular_file(
            evidence.evaluation_observations_path,
            "evaluation observations",
        )
        lockbox_path = _required_regular_file(
            evidence.lockbox_observations_path,
            "lockbox observations",
        )
        if artifact_sha256(artifact_path) != evidence.artifact_hash:
            return False, "artifact SHA-256 mismatch"
        if artifact_sha256(manifest_path) != evidence.evidence_manifest_hash:
            return False, "evidence manifest SHA-256 mismatch"
        if artifact_sha256(ledger_path) != evidence.trial_ledger_hash:
            return False, "trial ledger SHA-256 mismatch"
        if artifact_sha256(observations_path) != evidence.evaluation_observations_hash:
            return False, "evaluation observations SHA-256 mismatch"
        if artifact_sha256(lockbox_path) != evidence.lockbox_observations_hash:
            return False, "lockbox observations SHA-256 mismatch"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if not isinstance(manifest, Mapping) or manifest.get("schema_version") != 1:
            return False, "evidence manifest schema mismatch"
        if manifest.get("promotion_evidence") != _manifest_evidence_payload(evidence):
            return False, "manifest promotion_evidence does not match evaluated evidence"
        artifact = manifest.get("artifact")
        if not _manifest_file_reference_matches(artifact, artifact_path, evidence.artifact_hash):
            return False, "manifest artifact reference mismatch"
        ledger = manifest.get("trial_ledger")
        if not _manifest_file_reference_matches(ledger, ledger_path, evidence.trial_ledger_hash):
            return False, "manifest trial-ledger reference mismatch"
        observations = manifest.get("evaluation_observations")
        if not _manifest_file_reference_matches(
            observations,
            observations_path,
            evidence.evaluation_observations_hash,
        ):
            return False, "manifest evaluation-observations reference mismatch"
        lockbox_reference = manifest.get("lockbox_observations")
        if not _manifest_file_reference_matches(
            lockbox_reference,
            lockbox_path,
            evidence.lockbox_observations_hash,
        ):
            return False, "manifest lockbox-observations reference mismatch"
        model_metadata = manifest.get("model_metadata")
        if not _model_metadata_matches(
            model_metadata,
            evidence,
            windows=manifest.get("windows"),
            as_of=as_of,
        ):
            return False, "structured model metadata does not match evaluated evidence"
        trial_ids, latest_trial_completion = _verified_trial_ids(
            ledger_path,
            evidence,
            as_of=as_of,
        )
        if len(trial_ids) != evidence.trial_count:
            return False, "trial ledger count does not match trial_count"
        if not _manifest_windows_are_independent(manifest.get("windows"), as_of=as_of):
            return False, "train/calibration/test/lockbox windows overlap or are invalid"
        test_start, _ = _manifest_window_interval(manifest.get("windows"), "test")
        selected_at = _parse_aware_time(
            cast(Mapping[str, object], model_metadata).get("selected_at")
        )
        if selected_at is None or latest_trial_completion > selected_at:
            return False, "trial selection predates completion of the disclosed trial family"
        if latest_trial_completion >= test_start or selected_at >= test_start:
            return False, "trial selection was not frozen before the independent test"
        validation_reference = manifest.get("validation_returns")
        validation_path, validation_hash = _manifest_regular_file_reference(
            validation_reference, "validation returns"
        )
        if not _manifest_file_reference_matches(
            validation_reference, validation_path, validation_hash
        ):
            return False, "manifest validation-returns reference mismatch"
        statistics = _verified_validation_statistics(
            validation_path,
            evidence,
            trial_ids,
            manifest.get("windows"),
            as_of=as_of,
        )
        if not _close_probability(statistics["dsr"], evidence.dsr_probability):
            return False, "reported DSR does not match recomputed validation returns"
        if not _close_probability(statistics["pbo"], evidence.pbo_probability):
            return False, "reported PBO does not match recomputed validation returns"
        if statistics["sample_count"] != evidence.sample_count:
            return False, "sample_count does not match validation returns"
        if not _close_number(statistics["brier_improvement"], evidence.brier_improvement):
            return False, "Brier improvement does not match calibration audit"
        raw_statistics = _verified_promotion_observations(
            observations_path,
            evidence,
            windows=manifest.get("windows"),
            as_of=as_of,
        )
        if raw_statistics["timestamp_hash"] != statistics["timestamp_hash"]:
            return False, "evaluation observations do not align with validation timestamps"
        lockbox_statistics = _verified_lockbox_observations(
            lockbox_path,
            evidence,
            windows=manifest.get("windows"),
            as_of=as_of,
        )
        expected_raw_statistics: dict[str, object] = {
            "point_in_time_violations": evidence.point_in_time_violations,
            "future_feature_violations": evidence.future_feature_violations,
            "sample_count": evidence.sample_count,
            "net_expectancy_r": evidence.net_expectancy_r,
            "expectancy_ci_lower_r": evidence.expectancy_ci_lower_r,
            "max_drawdown_pct": evidence.max_drawdown_pct,
            "cost_stress_2x_expectancy_r": evidence.cost_stress_2x_expectancy_r,
            "regime_count": evidence.regime_count,
            "pair_count": evidence.pair_count,
            "major_operational_incidents": evidence.major_operational_incidents,
            "data_quality_incidents": evidence.data_quality_incidents,
        }
        for key, expected in expected_raw_statistics.items():
            actual = raw_statistics[key]
            if isinstance(actual, float) or isinstance(expected, float):
                matches = _close_number(actual, expected)
            else:
                matches = type(actual) is type(expected) and actual == expected
            if not matches:
                return False, f"{key} does not match immutable evaluation observations"
        if (
            lockbox_statistics["lockbox_evaluated_once"] is not evidence.lockbox_evaluated_once
            or lockbox_statistics["lockbox_reused_for_selection"]
            is not evidence.lockbox_reused_for_selection
        ):
            return False, "lockbox lifecycle does not match immutable lockbox observations"
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as error:
        return False, f"provenance verification failed: {type(error).__name__}"
    return (
        True,
        "artifact, structured manifest, complete trials, independent windows, "
        "and recomputed DSR/PBO/calibration verified",
    )


def _required_regular_file(value: object, label: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} path is required")
    path = Path(value).resolve()
    if not path.is_file():
        raise ValueError(f"{label} is not a regular file")
    return path


def _file_matches_hash(path: str | Path, expected_hash: object) -> bool:
    if not _is_sha256(expected_hash):
        return False
    try:
        file_path = _required_regular_file(str(path), "artifact")
        return artifact_sha256(file_path) == expected_hash
    except (OSError, ValueError):
        return False


def _manifest_embedded_references_match(path: str | Path) -> bool:
    try:
        manifest_path = _required_regular_file(str(path), "manifest")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if not isinstance(manifest, Mapping) or manifest.get("schema_version") != 1:
            return False
        for key in (
            "artifact",
            "trial_ledger",
            "validation_returns",
            "evaluation_observations",
            "lockbox_observations",
        ):
            reference = manifest.get(key)
            if not isinstance(reference, Mapping):
                return False
            referenced_path = _required_regular_file(reference.get("path"), key)
            digest = reference.get("sha256")
            if not _is_sha256(digest) or artifact_sha256(referenced_path) != digest:
                return False
        return True
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return False


def _manifest_file_reference_matches(
    value: object,
    actual_path: Path,
    expected_hash: object,
) -> bool:
    return bool(
        isinstance(value, Mapping)
        and isinstance(value.get("path"), str)
        and Path(str(value["path"])).resolve() == actual_path
        and value.get("sha256") == expected_hash
    )


def _verified_trial_ids(
    path: Path,
    evidence: PromotionEvidence,
    *,
    as_of: datetime,
) -> tuple[tuple[str, ...], datetime]:
    trial_ids: set[str] = set()
    ordered_ids: list[str] = []
    completion_times: list[datetime] = []
    for line_number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not raw_line.strip():
            raise ValueError(f"blank trial-ledger row at line {line_number}")
        row = json.loads(raw_line)
        if not isinstance(row, Mapping):
            raise ValueError("trial-ledger rows must be JSON objects")
        trial_id = row.get("trial_id")
        if not isinstance(trial_id, str) or not trial_id.strip():
            raise ValueError("every trial-ledger row requires trial_id")
        if trial_id in trial_ids:
            raise ValueError("trial-ledger trial_id values must be unique")
        if row.get("status") != "complete":
            raise ValueError("promotion trial ledger must contain only complete trials")
        if row.get("dataset_hash") != evidence.dataset_hash:
            raise ValueError("trial-ledger dataset_hash does not match evidence")
        if row.get("feature_version") != evidence.feature_version:
            raise ValueError("trial-ledger feature_version does not match evidence")
        if row.get("label_version") != evidence.label_version:
            raise ValueError("trial-ledger label_version does not match evidence")
        config_hash = row.get("config_hash")
        if not _is_sha256(config_hash):
            raise ValueError("every trial-ledger row requires config_hash")
        started = _parse_aware_time(row.get("started_at"))
        completed = _parse_aware_time(row.get("completed_at"))
        if started is None or completed is None or started >= completed or completed > as_of:
            raise ValueError("trial-ledger timestamps must be aware and ordered")
        trial_ids.add(trial_id)
        ordered_ids.append(trial_id)
        completion_times.append(completed)
    if not ordered_ids:
        raise ValueError("trial ledger must not be empty")
    if evidence.selected_trial_id not in trial_ids:
        raise ValueError("selected trial is absent from trial ledger")
    return tuple(ordered_ids), max(completion_times)


def _model_metadata_matches(
    value: object,
    evidence: PromotionEvidence,
    *,
    windows: object,
    as_of: datetime,
) -> bool:
    if not isinstance(value, Mapping):
        return False
    if value.get("schema_version") != 1:
        return False
    required = {
        "model_id": evidence.model_id,
        "dataset_hash": evidence.dataset_hash,
        "feature_version": evidence.feature_version,
        "label_version": evidence.label_version,
        "selected_trial_id": evidence.selected_trial_id,
        "git_commit": evidence.git_commit,
    }
    if any(value.get(key) != expected for key, expected in required.items()):
        return False
    trained_at = _parse_aware_time(value.get("trained_at"))
    data_cutoff = _parse_aware_time(value.get("data_cutoff"))
    selected_at = _parse_aware_time(value.get("selected_at"))
    model_format = value.get("model_format")
    try:
        _, train_end = _manifest_window_interval(windows, "train")
        tune_start, _ = _manifest_window_interval(windows, "tune")
        _, calibration_end = _manifest_window_interval(windows, "calibration")
        test_start, _ = _manifest_window_interval(windows, "test")
    except ValueError:
        return False
    return bool(
        trained_at is not None
        and data_cutoff is not None
        and selected_at is not None
        and data_cutoff <= trained_at
        and data_cutoff <= train_end
        and trained_at <= tune_start
        and calibration_end <= selected_at < test_start
        and trained_at <= as_of
        and isinstance(model_format, str)
        and model_format.strip()
    )


def _manifest_regular_file_reference(value: object, label: str) -> tuple[Path, str]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} reference must be an object")
    path = _required_regular_file(value.get("path"), label)
    digest = value.get("sha256")
    if not _is_sha256(digest) or artifact_sha256(path) != digest:
        raise ValueError(f"{label} SHA-256 mismatch")
    return path, str(digest)


def _verified_validation_statistics(
    path: Path,
    evidence: PromotionEvidence,
    trial_ids: tuple[str, ...],
    windows: object,
    *,
    as_of: datetime,
) -> dict[str, object]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, Mapping) or payload.get("schema_version") != 1:
        raise ValueError("validation returns schema mismatch")
    if payload.get("dataset_hash") != evidence.dataset_hash:
        raise ValueError("validation returns dataset_hash mismatch")
    if payload.get("selected_trial_id") != evidence.selected_trial_id:
        raise ValueError("validation returns selected_trial_id mismatch")
    timestamps = payload.get("timestamps")
    returns = payload.get("returns")
    if not isinstance(timestamps, list) or not isinstance(returns, Mapping):
        raise ValueError("validation returns must contain timestamps and returns")
    if set(returns) != set(trial_ids):
        raise ValueError("validation return trials do not match complete trial ledger")
    index = pd.DatetimeIndex(timestamps)
    if index.tz is None or not index.is_monotonic_increasing or index.has_duplicates:
        raise ValueError("validation timestamps must be unique, aware, and ordered")
    test_interval = _manifest_window_interval(windows, "test")
    if (
        len(index) == 0
        or index[0].to_pydatetime() < test_interval[0]
        or index[-1].to_pydatetime() > test_interval[1]
        or index[-1].to_pydatetime() > as_of
    ):
        raise ValueError("validation returns fall outside the independent test window")
    matrix = pd.DataFrame({trial_id: returns[trial_id] for trial_id in trial_ids}, index=index)
    if matrix.shape != (len(index), len(trial_ids)):
        raise ValueError("validation returns have inconsistent trial lengths")
    selected_trial_id = str(evidence.selected_trial_id)
    trial_sharpes = [per_period_sharpe(matrix[trial_id]) for trial_id in trial_ids]
    dsr = deflated_sharpe_ratio(matrix[selected_trial_id], trial_sharpes)
    n_blocks = payload.get("pbo_n_blocks")
    if not isinstance(n_blocks, int) or isinstance(n_blocks, bool):
        raise ValueError("validation returns require integer pbo_n_blocks")
    pbo = probability_of_backtest_overfitting(matrix, n_blocks=n_blocks)
    calibration = payload.get("calibration_holdout")
    if not isinstance(calibration, Mapping):
        raise ValueError("validation returns require an independent calibration holdout audit")
    raw_brier, calibrated_brier = _recomputed_validation_brier(
        calibration,
        window=_manifest_window_interval(windows, "test"),
        as_of=as_of,
        availability_cutoff=_manifest_window_interval(windows, "lockbox")[0],
    )
    if not 0.0 <= calibrated_brier < raw_brier <= 1.0:
        raise ValueError("calibration audit must demonstrate lower Brier loss")
    return {
        "dsr": float(dsr["dsr"]),
        "pbo": float(pbo["pbo"]),
        "sample_count": len(index),
        "brier_improvement": raw_brier - calibrated_brier,
        "timestamp_hash": _timestamp_sequence_hash(
            [timestamp.to_pydatetime() for timestamp in index]
        ),
    }


def _recomputed_validation_brier(
    payload: Mapping[str, object],
    *,
    window: tuple[datetime, datetime],
    as_of: datetime,
    availability_cutoff: datetime,
) -> tuple[float, float]:
    required_keys = {
        "prediction_time",
        "label_end_time",
        "label_available_time",
        "horizon_seconds",
        "barrier_path_sha256",
        "barrier_path",
        "y_true",
        "raw_probability",
        "calibrated_probability",
    }
    if set(payload) != required_keys:
        raise ValueError("calibration audit must use the closed label-evidence schema")
    timestamps = payload.get("prediction_time")
    labels = payload.get("y_true")
    raw_probabilities = payload.get("raw_probability")
    calibrated_probabilities = payload.get("calibrated_probability")
    if not all(
        isinstance(value, list)
        for value in (timestamps, labels, raw_probabilities, calibrated_probabilities)
    ):
        raise ValueError("calibration audit requires timestamp/label/probability arrays")
    assert isinstance(timestamps, list)
    assert isinstance(labels, list)
    assert isinstance(raw_probabilities, list)
    assert isinstance(calibrated_probabilities, list)
    rows = len(timestamps)
    if rows < 200 or any(
        len(values) != rows for values in (labels, raw_probabilities, calibrated_probabilities)
    ):
        raise ValueError("calibration audit requires at least 200 aligned observations")
    _verified_label_timing(
        payload,
        expected_prediction_times=None,
        window=window,
        availability_cutoff=availability_cutoff,
        as_of=as_of,
    )
    raw_loss = 0.0
    calibrated_loss = 0.0
    for position, (label, raw, calibrated) in enumerate(
        zip(labels, raw_probabilities, calibrated_probabilities, strict=True)
    ):
        if not isinstance(label, int) or isinstance(label, bool) or label not in (0, 1):
            raise ValueError(f"calibration label[{position}] must be binary integer")
        if not _is_finite_number(raw) or not _is_finite_number(calibrated):
            raise ValueError("calibration probabilities must be finite")
        raw_value = float(raw)
        calibrated_value = float(calibrated)
        if not 0.0 <= raw_value <= 1.0 or not 0.0 <= calibrated_value <= 1.0:
            raise ValueError("calibration probabilities must be in [0, 1]")
        raw_loss += (raw_value - label) ** 2
        calibrated_loss += (calibrated_value - label) ** 2
    return raw_loss / rows, calibrated_loss / rows


def _verified_promotion_observations(
    path: Path,
    evidence: PromotionEvidence,
    *,
    windows: object,
    as_of: datetime,
) -> dict[str, object]:
    """Recompute non-model-selection promotion gates from immutable raw rows."""

    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, Mapping) or payload.get("schema_version") != 1:
        raise ValueError("evaluation observations schema mismatch")
    if payload.get("dataset_hash") != evidence.dataset_hash:
        raise ValueError("evaluation observations dataset_hash mismatch")
    if payload.get("selected_trial_id") != evidence.selected_trial_id:
        raise ValueError("evaluation observations selected_trial_id mismatch")
    required_keys = {
        "schema_version",
        "dataset_hash",
        "selected_trial_id",
        "timestamps",
        "net_r",
        "cost_stress_2x_net_r",
        "initial_equity",
        "risk_fraction",
        "equity",
        "pairs",
        "regimes",
        "integrity",
        "incidents",
        "label_end_time",
        "label_available_time",
        "horizon_seconds",
        "barrier_path_sha256",
        "barrier_path",
    }
    if set(payload) != required_keys:
        raise ValueError("evaluation observations must use the closed schema")
    raw_timestamps = payload.get("timestamps")
    raw_net_r = payload.get("net_r")
    raw_cost_2x = payload.get("cost_stress_2x_net_r")
    raw_risk = payload.get("risk_fraction")
    raw_equity = payload.get("equity")
    raw_pairs = payload.get("pairs")
    raw_regimes = payload.get("regimes")
    if not all(
        isinstance(value, list)
        for value in (
            raw_timestamps,
            raw_net_r,
            raw_cost_2x,
            raw_risk,
            raw_equity,
            raw_pairs,
            raw_regimes,
        )
    ):
        raise ValueError("evaluation observations require aligned arrays")
    assert isinstance(raw_timestamps, list)
    assert isinstance(raw_net_r, list)
    assert isinstance(raw_cost_2x, list)
    assert isinstance(raw_risk, list)
    assert isinstance(raw_equity, list)
    assert isinstance(raw_pairs, list)
    assert isinstance(raw_regimes, list)
    rows = len(raw_timestamps)
    if rows < 200 or any(
        len(values) != rows
        for values in (raw_net_r, raw_cost_2x, raw_risk, raw_equity, raw_pairs, raw_regimes)
    ):
        raise ValueError("evaluation observations require at least 200 aligned rows")
    observed_times = [_parse_aware_time(value) for value in raw_timestamps]
    if any(value is None for value in observed_times):
        raise ValueError("evaluation observation timestamps must be timezone-aware")
    timestamps = [cast(datetime, value) for value in observed_times]
    test_start, test_end = _manifest_window_interval(windows, "test")
    if any(
        timestamp < test_start or timestamp > test_end or timestamp > as_of
        for timestamp in timestamps
    ) or any(left >= right for left, right in zip(timestamps, timestamps[1:])):
        raise ValueError("evaluation observations must be unique and inside the test window")
    lockbox_start, _ = _manifest_window_interval(windows, "lockbox")
    _verified_label_timing(
        payload,
        expected_prediction_times=timestamps,
        window=(test_start, test_end),
        availability_cutoff=lockbox_start,
        as_of=as_of,
    )

    net_r = [_strict_finite_number(value, "net_r") for value in raw_net_r]
    cost_2x = [_strict_finite_number(value, "cost_stress_2x_net_r") for value in raw_cost_2x]
    if any(stressed > base + 1e-12 for base, stressed in zip(net_r, cost_2x, strict=True)):
        raise ValueError("2x cost observations cannot improve net R")
    risk_fraction = [_strict_finite_number(value, "risk_fraction") for value in raw_risk]
    if any(value <= 0.0 or value > 0.10 for value in risk_fraction):
        raise ValueError("risk fractions must be in (0, 0.10]")
    initial_equity = _strict_finite_number(payload.get("initial_equity"), "initial_equity")
    if initial_equity <= 0.0:
        raise ValueError("initial_equity must be positive")
    equity = [_strict_finite_number(value, "equity") for value in raw_equity]
    expected_equity = initial_equity
    for position, (realized_r, fraction, observed_equity) in enumerate(
        zip(net_r, risk_fraction, equity, strict=True)
    ):
        expected_equity *= 1.0 + realized_r * fraction
        if expected_equity <= 0.0 or not math.isclose(
            observed_equity,
            expected_equity,
            rel_tol=1e-12,
            abs_tol=1e-8,
        ):
            raise ValueError(f"equity does not reconcile at row {position}")
    pairs = [_strict_nonempty_text(value, "pair") for value in raw_pairs]
    regimes = [_strict_nonempty_text(value, "regime") for value in raw_regimes]

    bootstrap = circular_block_bootstrap_mean_ci(
        pd.Series(net_r),
        block_size=min(5, rows),
        resamples=2_000,
        confidence=0.95,
        seed=42,
    )
    running_peak = initial_equity
    max_drawdown = 0.0
    for value in equity:
        running_peak = max(running_peak, value)
        max_drawdown = max(max_drawdown, (running_peak - value) / running_peak)

    integrity = payload.get("integrity")
    if not isinstance(integrity, Mapping) or set(integrity) != {
        "point_in_time_violations",
        "future_feature_violations",
    }:
        raise ValueError("evaluation integrity summary uses an invalid schema")
    pit_violations = _strict_nonnegative_integer(
        integrity.get("point_in_time_violations"),
        "point_in_time_violations",
    )
    future_violations = _strict_nonnegative_integer(
        integrity.get("future_feature_violations"),
        "future_feature_violations",
    )

    incidents = payload.get("incidents")
    if not isinstance(incidents, list):
        raise ValueError("evaluation incidents must be a list")
    operational_incidents = 0
    data_incidents = 0
    incident_ids: set[str] = set()
    for row in incidents:
        if not isinstance(row, Mapping) or set(row) != {
            "incident_id",
            "category",
            "severity",
            "status",
        }:
            raise ValueError("evaluation incident uses an invalid schema")
        incident_id = _strict_nonempty_text(row.get("incident_id"), "incident_id")
        if incident_id in incident_ids:
            raise ValueError("evaluation incident IDs must be unique")
        incident_ids.add(incident_id)
        category = row.get("category")
        severity = row.get("severity")
        status = row.get("status")
        if category not in {"operational", "data_quality"}:
            raise ValueError("evaluation incident category is invalid")
        if severity not in {"minor", "major"} or status not in {"open", "resolved"}:
            raise ValueError("evaluation incident severity/status is invalid")
        if status == "open" and category == "data_quality":
            data_incidents += 1
        if status == "open" and category == "operational" and severity == "major":
            operational_incidents += 1

    return {
        "point_in_time_violations": pit_violations,
        "future_feature_violations": future_violations,
        "sample_count": rows,
        "net_expectancy_r": float(sum(net_r) / rows),
        "expectancy_ci_lower_r": float(bootstrap.lower),
        "max_drawdown_pct": float(max_drawdown),
        "cost_stress_2x_expectancy_r": float(sum(cost_2x) / rows),
        "regime_count": len(set(regimes)),
        "pair_count": len(set(pairs)),
        "major_operational_incidents": operational_incidents,
        "data_quality_incidents": data_incidents,
        "timestamp_hash": _timestamp_sequence_hash(timestamps),
    }


def _verified_lockbox_observations(
    path: Path,
    evidence: PromotionEvidence,
    *,
    windows: object,
    as_of: datetime,
) -> dict[str, object]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    required_keys = {
        "schema_version",
        "dataset_hash",
        "selected_trial_id",
        "model_id",
        "artifact_hash",
        "timestamps",
        "net_r",
        "cost_stress_2x_net_r",
        "initial_equity",
        "risk_fraction",
        "equity",
        "calibration",
        "integrity",
        "evaluation",
        "label_end_time",
        "label_available_time",
        "horizon_seconds",
        "barrier_path_sha256",
        "barrier_path",
    }
    if (
        not isinstance(payload, Mapping)
        or set(payload) != required_keys
        or payload.get("schema_version") != 1
    ):
        raise ValueError("lockbox observations must use the closed schema")
    if (
        payload.get("dataset_hash") != evidence.dataset_hash
        or payload.get("selected_trial_id") != evidence.selected_trial_id
        or payload.get("model_id") != evidence.model_id
        or payload.get("artifact_hash") != evidence.artifact_hash
    ):
        raise ValueError("lockbox observations are not bound to the selected artifact")
    raw_timestamps = payload.get("timestamps")
    raw_net_r = payload.get("net_r")
    raw_cost_2x = payload.get("cost_stress_2x_net_r")
    raw_risk = payload.get("risk_fraction")
    raw_equity = payload.get("equity")
    if not all(
        isinstance(value, list)
        for value in (raw_timestamps, raw_net_r, raw_cost_2x, raw_risk, raw_equity)
    ):
        raise ValueError("lockbox observations require aligned arrays")
    assert isinstance(raw_timestamps, list)
    assert isinstance(raw_net_r, list)
    assert isinstance(raw_cost_2x, list)
    assert isinstance(raw_risk, list)
    assert isinstance(raw_equity, list)
    rows = len(raw_timestamps)
    if rows < 200 or any(
        len(values) != rows for values in (raw_net_r, raw_cost_2x, raw_risk, raw_equity)
    ):
        raise ValueError("lockbox observations require at least 200 aligned rows")
    observed_times = [_parse_aware_time(value) for value in raw_timestamps]
    if any(value is None for value in observed_times):
        raise ValueError("lockbox timestamps must be timezone-aware")
    timestamps = [cast(datetime, value) for value in observed_times]
    lockbox_start, lockbox_end = _manifest_window_interval(windows, "lockbox")
    if any(
        timestamp < lockbox_start or timestamp > lockbox_end or timestamp > as_of
        for timestamp in timestamps
    ) or any(left >= right for left, right in zip(timestamps, timestamps[1:])):
        raise ValueError("lockbox timestamps must be unique and inside the lockbox window")
    evaluation = payload.get("evaluation")
    if not isinstance(evaluation, Mapping):
        raise ValueError("lockbox evaluation uses an invalid schema")
    evaluated_at = _parse_aware_time(evaluation.get("evaluated_at"))
    if evaluated_at is None:
        raise ValueError("lockbox evaluation timestamp is invalid")
    _verified_label_timing(
        payload,
        expected_prediction_times=timestamps,
        window=(lockbox_start, lockbox_end),
        availability_cutoff=evaluated_at,
        as_of=as_of,
    )
    net_r = [_strict_finite_number(value, "lockbox net_r") for value in raw_net_r]
    cost_2x = [
        _strict_finite_number(value, "lockbox cost_stress_2x_net_r") for value in raw_cost_2x
    ]
    if any(stressed > base + 1e-12 for base, stressed in zip(net_r, cost_2x, strict=True)):
        raise ValueError("lockbox 2x cost observations cannot improve net R")
    risk_fraction = [_strict_finite_number(value, "lockbox risk_fraction") for value in raw_risk]
    if any(value <= 0.0 or value > 0.10 for value in risk_fraction):
        raise ValueError("lockbox risk fractions must be in (0, 0.10]")
    initial_equity = _strict_finite_number(payload.get("initial_equity"), "initial_equity")
    if initial_equity <= 0.0:
        raise ValueError("lockbox initial_equity must be positive")
    equity = [_strict_finite_number(value, "lockbox equity") for value in raw_equity]
    expected_equity = initial_equity
    for position, (realized_r, fraction, observed_equity) in enumerate(
        zip(net_r, risk_fraction, equity, strict=True)
    ):
        expected_equity *= 1.0 + realized_r * fraction
        if expected_equity <= 0.0 or not math.isclose(
            observed_equity,
            expected_equity,
            rel_tol=1e-12,
            abs_tol=1e-8,
        ):
            raise ValueError(f"lockbox equity does not reconcile at row {position}")
    calibration = payload.get("calibration")
    if not isinstance(calibration, Mapping) or calibration.get("prediction_time") != raw_timestamps:
        raise ValueError("lockbox calibration must align with lockbox observations")
    raw_brier, calibrated_brier = _recomputed_validation_brier(
        calibration,
        window=(lockbox_start, lockbox_end),
        as_of=as_of,
        availability_cutoff=evaluated_at,
    )
    if not 0.0 <= calibrated_brier < raw_brier <= 1.0:
        raise ValueError("lockbox calibration must demonstrate lower Brier loss")
    integrity = payload.get("integrity")
    if not isinstance(integrity, Mapping) or set(integrity) != {
        "point_in_time_violations",
        "future_feature_violations",
    }:
        raise ValueError("lockbox integrity summary uses an invalid schema")
    pit_violations = _strict_nonnegative_integer(
        integrity.get("point_in_time_violations"), "lockbox point_in_time_violations"
    )
    future_violations = _strict_nonnegative_integer(
        integrity.get("future_feature_violations"), "lockbox future_feature_violations"
    )
    if not isinstance(evaluation, Mapping) or set(evaluation) != {
        "evaluation_id",
        "evaluated_at",
        "model_id",
        "artifact_hash",
        "dataset_hash",
        "selected_trial_id",
        "reused_for_selection",
    }:
        raise ValueError("lockbox evaluation uses an invalid schema")
    _strict_nonempty_text(evaluation.get("evaluation_id"), "lockbox evaluation_id")
    if evaluated_at is None or evaluated_at < lockbox_end or evaluated_at > as_of:
        raise ValueError("lockbox evaluation timestamp is outside the permitted interval")
    if (
        evaluation.get("model_id") != evidence.model_id
        or evaluation.get("artifact_hash") != evidence.artifact_hash
        or evaluation.get("dataset_hash") != evidence.dataset_hash
        or evaluation.get("selected_trial_id") != evidence.selected_trial_id
        or not isinstance(evaluation.get("reused_for_selection"), bool)
    ):
        raise ValueError("lockbox evaluation is not bound to the selected artifact")
    running_peak = initial_equity
    max_drawdown = 0.0
    for value in equity:
        running_peak = max(running_peak, value)
        max_drawdown = max(max_drawdown, (running_peak - value) / running_peak)
    return {
        "sample_count": rows,
        "net_expectancy_r": float(sum(net_r) / rows),
        "max_drawdown_pct": float(max_drawdown),
        "cost_stress_2x_expectancy_r": float(sum(cost_2x) / rows),
        "brier_improvement": float(raw_brier - calibrated_brier),
        "point_in_time_violations": pit_violations,
        "future_feature_violations": future_violations,
        "lockbox_evaluated_once": True,
        "lockbox_reused_for_selection": bool(evaluation["reused_for_selection"]),
        "timestamp_hash": _timestamp_sequence_hash(timestamps),
    }


def _verified_lockbox_gate_statistics(
    evidence: PromotionEvidence,
    *,
    as_of: datetime,
) -> dict[str, object]:
    try:
        manifest_path = _required_regular_file(evidence.evidence_manifest_path, "manifest")
        lockbox_path = _required_regular_file(
            evidence.lockbox_observations_path, "lockbox observations"
        )
        if artifact_sha256(lockbox_path) != evidence.lockbox_observations_hash:
            return {}
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if not isinstance(manifest, Mapping):
            return {}
        reference = manifest.get("lockbox_observations")
        if not _manifest_file_reference_matches(
            reference, lockbox_path, evidence.lockbox_observations_hash
        ):
            return {}
        return _verified_lockbox_observations(
            lockbox_path,
            evidence,
            windows=manifest.get("windows"),
            as_of=as_of,
        )
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return {}


def _timestamp_sequence_hash(values: list[datetime]) -> str:
    encoded = json.dumps(
        [value.astimezone(UTC).isoformat() for value in values],
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _verified_label_timing(
    payload: Mapping[str, object],
    *,
    expected_prediction_times: list[datetime] | None,
    window: tuple[datetime, datetime],
    availability_cutoff: datetime,
    as_of: datetime,
) -> list[datetime]:
    """Verify label timing, full-horizon purge, and immutable barrier-path binding."""

    raw_prediction = payload.get("prediction_time", payload.get("timestamps"))
    raw_end = payload.get("label_end_time")
    raw_available = payload.get("label_available_time")
    raw_horizon = payload.get("horizon_seconds")
    raw_barrier_hashes = payload.get("barrier_path_sha256")
    arrays = (raw_prediction, raw_end, raw_available, raw_horizon, raw_barrier_hashes)
    if not all(isinstance(value, list) for value in arrays):
        raise ValueError("label evidence requires aligned timing and barrier arrays")
    assert isinstance(raw_prediction, list)
    assert isinstance(raw_end, list)
    assert isinstance(raw_available, list)
    assert isinstance(raw_horizon, list)
    assert isinstance(raw_barrier_hashes, list)
    rows = len(raw_prediction)
    aligned_arrays = (raw_end, raw_available, raw_horizon, raw_barrier_hashes)
    if rows == 0 or any(len(value) != rows for value in aligned_arrays):
        raise ValueError("label evidence timing arrays must be non-empty and aligned")

    _, barrier_hash = _manifest_regular_file_reference(payload.get("barrier_path"), "barrier path")
    if any(value != barrier_hash for value in raw_barrier_hashes):
        raise ValueError("every label row must bind the verified barrier-path artifact")

    prediction_times = [_parse_aware_time(value) for value in raw_prediction]
    end_times = [_parse_aware_time(value) for value in raw_end]
    available_times = [_parse_aware_time(value) for value in raw_available]
    if any(
        value is None
        for values in (prediction_times, end_times, available_times)
        for value in values
    ):
        raise ValueError("label timestamps must be timezone-aware")
    predictions = [cast(datetime, value) for value in prediction_times]
    ends = [cast(datetime, value) for value in end_times]
    available = [cast(datetime, value) for value in available_times]
    if expected_prediction_times is not None and predictions != expected_prediction_times:
        raise ValueError("label prediction times do not align with evaluation observations")
    if any(left >= right for left, right in zip(predictions, predictions[1:])):
        raise ValueError("label prediction times must be unique and ordered")

    for position, (prediction, label_end, label_available, raw_seconds) in enumerate(
        zip(predictions, ends, available, raw_horizon, strict=True)
    ):
        if not _is_finite_number(raw_seconds) or float(raw_seconds) <= 0.0:
            raise ValueError(f"label horizon_seconds[{position}] must be finite and positive")
        horizon = timedelta(seconds=float(raw_seconds))
        horizon_end = prediction + horizon
        if prediction < window[0] or prediction > window[1] or prediction > as_of:
            raise ValueError("label prediction time falls outside its evaluation window")
        if not prediction < label_end <= horizon_end:
            raise ValueError("label end time must be after prediction and within its horizon")
        if not label_end <= label_available <= availability_cutoff:
            raise ValueError("label was unavailable before the next evaluation window")
        if horizon_end > availability_cutoff:
            raise ValueError("label horizon is not purged before the next evaluation window")
        if label_available > as_of:
            raise ValueError("label availability is after the evaluation cutoff")
    return predictions


def _strict_finite_number(value: object, label: str) -> float:
    if not _is_finite_number(value):
        raise ValueError(f"{label} must be a finite number")
    return float(value)


def _strict_nonempty_text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty string")
    return value.strip()


def _strict_nonnegative_integer(value: object, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"{label} must be a non-negative integer")
    return value


def _manifest_window_interval(value: object, name: str) -> tuple[datetime, datetime]:
    if not isinstance(value, Mapping) or not isinstance(value.get(name), Mapping):
        raise ValueError(f"manifest {name} window is missing")
    raw = value[name]
    start = _parse_aware_time(raw.get("start"))
    end = _parse_aware_time(raw.get("end"))
    if start is None or end is None or start >= end:
        raise ValueError(f"manifest {name} window is invalid")
    return start, end


def _required_finite_metric(payload: Mapping[str, object], key: str) -> float:
    value = payload.get(key)
    if not _is_finite_number(value):
        raise ValueError(f"{key} must be finite")
    return float(value)


def _close_probability(actual: object, expected: object) -> bool:
    return bool(
        _is_finite_number(actual)
        and _is_finite_number(expected)
        and 0.0 <= float(actual) <= 1.0
        and math.isclose(float(actual), float(expected), rel_tol=0.0, abs_tol=1e-12)
    )


def _close_number(actual: object, expected: object) -> bool:
    return bool(
        _is_finite_number(actual)
        and _is_finite_number(expected)
        and math.isclose(float(actual), float(expected), rel_tol=0.0, abs_tol=1e-12)
    )


def _manifest_evidence_payload(evidence: PromotionEvidence) -> dict[str, object]:
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
    return {key: value for key, value in asdict(evidence).items() if key not in excluded}


def _manifest_windows_are_independent(value: object, *, as_of: datetime) -> bool:
    if not isinstance(value, Mapping):
        return False
    names = ("train", "tune", "calibration", "test", "lockbox")
    intervals: list[tuple[datetime, datetime]] = []
    for name in names:
        raw = value.get(name)
        if not isinstance(raw, Mapping):
            return False
        start = _parse_aware_time(raw.get("start"))
        end = _parse_aware_time(raw.get("end"))
        if start is None or end is None or start >= end or end > as_of:
            return False
        intervals.append((start, end))
    return all(left[1] < right[0] for left, right in zip(intervals, intervals[1:]))


def _parse_aware_time(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(UTC)


def _is_sha256(value: object) -> bool:
    return bool(
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _at_least(value: float | None, threshold: float) -> bool:
    return _is_finite_number(value) and float(value) >= threshold


def _greater_than(value: float | None, threshold: float) -> bool:
    return _is_finite_number(value) and float(value) > threshold


def _is_finite_number(value: object) -> TypeGuard[int | float]:
    return isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value)


def _validate_finite_metric_tree(value: object, label: str) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str) or not key.strip():
                raise ValueError(f"{label} keys must be non-empty strings")
            _validate_finite_metric_tree(item, f"{label}.{key}")
        return
    if isinstance(value, (list, tuple)):
        for position, item in enumerate(value):
            _validate_finite_metric_tree(item, f"{label}[{position}]")
        return
    if not _is_finite_number(value):
        raise ValueError(f"{label} leaves must be finite numeric values")


def _validate_model_record_as_of(record: ModelRecord, as_of: datetime) -> None:
    if as_of.tzinfo is None:
        raise GovernanceError("model record as_of must be timezone-aware")
    trained_at = _parse_aware_time(record.trained_at)
    data_cutoff = _parse_aware_time(record.data_cutoff)
    if (
        trained_at is None
        or data_cutoff is None
        or data_cutoff > trained_at
        or trained_at > as_of.astimezone(UTC)
    ):
        raise GovernanceError("model record contains future or invalid timestamps")


def _is_integer_equal(value: object, expected: int) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value == expected


def _integer_at_least(value: object, threshold: int) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= threshold


def _probability_at_least(value: float | None, threshold: float) -> bool:
    return _is_finite_number(value) and 0.0 <= float(value) <= 1.0 and float(value) >= threshold


def _probability_at_most(value: float | None, threshold: float) -> bool:
    return _is_finite_number(value) and 0.0 <= float(value) <= 1.0 and float(value) <= threshold


def _bounded_at_most(value: float | None, threshold: float, lower: float, upper: float) -> bool:
    return _is_finite_number(value) and lower <= float(value) <= upper and float(value) <= threshold


def _bounded_greater_than(
    value: float | None,
    threshold: float,
    lower: float,
    upper: float,
) -> bool:
    return _is_finite_number(value) and lower <= float(value) <= upper and float(value) > threshold


def _promotion_report_id(report: PromotionReport) -> str:
    payload = {
        "model_id": report.model_id,
        "artifact_hash": report.artifact_hash,
        "evidence_manifest_path": report.evidence_manifest_path,
        "evidence_manifest_hash": report.evidence_manifest_hash,
        "trial_ledger_path": report.trial_ledger_path,
        "trial_ledger_hash": report.trial_ledger_hash,
        "evaluation_observations_path": report.evaluation_observations_path,
        "evaluation_observations_hash": report.evaluation_observations_hash,
        "lockbox_observations_path": report.lockbox_observations_path,
        "lockbox_observations_hash": report.lockbox_observations_hash,
        "evaluated_at": report.evaluated_at,
        "policy": report.policy,
        "evidence": report.evidence,
        "target_stage": report.target_stage,
        "policy_rationale": report.policy_rationale,
        "gates": [gate.to_dict() for gate in report.gates],
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
