"""Fail-closed model promotion, immutable audit events, and hard risk vetoes."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path
from typing import Literal

ModelStage = Literal["research", "validated", "shadow", "paper", "limited_live", "live"]
STAGES: tuple[ModelStage, ...] = (
    "research",
    "validated",
    "shadow",
    "paper",
    "limited_live",
    "live",
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


@dataclass(frozen=True)
class PromotionEvidence:
    dataset_hash: str | None = None
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

    @property
    def passed(self) -> bool:
        return all(gate.passed for gate in self.gates if gate.critical)

    @property
    def failures(self) -> tuple[str, ...]:
        return tuple(gate.name for gate in self.gates if gate.critical and not gate.passed)

    def to_dict(self) -> dict[str, object]:
        return {
            "target_stage": self.target_stage,
            "passed": self.passed,
            "failures": list(self.failures),
            "policy_rationale": self.policy_rationale,
            "gates": [gate.to_dict() for gate in self.gates],
        }


def evaluate_promotion(
    evidence: PromotionEvidence,
    *,
    target_stage: ModelStage = "validated",
    policy: PromotionPolicy | None = None,
) -> PromotionReport:
    """Evaluate every required signal; missing evidence is a failed gate."""

    if target_stage not in STAGES:
        raise ValueError(f"unknown model stage: {target_stage}")
    settings = policy or PromotionPolicy()
    gates: list[PromotionGate] = []

    def require(name: str, observed: object, passed: bool, requirement: str) -> None:
        gates.append(PromotionGate(name, bool(passed), observed, requirement))

    require(
        "dataset_hash",
        evidence.dataset_hash,
        _is_sha256(evidence.dataset_hash),
        "immutable dataset SHA-256 is recorded",
    )
    require(
        "git_commit",
        evidence.git_commit,
        bool(evidence.git_commit and len(evidence.git_commit) >= 7),
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
        evidence.point_in_time_violations == 0,
        "zero point-in-time violations",
    )
    require(
        "future_feature_integrity",
        evidence.future_feature_violations,
        evidence.future_feature_violations == 0,
        "zero future-feature violations",
    )
    require(
        "all_trials_recorded",
        evidence.trial_count,
        evidence.trial_count is not None and evidence.trial_count >= 1,
        "at least one trial and the complete trial ledger are recorded",
    )
    require(
        "sample_size",
        evidence.sample_count,
        evidence.sample_count is not None and evidence.sample_count >= settings.min_samples,
        f"sample_count >= {settings.min_samples}",
    )
    require(
        "net_expectancy",
        evidence.net_expectancy_r,
        _at_least(evidence.net_expectancy_r, settings.min_net_expectancy_r),
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
        _at_least(evidence.dsr_probability, settings.min_dsr_probability),
        f"DSR probability >= {settings.min_dsr_probability:.2f}",
    )
    require(
        "probability_of_backtest_overfitting",
        evidence.pbo_probability,
        _at_most(evidence.pbo_probability, settings.max_pbo_probability),
        f"PBO <= {settings.max_pbo_probability:.2f}",
    )
    require(
        "drawdown",
        evidence.max_drawdown_pct,
        _at_most(evidence.max_drawdown_pct, settings.max_drawdown_pct),
        f"max drawdown <= {settings.max_drawdown_pct:.1%}",
    )
    require(
        "calibration_improvement",
        evidence.brier_improvement,
        _greater_than(evidence.brier_improvement, settings.min_brier_improvement),
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
        evidence.regime_count is not None and evidence.regime_count >= settings.min_regimes,
        f"at least {settings.min_regimes} regimes",
    )
    require(
        "pair_coverage",
        evidence.pair_count,
        evidence.pair_count is not None and evidence.pair_count >= settings.min_pairs,
        f"at least {settings.min_pairs} currency pairs",
    )
    require(
        "operational_incidents",
        evidence.major_operational_incidents,
        evidence.major_operational_incidents == 0,
        "zero unresolved major operational incidents",
    )
    require(
        "data_quality_incidents",
        evidence.data_quality_incidents,
        evidence.data_quality_incidents == 0,
        "zero unresolved data-quality incidents",
    )

    if target_stage in {"paper", "limited_live", "live"}:
        require(
            "shadow_duration",
            evidence.shadow_days,
            evidence.shadow_days is not None
            and evidence.shadow_days >= settings.min_shadow_days_for_paper,
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
            evidence.paper_days is not None
            and evidence.paper_days >= settings.min_paper_days_for_limited_live,
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
    return PromotionReport(target_stage, tuple(gates), settings.rationale)


@dataclass
class ModelRecord:
    model_id: str
    artifact_hash: str
    trained_at: str
    data_cutoff: str
    metrics: dict[str, object]
    calibration_metrics: dict[str, object]
    limitations: list[str]
    stage: ModelStage = "research"
    approved_by: str = ""
    promotion_reason: str = ""
    demotion_reason: str = ""

    def __post_init__(self) -> None:
        if not self.model_id.strip():
            raise ValueError("model_id is required")
        if not _is_sha256(self.artifact_hash):
            raise ValueError("artifact_hash must be a SHA-256")
        if self.stage not in STAGES:
            raise ValueError("invalid model stage")


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
        current_index = STAGES.index(record.stage)
        if current_index + 1 >= len(STAGES) or STAGES[current_index + 1] != report.target_stage:
            raise GovernanceError("promotion must move exactly one stage")
        if report.target_stage in {"limited_live", "live"}:
            raise GovernanceError("this registry build cannot enable live trading")
        if not report.passed:
            raise GovernanceError(f"promotion evidence failed: {', '.join(report.failures)}")
        if not approved_by.strip() or not reason.strip():
            raise GovernanceError("human approver and promotion reason are required")
        previous = record.stage
        record.stage = report.target_stage
        record.approved_by = approved_by.strip()
        record.promotion_reason = reason.strip()
        record.demotion_reason = ""
        self._event(
            model_id,
            "promoted",
            previous,
            record.stage,
            actor=record.approved_by,
            reason=record.promotion_reason,
            report=report.to_dict(),
            now=now,
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
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
        )
        os.replace(temporary, target)

    @classmethod
    def load(cls, path: str | Path) -> ModelRegistry:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        if payload.get("schema_version") != cls.schema_version:
            raise GovernanceError("model registry schema mismatch")
        registry = cls()
        raw_models = payload.get("models")
        if not isinstance(raw_models, Mapping):
            raise GovernanceError("model registry models must be an object")
        registry.models = {
            str(model_id): ModelRecord(**dict(raw))
            for model_id, raw in raw_models.items()
            if isinstance(raw, Mapping)
        }
        raw_events = payload.get("events")
        if not isinstance(raw_events, list) or not all(
            isinstance(event, dict) for event in raw_events
        ):
            raise GovernanceError("model registry events must be a list of objects")
        registry.events = [dict(event) for event in raw_events]
        return registry

    def _record(self, model_id: str) -> ModelRecord:
        try:
            return self.models[model_id]
        except KeyError as error:
            raise GovernanceError(f"unknown model: {model_id}") from error

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


def _is_sha256(value: str | None) -> bool:
    return bool(
        value and len(value) == 64 and all(character in "0123456789abcdef" for character in value)
    )


def _at_least(value: float | None, threshold: float) -> bool:
    return value is not None and value >= threshold


def _greater_than(value: float | None, threshold: float) -> bool:
    return value is not None and value > threshold


def _at_most(value: float | None, threshold: float) -> bool:
    return value is not None and value <= threshold
