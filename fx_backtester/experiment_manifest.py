"""Authoritative experiment manifest: a strict, content-addressed run declaration.

The manifest is the single pre-registration document for one experiment. It is
stored as JSON, not YAML, because the runtime dependency set (requirements.lock)
does not include a YAML parser and canonical JSON is already the repository's
hashing substrate. Unknown keys, missing keys, naive datetimes and out-of-range
values are rejected with typed failures instead of being defaulted.

Version 1 deliberately binds one symbol, one horizon and one label family per
experiment; multi-pair aggregation is a separate, explicitly future contract.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fx_backtester.failures import FailureReason, TypedFailure

MANIFEST_SCHEMA_VERSION = 1

_EXPERIMENT_ID = re.compile(r"^[a-z0-9][a-z0-9_\-]{2,63}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_GIT_COMMIT = re.compile(r"^[0-9a-f]{40}$|^[0-9a-f]{64}$")
_PYTHON_VERSION = re.compile(r"^3\.\d{1,2}$")

REQUIRED_STRESS_MULTIPLIERS = (1.0, 1.25, 1.5, 2.0)
SUPPORTED_SOURCE_KINDS = frozenset({"price_csv"})
SUPPORTED_AVAILABILITY_RULES = frozenset({"completed_bar_close"})
SUPPORTED_LABEL_TYPES = frozenset({"triple_barrier"})
SUPPORTED_SAME_BAR_POLICIES = frozenset({"stop_first"})
SUPPORTED_GAP_POLICIES = frozenset({"adverse_open"})
SUPPORTED_SPLIT_METHODS = frozenset({"chronological_five_way"})
SUPPORTED_CALIBRATION_METHODS = frozenset({"platt", "isotonic", "beta"})
SUPPORTED_COST_MODEL_VERSIONS = frozenset({"declared_static_v1"})
SUPPORTED_PRIMARY_METRICS = frozenset({"net_expectancy_r"})
SUPPORTED_MULTIPLE_TESTING = frozenset({"holm"})
SUPPORTED_LOCKBOX_POLICIES = frozenset({"single_use"})
SUPPORTED_TARGET_STAGES = frozenset({"validated"})


def _invalid(message: str, **context: Any) -> TypedFailure:
    return TypedFailure(FailureReason.INVALID, message, context=context)


def _incomplete(message: str, **context: Any) -> TypedFailure:
    return TypedFailure(FailureReason.INCOMPLETE, message, context=context)


def _require_mapping(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise _invalid(f"{name} must be a JSON object", observed=type(value).__name__)
    return dict(value)


def _take(mapping: dict[str, Any], name: str, allowed: Sequence[str]) -> dict[str, Any]:
    unknown = sorted(set(mapping) - set(allowed))
    if unknown:
        raise _invalid(f"{name} has unknown keys", unknown_keys=unknown)
    missing = sorted(set(allowed) - set(mapping))
    if missing:
        raise _incomplete(f"{name} is missing required keys", missing_keys=missing)
    return mapping


def _text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise _invalid(f"{name} must be a non-empty string")
    return value.strip()


def _boolean(value: Any, name: str) -> bool:
    if not isinstance(value, bool):
        raise _invalid(f"{name} must be a boolean")
    return value


def _finite_number(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise _invalid(f"{name} must be a number")
    number = float(value)
    if not math.isfinite(number):
        raise _invalid(f"{name} must be finite")
    return number


def _positive_number(value: Any, name: str) -> float:
    number = _finite_number(value, name)
    if number <= 0:
        raise _invalid(f"{name} must be positive", observed=number)
    return number


def _non_negative_number(value: Any, name: str) -> float:
    number = _finite_number(value, name)
    if number < 0:
        raise _invalid(f"{name} must be >= 0", observed=number)
    return number


def _positive_int(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise _invalid(f"{name} must be an integer")
    if value <= 0:
        raise _invalid(f"{name} must be positive", observed=value)
    return value


def _utc_datetime(value: Any, name: str) -> datetime:
    if not isinstance(value, str):
        raise _invalid(f"{name} must be an ISO-8601 string")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise _invalid(f"{name} is not ISO-8601", observed=value) from error
    if parsed.tzinfo is None:
        raise _invalid(f"{name} must be timezone-aware", observed=value)
    return parsed.astimezone(UTC)


def _choice(value: Any, name: str, allowed: frozenset[str]) -> str:
    text = _text(value, name)
    if text not in allowed:
        raise _invalid(f"{name} is unsupported", observed=text, allowed=sorted(allowed))
    return text


@dataclass(frozen=True)
class GitBinding:
    commit: str
    dirty_worktree_allowed: bool


@dataclass(frozen=True)
class EnvironmentBinding:
    python_version: str
    dependency_lock_sha256: str | None
    platform_note: str


@dataclass(frozen=True)
class DataSource:
    source_id: str
    kind: str
    path: str
    raw_sha256: str
    license_note: str


@dataclass(frozen=True)
class DataSection:
    sources: tuple[DataSource, ...]
    symbol: str
    start: datetime
    end: datetime
    timezone: str
    as_of_cutoff: datetime
    bar_interval_minutes: int
    required_quality_level: str
    synthetic: bool


@dataclass(frozen=True)
class FeatureSection:
    definitions: tuple[str, ...]
    availability_rule: str
    version: str


@dataclass(frozen=True)
class LabelSection:
    type: str
    horizon_bars: int
    take_profit_vol_multiple: float
    stop_vol_multiple: float
    volatility_window_bars: int
    same_bar_policy: str
    gap_policy: str


@dataclass(frozen=True)
class SplitSection:
    method: str
    train_fraction: float
    tune_fraction: float
    calibration_fraction: float
    test_fraction: float
    lockbox_fraction: float
    purge_bars: int
    embargo_bars: int
    min_rows_per_partition: int


@dataclass(frozen=True)
class ModelCandidate:
    candidate_id: str
    family: str
    hyperparameters: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ModelSection:
    candidates: tuple[ModelCandidate, ...]
    random_seed: int
    trial_budget: int


@dataclass(frozen=True)
class CalibrationSection:
    method: str


@dataclass(frozen=True)
class CostSection:
    cost_model_version: str
    spread_pips: float
    slippage_pips: float
    pip_size: float
    commission_r_per_trade: float
    financing_r_per_bar: float
    stress_multipliers: tuple[float, ...]


@dataclass(frozen=True)
class SelectionSection:
    primary_metric: str
    minimum_trade_count: int
    minimum_effective_trades: int
    max_regime_concentration: float
    max_month_concentration: float
    multiple_testing_method: str
    bootstrap_block_size: int
    pbo_blocks: int


@dataclass(frozen=True)
class StrategyCard:
    """Hypothesis-driven economics of the candidate family (§strategy card).

    ``exploratory=True`` declares economically unmotivated feature mining;
    such experiments face the same gates but must say so up front.
    """

    strategy_id: str
    economic_mechanism: str
    why_should_edge_exist: str
    who_is_paying_the_edge: str
    expected_horizon: str
    known_failure_regimes: tuple[str, ...]
    capacity_assumption: str
    cost_assumption: str
    exploratory: bool


@dataclass(frozen=True)
class LockboxSection:
    access_policy: str
    access_count_limit: int


@dataclass(frozen=True)
class PromotionSection:
    target_stage: str
    policy_path: str | None


@dataclass(frozen=True)
class ExperimentManifest:
    schema_version: int
    experiment_id: str
    created_at: datetime
    created_by: str
    research_question: str
    economic_hypothesis: str
    invalidation_conditions: tuple[str, ...]
    strategy_card: StrategyCard
    git: GitBinding
    environment: EnvironmentBinding
    data: DataSection
    features: FeatureSection
    labels: LabelSection
    splits: SplitSection
    models: ModelSection
    calibration: CalibrationSection
    costs: CostSection
    selection: SelectionSection
    lockbox: LockboxSection
    promotion: PromotionSection

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "experiment_id": self.experiment_id,
            "created_at": self.created_at.isoformat(),
            "created_by": self.created_by,
            "research_question": self.research_question,
            "economic_hypothesis": self.economic_hypothesis,
            "invalidation_conditions": list(self.invalidation_conditions),
            "strategy_card": {
                "strategy_id": self.strategy_card.strategy_id,
                "economic_mechanism": self.strategy_card.economic_mechanism,
                "why_should_edge_exist": self.strategy_card.why_should_edge_exist,
                "who_is_paying_the_edge": self.strategy_card.who_is_paying_the_edge,
                "expected_horizon": self.strategy_card.expected_horizon,
                "known_failure_regimes": list(self.strategy_card.known_failure_regimes),
                "capacity_assumption": self.strategy_card.capacity_assumption,
                "cost_assumption": self.strategy_card.cost_assumption,
                "exploratory": self.strategy_card.exploratory,
            },
            "git": {
                "commit": self.git.commit,
                "dirty_worktree_allowed": self.git.dirty_worktree_allowed,
            },
            "environment": {
                "python_version": self.environment.python_version,
                "dependency_lock_sha256": self.environment.dependency_lock_sha256,
                "platform_note": self.environment.platform_note,
            },
            "data": {
                "sources": [
                    {
                        "source_id": source.source_id,
                        "kind": source.kind,
                        "path": source.path,
                        "raw_sha256": source.raw_sha256,
                        "license_note": source.license_note,
                    }
                    for source in self.data.sources
                ],
                "symbol": self.data.symbol,
                "start": self.data.start.isoformat(),
                "end": self.data.end.isoformat(),
                "timezone": self.data.timezone,
                "as_of_cutoff": self.data.as_of_cutoff.isoformat(),
                "bar_interval_minutes": self.data.bar_interval_minutes,
                "required_quality_level": self.data.required_quality_level,
                "synthetic": self.data.synthetic,
            },
            "features": {
                "definitions": list(self.features.definitions),
                "availability_rule": self.features.availability_rule,
                "version": self.features.version,
            },
            "labels": {
                "type": self.labels.type,
                "horizon_bars": self.labels.horizon_bars,
                "take_profit_vol_multiple": self.labels.take_profit_vol_multiple,
                "stop_vol_multiple": self.labels.stop_vol_multiple,
                "volatility_window_bars": self.labels.volatility_window_bars,
                "same_bar_policy": self.labels.same_bar_policy,
                "gap_policy": self.labels.gap_policy,
            },
            "splits": {
                "method": self.splits.method,
                "train_fraction": self.splits.train_fraction,
                "tune_fraction": self.splits.tune_fraction,
                "calibration_fraction": self.splits.calibration_fraction,
                "test_fraction": self.splits.test_fraction,
                "lockbox_fraction": self.splits.lockbox_fraction,
                "purge_bars": self.splits.purge_bars,
                "embargo_bars": self.splits.embargo_bars,
                "min_rows_per_partition": self.splits.min_rows_per_partition,
            },
            "models": {
                "candidates": [
                    {
                        "candidate_id": candidate.candidate_id,
                        "family": candidate.family,
                        "hyperparameters": dict(candidate.hyperparameters),
                    }
                    for candidate in self.models.candidates
                ],
                "random_seed": self.models.random_seed,
                "trial_budget": self.models.trial_budget,
            },
            "calibration": {"method": self.calibration.method},
            "costs": {
                "cost_model_version": self.costs.cost_model_version,
                "spread_pips": self.costs.spread_pips,
                "slippage_pips": self.costs.slippage_pips,
                "pip_size": self.costs.pip_size,
                "commission_r_per_trade": self.costs.commission_r_per_trade,
                "financing_r_per_bar": self.costs.financing_r_per_bar,
                "stress_multipliers": list(self.costs.stress_multipliers),
            },
            "selection": {
                "primary_metric": self.selection.primary_metric,
                "minimum_trade_count": self.selection.minimum_trade_count,
                "minimum_effective_trades": self.selection.minimum_effective_trades,
                "max_regime_concentration": self.selection.max_regime_concentration,
                "max_month_concentration": self.selection.max_month_concentration,
                "multiple_testing_method": self.selection.multiple_testing_method,
                "bootstrap_block_size": self.selection.bootstrap_block_size,
                "pbo_blocks": self.selection.pbo_blocks,
            },
            "lockbox": {
                "access_policy": self.lockbox.access_policy,
                "access_count_limit": self.lockbox.access_count_limit,
            },
            "promotion": {
                "target_stage": self.promotion.target_stage,
                "policy_path": self.promotion.policy_path,
            },
        }


def canonical_manifest_bytes(manifest: ExperimentManifest) -> bytes:
    return json.dumps(
        manifest.to_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("ascii")


def manifest_sha256(manifest: ExperimentManifest) -> str:
    return hashlib.sha256(canonical_manifest_bytes(manifest)).hexdigest()


def parse_experiment_manifest(payload: Any) -> ExperimentManifest:
    root = _take(
        _require_mapping(payload, "manifest"),
        "manifest",
        (
            "schema_version",
            "experiment_id",
            "created_at",
            "created_by",
            "research_question",
            "economic_hypothesis",
            "invalidation_conditions",
            "strategy_card",
            "git",
            "environment",
            "data",
            "features",
            "labels",
            "splits",
            "models",
            "calibration",
            "costs",
            "selection",
            "lockbox",
            "promotion",
        ),
    )
    schema_version = _positive_int(root["schema_version"], "schema_version")
    if schema_version != MANIFEST_SCHEMA_VERSION:
        raise _invalid(
            "unsupported manifest schema_version",
            observed=schema_version,
            supported=MANIFEST_SCHEMA_VERSION,
        )
    experiment_id = _text(root["experiment_id"], "experiment_id")
    if not _EXPERIMENT_ID.match(experiment_id):
        raise _invalid(
            "experiment_id must match ^[a-z0-9][a-z0-9_-]{2,63}$", observed=experiment_id
        )

    invalidation_raw = root["invalidation_conditions"]
    if not isinstance(invalidation_raw, list) or not invalidation_raw:
        raise _invalid("invalidation_conditions must be a non-empty list")
    invalidation = tuple(
        _text(item, f"invalidation_conditions[{index}]")
        for index, item in enumerate(invalidation_raw)
    )

    git_map = _take(
        _require_mapping(root["git"], "git"), "git", ("commit", "dirty_worktree_allowed")
    )
    commit = _text(git_map["commit"], "git.commit").lower()
    if not _GIT_COMMIT.match(commit):
        raise _invalid("git.commit must be a full commit hash", observed=commit)
    git = GitBinding(
        commit=commit,
        dirty_worktree_allowed=_boolean(
            git_map["dirty_worktree_allowed"], "git.dirty_worktree_allowed"
        ),
    )

    env_map = _take(
        _require_mapping(root["environment"], "environment"),
        "environment",
        ("python_version", "dependency_lock_sha256", "platform_note"),
    )
    python_version = _text(env_map["python_version"], "environment.python_version")
    if not _PYTHON_VERSION.match(python_version):
        raise _invalid("environment.python_version must be like '3.12'", observed=python_version)
    lock_raw = env_map["dependency_lock_sha256"]
    lock_sha: str | None
    if lock_raw is None:
        lock_sha = None
    else:
        lock_sha = _text(lock_raw, "environment.dependency_lock_sha256").lower()
        if not _SHA256.match(lock_sha):
            raise _invalid("environment.dependency_lock_sha256 must be a SHA-256")
    environment = EnvironmentBinding(
        python_version=python_version,
        dependency_lock_sha256=lock_sha,
        platform_note=_text(env_map["platform_note"], "environment.platform_note"),
    )

    data_map = _take(
        _require_mapping(root["data"], "data"),
        "data",
        (
            "sources",
            "symbol",
            "start",
            "end",
            "timezone",
            "as_of_cutoff",
            "bar_interval_minutes",
            "required_quality_level",
            "synthetic",
        ),
    )
    sources_raw = data_map["sources"]
    if not isinstance(sources_raw, list) or not sources_raw:
        raise _invalid("data.sources must be a non-empty list")
    sources: list[DataSource] = []
    seen_source_ids: set[str] = set()
    for index, item in enumerate(sources_raw):
        source_map = _take(
            _require_mapping(item, f"data.sources[{index}]"),
            f"data.sources[{index}]",
            ("source_id", "kind", "path", "raw_sha256", "license_note"),
        )
        source_id = _text(source_map["source_id"], f"data.sources[{index}].source_id")
        if source_id in seen_source_ids:
            raise _invalid("duplicate data source_id", source_id=source_id)
        seen_source_ids.add(source_id)
        raw_sha = _text(source_map["raw_sha256"], f"data.sources[{index}].raw_sha256").lower()
        if not _SHA256.match(raw_sha):
            raise _invalid("data source raw_sha256 must be a SHA-256", source_id=source_id)
        sources.append(
            DataSource(
                source_id=source_id,
                kind=_choice(
                    source_map["kind"], f"data.sources[{index}].kind", SUPPORTED_SOURCE_KINDS
                ),
                path=_text(source_map["path"], f"data.sources[{index}].path"),
                raw_sha256=raw_sha,
                license_note=_text(
                    source_map["license_note"], f"data.sources[{index}].license_note"
                ),
            )
        )
    timezone_name = _text(data_map["timezone"], "data.timezone")
    if timezone_name != "UTC":
        raise _invalid("data.timezone must be 'UTC'", observed=timezone_name)
    start = _utc_datetime(data_map["start"], "data.start")
    end = _utc_datetime(data_map["end"], "data.end")
    if end <= start:
        raise _invalid("data.end must be after data.start")
    as_of_cutoff = _utc_datetime(data_map["as_of_cutoff"], "data.as_of_cutoff")
    if end > as_of_cutoff:
        raise _invalid("data.end cannot exceed data.as_of_cutoff")
    data = DataSection(
        sources=tuple(sources),
        symbol=_text(data_map["symbol"], "data.symbol").upper(),
        start=start,
        end=end,
        timezone=timezone_name,
        as_of_cutoff=as_of_cutoff,
        bar_interval_minutes=_positive_int(
            data_map["bar_interval_minutes"], "data.bar_interval_minutes"
        ),
        required_quality_level=_choice(
            data_map["required_quality_level"],
            "data.required_quality_level",
            frozenset({"strict"}),
        ),
        synthetic=_boolean(data_map["synthetic"], "data.synthetic"),
    )

    features_map = _take(
        _require_mapping(root["features"], "features"),
        "features",
        ("definitions", "availability_rule", "version"),
    )
    definitions_raw = features_map["definitions"]
    if not isinstance(definitions_raw, list) or not definitions_raw:
        raise _invalid("features.definitions must be a non-empty list")
    definitions = tuple(
        _text(item, f"features.definitions[{index}]") for index, item in enumerate(definitions_raw)
    )
    if len(set(definitions)) != len(definitions):
        raise _invalid("features.definitions must be unique")
    features = FeatureSection(
        definitions=definitions,
        availability_rule=_choice(
            features_map["availability_rule"],
            "features.availability_rule",
            SUPPORTED_AVAILABILITY_RULES,
        ),
        version=_text(features_map["version"], "features.version"),
    )

    labels_map = _take(
        _require_mapping(root["labels"], "labels"),
        "labels",
        (
            "type",
            "horizon_bars",
            "take_profit_vol_multiple",
            "stop_vol_multiple",
            "volatility_window_bars",
            "same_bar_policy",
            "gap_policy",
        ),
    )
    labels = LabelSection(
        type=_choice(labels_map["type"], "labels.type", SUPPORTED_LABEL_TYPES),
        horizon_bars=_positive_int(labels_map["horizon_bars"], "labels.horizon_bars"),
        take_profit_vol_multiple=_positive_number(
            labels_map["take_profit_vol_multiple"], "labels.take_profit_vol_multiple"
        ),
        stop_vol_multiple=_positive_number(
            labels_map["stop_vol_multiple"], "labels.stop_vol_multiple"
        ),
        volatility_window_bars=_positive_int(
            labels_map["volatility_window_bars"], "labels.volatility_window_bars"
        ),
        same_bar_policy=_choice(
            labels_map["same_bar_policy"], "labels.same_bar_policy", SUPPORTED_SAME_BAR_POLICIES
        ),
        gap_policy=_choice(labels_map["gap_policy"], "labels.gap_policy", SUPPORTED_GAP_POLICIES),
    )

    splits_map = _take(
        _require_mapping(root["splits"], "splits"),
        "splits",
        (
            "method",
            "train_fraction",
            "tune_fraction",
            "calibration_fraction",
            "test_fraction",
            "lockbox_fraction",
            "purge_bars",
            "embargo_bars",
            "min_rows_per_partition",
        ),
    )
    splits = SplitSection(
        method=_choice(splits_map["method"], "splits.method", SUPPORTED_SPLIT_METHODS),
        train_fraction=_positive_number(splits_map["train_fraction"], "splits.train_fraction"),
        tune_fraction=_positive_number(splits_map["tune_fraction"], "splits.tune_fraction"),
        calibration_fraction=_positive_number(
            splits_map["calibration_fraction"], "splits.calibration_fraction"
        ),
        test_fraction=_positive_number(splits_map["test_fraction"], "splits.test_fraction"),
        lockbox_fraction=_positive_number(
            splits_map["lockbox_fraction"], "splits.lockbox_fraction"
        ),
        purge_bars=_non_negative_int(splits_map["purge_bars"], "splits.purge_bars"),
        embargo_bars=_non_negative_int(splits_map["embargo_bars"], "splits.embargo_bars"),
        min_rows_per_partition=_positive_int(
            splits_map["min_rows_per_partition"], "splits.min_rows_per_partition"
        ),
    )
    fraction_sum = (
        splits.train_fraction
        + splits.tune_fraction
        + splits.calibration_fraction
        + splits.test_fraction
        + splits.lockbox_fraction
    )
    if abs(fraction_sum - 1.0) > 1e-9:
        raise _invalid("splits fractions must sum to 1", observed=fraction_sum)

    models_map = _take(
        _require_mapping(root["models"], "models"),
        "models",
        ("candidates", "random_seed", "trial_budget"),
    )
    candidates_raw = models_map["candidates"]
    if not isinstance(candidates_raw, list) or not candidates_raw:
        raise _invalid("models.candidates must be a non-empty list")
    candidates: list[ModelCandidate] = []
    seen_candidates: set[str] = set()
    for index, item in enumerate(candidates_raw):
        candidate_map = _take(
            _require_mapping(item, f"models.candidates[{index}]"),
            f"models.candidates[{index}]",
            ("candidate_id", "family", "hyperparameters"),
        )
        candidate_id = _text(
            candidate_map["candidate_id"], f"models.candidates[{index}].candidate_id"
        )
        if candidate_id in seen_candidates:
            raise _invalid("duplicate candidate_id", candidate_id=candidate_id)
        seen_candidates.add(candidate_id)
        hyper_map = _require_mapping(
            candidate_map["hyperparameters"], f"models.candidates[{index}].hyperparameters"
        )
        for key, value in hyper_map.items():
            if isinstance(value, bool) or not isinstance(value, int | float | str):
                raise _invalid(
                    "hyperparameters must be numbers or strings",
                    candidate_id=candidate_id,
                    key=str(key),
                )
            if isinstance(value, int | float) and not math.isfinite(float(value)):
                raise _invalid(
                    "hyperparameters must be finite", candidate_id=candidate_id, key=str(key)
                )
        candidates.append(
            ModelCandidate(
                candidate_id=candidate_id,
                family=_text(candidate_map["family"], f"models.candidates[{index}].family"),
                hyperparameters=dict(hyper_map),
            )
        )
    random_seed = models_map["random_seed"]
    if isinstance(random_seed, bool) or not isinstance(random_seed, int) or random_seed < 0:
        raise _invalid("models.random_seed must be a non-negative integer")
    trial_budget = _positive_int(models_map["trial_budget"], "models.trial_budget")
    if len(candidates) > trial_budget:
        raise _invalid(
            "declared candidates exceed trial_budget",
            candidates=len(candidates),
            trial_budget=trial_budget,
        )
    models = ModelSection(
        candidates=tuple(candidates), random_seed=random_seed, trial_budget=trial_budget
    )

    calibration_map = _take(
        _require_mapping(root["calibration"], "calibration"), "calibration", ("method",)
    )
    calibration = CalibrationSection(
        method=_choice(
            calibration_map["method"], "calibration.method", SUPPORTED_CALIBRATION_METHODS
        )
    )

    costs_map = _take(
        _require_mapping(root["costs"], "costs"),
        "costs",
        (
            "cost_model_version",
            "spread_pips",
            "slippage_pips",
            "pip_size",
            "commission_r_per_trade",
            "financing_r_per_bar",
            "stress_multipliers",
        ),
    )
    multipliers_raw = costs_map["stress_multipliers"]
    if not isinstance(multipliers_raw, list) or not multipliers_raw:
        raise _invalid("costs.stress_multipliers must be a non-empty list")
    multipliers = tuple(
        _positive_number(item, f"costs.stress_multipliers[{index}]")
        for index, item in enumerate(multipliers_raw)
    )
    if len(set(multipliers)) != len(multipliers):
        raise _invalid("costs.stress_multipliers must be unique")
    missing_required = [
        value for value in REQUIRED_STRESS_MULTIPLIERS if value not in set(multipliers)
    ]
    if missing_required:
        raise _incomplete(
            "costs.stress_multipliers must include the required multipliers",
            missing=missing_required,
        )
    costs = CostSection(
        cost_model_version=_choice(
            costs_map["cost_model_version"],
            "costs.cost_model_version",
            SUPPORTED_COST_MODEL_VERSIONS,
        ),
        spread_pips=_positive_number(costs_map["spread_pips"], "costs.spread_pips"),
        slippage_pips=_non_negative_number(costs_map["slippage_pips"], "costs.slippage_pips"),
        pip_size=_positive_number(costs_map["pip_size"], "costs.pip_size"),
        commission_r_per_trade=_non_negative_number(
            costs_map["commission_r_per_trade"], "costs.commission_r_per_trade"
        ),
        financing_r_per_bar=_non_negative_number(
            costs_map["financing_r_per_bar"], "costs.financing_r_per_bar"
        ),
        stress_multipliers=tuple(sorted(multipliers)),
    )

    card_map = _take(
        _require_mapping(root["strategy_card"], "strategy_card"),
        "strategy_card",
        (
            "strategy_id",
            "economic_mechanism",
            "why_should_edge_exist",
            "who_is_paying_the_edge",
            "expected_horizon",
            "known_failure_regimes",
            "capacity_assumption",
            "cost_assumption",
            "exploratory",
        ),
    )
    failure_regimes_raw = card_map["known_failure_regimes"]
    if not isinstance(failure_regimes_raw, list) or not failure_regimes_raw:
        raise _invalid("strategy_card.known_failure_regimes must be a non-empty list")
    strategy_card = StrategyCard(
        strategy_id=_text(card_map["strategy_id"], "strategy_card.strategy_id"),
        economic_mechanism=_text(
            card_map["economic_mechanism"], "strategy_card.economic_mechanism"
        ),
        why_should_edge_exist=_text(
            card_map["why_should_edge_exist"], "strategy_card.why_should_edge_exist"
        ),
        who_is_paying_the_edge=_text(
            card_map["who_is_paying_the_edge"], "strategy_card.who_is_paying_the_edge"
        ),
        expected_horizon=_text(card_map["expected_horizon"], "strategy_card.expected_horizon"),
        known_failure_regimes=tuple(
            _text(item, f"strategy_card.known_failure_regimes[{index}]")
            for index, item in enumerate(failure_regimes_raw)
        ),
        capacity_assumption=_text(
            card_map["capacity_assumption"], "strategy_card.capacity_assumption"
        ),
        cost_assumption=_text(card_map["cost_assumption"], "strategy_card.cost_assumption"),
        exploratory=_boolean(card_map["exploratory"], "strategy_card.exploratory"),
    )

    selection_map = _take(
        _require_mapping(root["selection"], "selection"),
        "selection",
        (
            "primary_metric",
            "minimum_trade_count",
            "minimum_effective_trades",
            "max_regime_concentration",
            "max_month_concentration",
            "multiple_testing_method",
            "bootstrap_block_size",
            "pbo_blocks",
        ),
    )
    selection = SelectionSection(
        primary_metric=_choice(
            selection_map["primary_metric"], "selection.primary_metric", SUPPORTED_PRIMARY_METRICS
        ),
        minimum_trade_count=_positive_int(
            selection_map["minimum_trade_count"], "selection.minimum_trade_count"
        ),
        minimum_effective_trades=_positive_int(
            selection_map["minimum_effective_trades"], "selection.minimum_effective_trades"
        ),
        max_regime_concentration=_positive_number(
            selection_map["max_regime_concentration"], "selection.max_regime_concentration"
        ),
        max_month_concentration=_positive_number(
            selection_map["max_month_concentration"], "selection.max_month_concentration"
        ),
        multiple_testing_method=_choice(
            selection_map["multiple_testing_method"],
            "selection.multiple_testing_method",
            SUPPORTED_MULTIPLE_TESTING,
        ),
        bootstrap_block_size=_positive_int(
            selection_map["bootstrap_block_size"], "selection.bootstrap_block_size"
        ),
        pbo_blocks=_positive_int(selection_map["pbo_blocks"], "selection.pbo_blocks"),
    )
    if selection.pbo_blocks < 4 or selection.pbo_blocks % 2 != 0:
        raise _invalid("selection.pbo_blocks must be an even number >= 4")
    if not 0.0 < selection.max_regime_concentration <= 1.0:
        raise _invalid("selection.max_regime_concentration must be inside (0, 1]")
    if not 0.0 < selection.max_month_concentration <= 1.0:
        raise _invalid("selection.max_month_concentration must be inside (0, 1]")
    if selection.minimum_effective_trades > selection.minimum_trade_count:
        raise _invalid(
            "selection.minimum_effective_trades cannot exceed minimum_trade_count",
            minimum_trade_count=selection.minimum_trade_count,
            minimum_effective_trades=selection.minimum_effective_trades,
        )

    lockbox_map = _take(
        _require_mapping(root["lockbox"], "lockbox"),
        "lockbox",
        ("access_policy", "access_count_limit"),
    )
    lockbox = LockboxSection(
        access_policy=_choice(
            lockbox_map["access_policy"], "lockbox.access_policy", SUPPORTED_LOCKBOX_POLICIES
        ),
        access_count_limit=_positive_int(
            lockbox_map["access_count_limit"], "lockbox.access_count_limit"
        ),
    )
    if lockbox.access_count_limit != 1:
        raise _invalid("lockbox.access_count_limit must be exactly 1 for single_use policy")

    promotion_map = _take(
        _require_mapping(root["promotion"], "promotion"),
        "promotion",
        ("target_stage", "policy_path"),
    )
    policy_path_raw = promotion_map["policy_path"]
    promotion = PromotionSection(
        target_stage=_choice(
            promotion_map["target_stage"], "promotion.target_stage", SUPPORTED_TARGET_STAGES
        ),
        policy_path=(
            None if policy_path_raw is None else _text(policy_path_raw, "promotion.policy_path")
        ),
    )

    return ExperimentManifest(
        schema_version=schema_version,
        experiment_id=experiment_id,
        created_at=_utc_datetime(root["created_at"], "created_at"),
        created_by=_text(root["created_by"], "created_by"),
        research_question=_text(root["research_question"], "research_question"),
        economic_hypothesis=_text(root["economic_hypothesis"], "economic_hypothesis"),
        invalidation_conditions=invalidation,
        strategy_card=strategy_card,
        git=git,
        environment=environment,
        data=data,
        features=features,
        labels=labels,
        splits=splits,
        models=models,
        calibration=calibration,
        costs=costs,
        selection=selection,
        lockbox=lockbox,
        promotion=promotion,
    )


def load_experiment_manifest(path: str | Path) -> ExperimentManifest:
    manifest_path = Path(path)
    if not manifest_path.is_file():
        raise TypedFailure(
            FailureReason.UNAVAILABLE,
            "experiment manifest file does not exist",
            context={"path": str(manifest_path)},
        )
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise _invalid("experiment manifest is not valid JSON", path=str(manifest_path)) from error
    return parse_experiment_manifest(payload)


def _non_negative_int(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise _invalid(f"{name} must be an integer")
    if value < 0:
        raise _invalid(f"{name} must be >= 0", observed=value)
    return value
