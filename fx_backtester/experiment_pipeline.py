"""Authoritative experiment pipeline owning raw data through the evidence bundle.

One manifest drives one deterministic run:

    raw sources -> PIT/quality checks -> as-of features -> triple-barrier labels
    -> five-way chronological split -> trials -> selection -> calibration
    -> single test evaluation -> cost stress -> promotion decision -> bundle

The lockbox partition's outcomes are never evaluated or persisted by ``run``;
only a positional commitment is recorded. Evaluating the lockbox is a separate,
governed step. Every stage output is hashed into one lineage chain, and any
unknown, missing or contradictory input stops the run with a ``TypedFailure``
instead of degrading into defaults. Promotion is decided by
``fx_backtester.governance``; with synthetic data or absent evidence the only
correct outcome is a denial, and this module makes no attempt to avoid it.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import subprocess
import sys
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import numpy as np
import pandas as pd

from fx_backtester.calibration import (
    CalibrationError,
    CalibrationMethod,
    ProbabilityCalibrator,
    calibration_metrics,
    fit_calibrator,
)
from fx_backtester.data import load_price_csv
from fx_backtester.experiment_manifest import (
    ExperimentManifest,
    ModelCandidate,
    load_experiment_manifest,
    manifest_sha256,
    parse_experiment_manifest,
)
from fx_backtester.failures import FailureReason, TypedFailure
from fx_backtester.lockbox import LockboxRegistry, LockboxState, verify_lockbox_file
from fx_backtester.promotion_policy import load_promotion_policy
from fx_backtester.governance import (
    PromotionEvidence,
    PromotionPolicy,
    PromotionReport,
    evaluate_promotion,
)
from fx_backtester.labeling import TripleBarrierConfig, TripleBarrierLabel, triple_barrier_label
from fx_backtester.overfitting import (
    deflated_sharpe_ratio,
    per_period_sharpe,
    probability_of_backtest_overfitting,
)
from fx_backtester.point_in_time import evaluate_price_quality
from fx_backtester.statistical_validation import (
    adjust_p_values,
    block_sign_permutation_test,
    circular_block_bootstrap_mean_ci,
    probabilistic_sharpe_ratio,
)
from fx_backtester.time_series_validation import (
    ModelPartitionConfig,
    ModelPartitions,
    TemporalLeakageError,
    chronological_model_partitions,
)
from fx_backtester.trial_ledger import TrialLedger, TrialLedgerEntry
from fx_intel.gbm import GradientBoostingClassifier

PIPELINE_VERSION = "experiment_pipeline_v1"
FEATURE_LEAKAGE_SAMPLES = 7

DETERMINISTIC_ARTIFACTS = (
    "manifest.json",
    "data_lineage.json",
    "dataset_rows.jsonl",
    "lockbox.json",
    "evaluation.json",
    "cost_stress.json",
    "promotion_decision.json",
)
OPERATIONAL_ARTIFACTS = (
    "environment.json",
    "git.json",
    "trial_ledger_snapshot.jsonl",
    "run_info.json",
)


@dataclass(frozen=True)
class GitState:
    commit: str
    dirty: bool


@dataclass(frozen=True)
class ExperimentRunResult:
    experiment_id: str
    output_dir: Path
    manifest_sha256: str
    deterministic_result_sha256: str
    evidence_bundle_sha256: str
    selected_candidate_id: str
    promotion_passed: bool
    promotion_failures: tuple[str, ...]
    summary: dict[str, Any]


# ---------------------------------------------------------------------------
# provenance
# ---------------------------------------------------------------------------


def collect_git_state(repository_root: Path) -> GitState:
    try:
        commit = _git_output(repository_root, "rev-parse", "HEAD")
        status = _git_output(repository_root, "status", "--porcelain")
    except (OSError, subprocess.CalledProcessError) as error:
        raise TypedFailure(
            FailureReason.UNAVAILABLE,
            "git provenance could not be collected",
            context={"repository_root": str(repository_root), "error": str(error)},
        ) from error
    return GitState(commit=commit.lower(), dirty=bool(status.strip()))


def _git_output(repository_root: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", *arguments],
        cwd=repository_root,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _verify_git_binding(manifest: ExperimentManifest, git_state: GitState) -> None:
    if git_state.commit != manifest.git.commit:
        raise TypedFailure(
            FailureReason.INVALID,
            "worktree commit does not match the manifest git binding",
            context={"manifest_commit": manifest.git.commit, "observed": git_state.commit},
        )
    if git_state.dirty and not manifest.git.dirty_worktree_allowed:
        raise TypedFailure(
            FailureReason.INVALID,
            "dirty worktree is not allowed for a formal experiment claim",
            context={"commit": git_state.commit},
        )


def _verify_environment(manifest: ExperimentManifest, repository_root: Path) -> dict[str, Any]:
    running = f"{sys.version_info.major}.{sys.version_info.minor}"
    if running != manifest.environment.python_version:
        raise TypedFailure(
            FailureReason.INVALID,
            "running Python version does not match the manifest",
            context={"manifest": manifest.environment.python_version, "observed": running},
        )
    lock_path = repository_root / "requirements.lock"
    observed_lock: str | None = None
    if lock_path.is_file():
        observed_lock = hashlib.sha256(lock_path.read_bytes()).hexdigest()
    declared_lock = manifest.environment.dependency_lock_sha256
    if declared_lock is not None:
        if observed_lock is None:
            raise TypedFailure(
                FailureReason.UNAVAILABLE,
                "manifest pins a dependency lock but requirements.lock is missing",
                context={"expected_sha256": declared_lock},
            )
        if observed_lock != declared_lock:
            raise TypedFailure(
                FailureReason.HASH_MISMATCH,
                "requirements.lock does not match the manifest dependency lock hash",
                context={"expected": declared_lock, "observed": observed_lock},
            )
    return {
        "python_version": running,
        "python_full": sys.version,
        "platform": platform.platform(),
        "dependency_lock_sha256_observed": observed_lock,
        "dependency_lock_sha256_declared": declared_lock,
        "dependency_lock_pinned": declared_lock is not None,
    }


# ---------------------------------------------------------------------------
# data
# ---------------------------------------------------------------------------


def _load_symbol_frame(manifest: ExperimentManifest, repository_root: Path) -> pd.DataFrame:
    frames: list[pd.DataFrame] = []
    for source in manifest.data.sources:
        source_path = Path(source.path)
        if not source_path.is_absolute():
            source_path = repository_root / source_path
        if not source_path.is_file():
            raise TypedFailure(
                FailureReason.UNAVAILABLE,
                "declared raw data source does not exist",
                context={"source_id": source.source_id, "path": str(source_path)},
            )
        observed = hashlib.sha256(source_path.read_bytes()).hexdigest()
        if observed != source.raw_sha256:
            raise TypedFailure(
                FailureReason.HASH_MISMATCH,
                "raw source bytes do not match the manifest hash",
                context={
                    "source_id": source.source_id,
                    "expected": source.raw_sha256,
                    "observed": observed,
                },
            )
        loaded = load_price_csv(source_path, timezone="UTC")
        if manifest.data.symbol not in loaded:
            raise TypedFailure(
                FailureReason.INCOMPLETE,
                "declared symbol is absent from the raw source",
                context={
                    "source_id": source.source_id,
                    "symbol": manifest.data.symbol,
                    "available": sorted(loaded),
                },
            )
        frames.append(loaded[manifest.data.symbol])
    combined = pd.concat(frames).sort_index()
    if combined.index.has_duplicates:
        raise TypedFailure(
            FailureReason.INVALID,
            "combined raw sources contain duplicate timestamps",
            context={"symbol": manifest.data.symbol},
        )
    window = combined[
        (combined.index >= manifest.data.start) & (combined.index <= manifest.data.end)
    ].copy()
    if window.empty:
        raise TypedFailure(
            FailureReason.INCOMPLETE,
            "no rows remain inside the declared data window",
            context={
                "start": manifest.data.start.isoformat(),
                "end": manifest.data.end.isoformat(),
            },
        )
    interval = timedelta(minutes=manifest.data.bar_interval_minutes)
    diffs = window.index.to_series().diff().dropna()
    offending = diffs[(diffs <= timedelta(0)) | ((diffs % interval) != timedelta(0))]
    if not offending.empty:
        raise TypedFailure(
            FailureReason.INVALID,
            "bar spacing is not a positive multiple of the declared interval",
            context={
                "bar_interval_minutes": manifest.data.bar_interval_minutes,
                "first_offender": offending.index[0].isoformat(),
            },
        )
    last_completion = window.index.max() + interval
    if last_completion > manifest.data.as_of_cutoff:
        raise TypedFailure(
            FailureReason.INVALID,
            "final bar completes after the declared as-of cutoff",
            context={
                "last_bar_completion": last_completion.isoformat(),
                "as_of_cutoff": manifest.data.as_of_cutoff.isoformat(),
            },
        )
    return window


def _check_data_quality(manifest: ExperimentManifest, frame: pd.DataFrame) -> dict[str, Any]:
    report = evaluate_price_quality(frame, now=manifest.data.as_of_cutoff)
    if report.critical_flags:
        raise TypedFailure(
            FailureReason.INVALID,
            "price data failed strict quality checks",
            context={"critical_flags": list(report.critical_flags)},
        )
    return report.to_dict()


def _normalized_dataset_hash(frame: pd.DataFrame) -> str:
    records = [
        {
            "timestamp": timestamp.isoformat(),
            "open": float(row["open"]),
            "high": float(row["high"]),
            "low": float(row["low"]),
            "close": float(row["close"]),
        }
        for timestamp, row in frame.iterrows()
    ]
    return _canonical_sha256(records)


# ---------------------------------------------------------------------------
# features
# ---------------------------------------------------------------------------


def _feature_ret(frame: pd.DataFrame, bars: int) -> pd.Series:
    return frame["close"].pct_change(periods=bars)


def _feature_ma_ratio(frame: pd.DataFrame, window: int) -> pd.Series:
    mean = frame["close"].rolling(window, min_periods=window).mean()
    return frame["close"] / mean - 1.0


def _feature_rsi(frame: pd.DataFrame, window: int) -> pd.Series:
    delta = frame["close"].diff()
    gains = delta.clip(lower=0.0).rolling(window, min_periods=window).mean()
    losses = (-delta.clip(upper=0.0)).rolling(window, min_periods=window).mean()
    denominator = gains + losses
    rsi = pd.Series(np.where(denominator > 0, 100.0 * gains / denominator, 50.0), frame.index)
    rsi[denominator.isna()] = np.nan
    return rsi


def _feature_vol(frame: pd.DataFrame, window: int) -> pd.Series:
    return frame["close"].pct_change().rolling(window, min_periods=window).std(ddof=1)


def _feature_range_position(frame: pd.DataFrame, window: int) -> pd.Series:
    lowest = frame["low"].rolling(window, min_periods=window).min()
    highest = frame["high"].rolling(window, min_periods=window).max()
    span = highest - lowest
    position = pd.Series(np.where(span > 0, (frame["close"] - lowest) / span, 0.5), frame.index)
    position[span.isna()] = np.nan
    return position


FEATURE_REGISTRY: dict[str, Any] = {
    "ret_1": lambda frame: _feature_ret(frame, 1),
    "ret_4": lambda frame: _feature_ret(frame, 4),
    "ret_24": lambda frame: _feature_ret(frame, 24),
    "ma_ratio_24": lambda frame: _feature_ma_ratio(frame, 24),
    "rsi_14": lambda frame: _feature_rsi(frame, 14),
    "vol_24": lambda frame: _feature_vol(frame, 24),
    "range_pos_24": lambda frame: _feature_range_position(frame, 24),
}


def _compute_features(manifest: ExperimentManifest, frame: pd.DataFrame) -> pd.DataFrame:
    unknown = [name for name in manifest.features.definitions if name not in FEATURE_REGISTRY]
    if unknown:
        raise TypedFailure(
            FailureReason.INVALID,
            "manifest declares unregistered feature definitions",
            context={"unknown": unknown, "registered": sorted(FEATURE_REGISTRY)},
        )
    columns = {name: FEATURE_REGISTRY[name](frame) for name in manifest.features.definitions}
    return pd.DataFrame(columns, index=frame.index)


def _assert_features_causal(
    manifest: ExperimentManifest, frame: pd.DataFrame, features: pd.DataFrame
) -> dict[str, Any]:
    """Recompute features on truncated history and require exact agreement."""

    total = len(frame)
    step = max(1, total // FEATURE_LEAKAGE_SAMPLES)
    checked: list[int] = sorted({*range(step, total, step), total - 1})
    for position in checked:
        truncated = _compute_features(manifest, frame.iloc[: position + 1])
        full_row = features.iloc[position].to_numpy(dtype=float)
        truncated_row = truncated.iloc[-1].to_numpy(dtype=float)
        both_nan = np.isnan(full_row) & np.isnan(truncated_row)
        agree = both_nan | np.isclose(full_row, truncated_row, rtol=0.0, atol=1e-12)
        if not bool(agree.all()):
            names = [
                name for name, ok in zip(manifest.features.definitions, agree.tolist()) if not ok
            ]
            raise TypedFailure(
                FailureReason.DATA_LEAKAGE_DETECTED,
                "feature values change when future bars are removed",
                context={"position": position, "features": names},
            )
    return {
        "method": "truncated_history_reconstruction",
        "positions_checked": checked,
        "violations": 0,
    }


# ---------------------------------------------------------------------------
# labels
# ---------------------------------------------------------------------------


def _label_volatility(frame: pd.DataFrame, window: int) -> pd.Series:
    return frame["close"].diff().rolling(window, min_periods=window).std(ddof=1)


def _resolved(label: TripleBarrierLabel) -> bool:
    # ``ambiguous_intrabar`` stays True under stop_first, which resolves the
    # same-bar touch conservatively; only genuinely open outcomes are excluded.
    return (
        label.label_end_time is not None
        and label.gross_r is not None
        and label.exit_price is not None
    )


def build_dataset_rows(
    manifest: ExperimentManifest, frame: pd.DataFrame
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Build one row per usable prediction with long/short gross outcomes."""

    features = _compute_features(manifest, frame)
    leakage_check = _assert_features_causal(manifest, frame, features)
    volatility = _label_volatility(frame, manifest.labels.volatility_window_bars)
    config = TripleBarrierConfig(
        take_profit_vol_multiple=manifest.labels.take_profit_vol_multiple,
        stop_vol_multiple=manifest.labels.stop_vol_multiple,
        entry_lag_bars=1,
        same_bar_policy="stop_first",
        cost_r=0.0,
    )
    rows: list[dict[str, Any]] = []
    excluded = {"missing_feature": 0, "missing_volatility": 0, "unresolved_label": 0}
    for position in range(len(frame)):
        feature_row = features.iloc[position]
        if not bool(np.isfinite(feature_row.to_numpy(dtype=float)).all()):
            excluded["missing_feature"] += 1
            continue
        vol = float(volatility.iloc[position]) if pd.notna(volatility.iloc[position]) else None
        if vol is None or not np.isfinite(vol) or vol <= 0:
            excluded["missing_volatility"] += 1
            continue
        long_label = triple_barrier_label(
            frame,
            prediction_position=position,
            direction=1,
            volatility=vol,
            max_horizon_bars=manifest.labels.horizon_bars,
            horizon=f"{manifest.labels.horizon_bars}bar",
            config=config,
        )
        short_label = triple_barrier_label(
            frame,
            prediction_position=position,
            direction=-1,
            volatility=vol,
            max_horizon_bars=manifest.labels.horizon_bars,
            horizon=f"{manifest.labels.horizon_bars}bar",
            config=config,
        )
        if not (_resolved(long_label) and _resolved(short_label)):
            excluded["unresolved_label"] += 1
            continue
        label_up = long_label.label_up
        if label_up is None:
            excluded["unresolved_label"] += 1
            continue
        label_end = max(
            pd.Timestamp(long_label.label_end_time), pd.Timestamp(short_label.label_end_time)
        )
        rows.append(
            {
                "symbol": manifest.data.symbol,
                "position": position,
                "prediction_time": frame.index[position].isoformat(),
                "label_end_time": label_end.isoformat(),
                "features": {
                    name: float(feature_row[name]) for name in manifest.features.definitions
                },
                "volatility": vol,
                "label_up": int(label_up),
                "long_gross_r": float(long_label.gross_r or 0.0),
                "long_bars_to_exit": int(long_label.bars_to_exit),
                "long_first_touch": long_label.first_touch,
                "short_gross_r": float(short_label.gross_r or 0.0),
                "short_bars_to_exit": int(short_label.bars_to_exit),
                "short_first_touch": short_label.first_touch,
            }
        )
    if not rows:
        raise TypedFailure(
            FailureReason.INSUFFICIENT_SAMPLE,
            "no usable labeled rows were produced",
            context={"excluded": excluded},
        )
    summary = {
        "rows": len(rows),
        "excluded": excluded,
        "feature_leakage_check": leakage_check,
        "volatility_definition": (
            f"rolling std of close differences over {manifest.labels.volatility_window_bars} bars"
        ),
    }
    return rows, summary


def _cost_r(
    manifest: ExperimentManifest, volatility: float, bars_to_exit: int, multiplier: float
) -> float:
    costs = manifest.costs
    risk_distance = volatility * manifest.labels.stop_vol_multiple
    spread_slippage_r = ((costs.spread_pips + costs.slippage_pips) * costs.pip_size) / risk_distance
    financing_r = costs.financing_r_per_bar * bars_to_exit
    return (spread_slippage_r + costs.commission_r_per_trade + financing_r) * multiplier


def trade_net_r(
    manifest: ExperimentManifest, row: Mapping[str, Any], side: str, multiplier: float = 1.0
) -> float:
    if side == "long":
        gross = float(row["long_gross_r"])
        bars = int(row["long_bars_to_exit"])
    elif side == "short":
        gross = float(row["short_gross_r"])
        bars = int(row["short_bars_to_exit"])
    else:
        raise ValueError(f"unknown trade side: {side}")
    return gross - _cost_r(manifest, float(row["volatility"]), bars, multiplier)


# ---------------------------------------------------------------------------
# models
# ---------------------------------------------------------------------------


class _TrainedModel:
    def predict_probability(self, features: np.ndarray) -> np.ndarray:
        raise NotImplementedError

    def to_dict(self) -> dict[str, Any]:
        raise NotImplementedError


class _ConstantProbabilityModel(_TrainedModel):
    def __init__(self, probability: float) -> None:
        self.probability = probability

    def predict_probability(self, features: np.ndarray) -> np.ndarray:
        return np.full(features.shape[0], self.probability, dtype=float)

    def to_dict(self) -> dict[str, Any]:
        return {"family": "constant_probability", "probability": self.probability}


class _LogisticRidgeModel(_TrainedModel):
    def __init__(self, coefficients: np.ndarray, means: np.ndarray, scales: np.ndarray) -> None:
        self.coefficients = coefficients
        self.means = means
        self.scales = scales

    def predict_probability(self, features: np.ndarray) -> np.ndarray:
        standardized = (features - self.means) / self.scales
        design = np.hstack([standardized, np.ones((standardized.shape[0], 1))])
        logits = design @ self.coefficients
        return 1.0 / (1.0 + np.exp(-np.clip(logits, -60.0, 60.0)))

    def to_dict(self) -> dict[str, Any]:
        return {
            "family": "logistic_ridge",
            "coefficients": [float(value) for value in self.coefficients],
            "means": [float(value) for value in self.means],
            "scales": [float(value) for value in self.scales],
        }


def _require_hyper(candidate: ModelCandidate, allowed: set[str]) -> dict[str, Any]:
    supplied = dict(candidate.hyperparameters)
    required = allowed | {"long_threshold", "short_threshold"}
    unknown = sorted(set(supplied) - required)
    if unknown:
        raise TypedFailure(
            FailureReason.INVALID,
            "candidate declares unknown hyperparameters",
            context={"candidate_id": candidate.candidate_id, "unknown": unknown},
        )
    missing = sorted(required - set(supplied))
    if missing:
        raise TypedFailure(
            FailureReason.INCOMPLETE,
            "candidate is missing required hyperparameters",
            context={"candidate_id": candidate.candidate_id, "missing": missing},
        )
    long_threshold = float(supplied["long_threshold"])
    short_threshold = float(supplied["short_threshold"])
    if not 0.5 <= long_threshold < 1.0 or not 0.0 < short_threshold <= 0.5:
        raise TypedFailure(
            FailureReason.INVALID,
            "decision thresholds must satisfy 0 < short <= 0.5 <= long < 1",
            context={"candidate_id": candidate.candidate_id},
        )
    return supplied


class _RowHashRandomModel(_TrainedModel):
    """No-skill random predictor keyed by row content, deterministic per seed."""

    def __init__(self, seed: int) -> None:
        self.seed = seed

    def predict_probability(self, features: np.ndarray) -> np.ndarray:
        probabilities = np.empty(features.shape[0], dtype=float)
        for index in range(features.shape[0]):
            key = json.dumps(
                [self.seed, [float(value) for value in features[index]]],
                separators=(",", ":"),
            ).encode("ascii")
            digest = hashlib.sha256(key).digest()
            probabilities[index] = int.from_bytes(digest[:8], "big") / float(2**64)
        return np.clip(probabilities, 1e-6, 1.0 - 1e-6)

    def to_dict(self) -> dict[str, Any]:
        return {"family": "random_uniform", "seed": self.seed}


class _FeatureRuleModel(_TrainedModel):
    """Simple transparent baselines driven by one declared feature."""

    def __init__(self, family: str, feature_index: int, strength: float) -> None:
        self.family = family
        self.feature_index = feature_index
        self.strength = strength

    def predict_probability(self, features: np.ndarray) -> np.ndarray:
        values = features[:, self.feature_index]
        if self.family == "rsi_reversion":
            signal = np.clip((50.0 - values) / 50.0, -1.0, 1.0)
        else:
            signal = np.sign(values)
        return np.clip(0.5 + self.strength * signal, 1e-6, 1.0 - 1e-6)

    def to_dict(self) -> dict[str, Any]:
        return {
            "family": self.family,
            "feature_index": self.feature_index,
            "strength": self.strength,
        }


class _RidgeRegressionModel(_TrainedModel):
    def __init__(self, coefficients: np.ndarray, means: np.ndarray, scales: np.ndarray) -> None:
        self.coefficients = coefficients
        self.means = means
        self.scales = scales

    def predict_probability(self, features: np.ndarray) -> np.ndarray:
        standardized = (features - self.means) / self.scales
        design = np.hstack([standardized, np.ones((standardized.shape[0], 1))])
        return np.clip(design @ self.coefficients, 1e-6, 1.0 - 1e-6)

    def to_dict(self) -> dict[str, Any]:
        return {
            "family": "ridge_regression",
            "coefficients": [float(value) for value in self.coefficients],
            "means": [float(value) for value in self.means],
            "scales": [float(value) for value in self.scales],
        }


class _GbdtModel(_TrainedModel):
    def __init__(self, classifier: GradientBoostingClassifier) -> None:
        self.classifier = classifier

    def predict_probability(self, features: np.ndarray) -> np.ndarray:
        return np.asarray(self.classifier.predict_proba_many(features.tolist()), dtype=float)

    def to_dict(self) -> dict[str, Any]:
        return {"family": "gbdt", "model": self.classifier.to_dict()}


# baseline: transparent reference strategies; complex: learned candidates that
# are admissible only when they strictly beat the best baseline on tune.
MODEL_FAMILY_KIND: dict[str, str] = {
    "constant_probability": "baseline",
    "random_uniform": "baseline",
    "always_long": "baseline",
    "always_short": "baseline",
    "previous_return_sign": "baseline",
    "ma_crossover": "baseline",
    "rsi_reversion": "baseline",
    "logistic_ridge": "complex",
    "ridge_regression": "complex",
    "gbdt": "complex",
}

_RULE_FAMILY_FEATURE = {
    "previous_return_sign": "ret_1",
    "ma_crossover": "ma_ratio_24",
    "rsi_reversion": "rsi_14",
}
_GBDT_HYPERS = {
    "n_estimators",
    "learning_rate",
    "max_depth",
    "min_samples_leaf",
    "subsample",
    "feature_fraction",
    "reg_lambda",
}


def _feature_index(
    candidate: ModelCandidate, feature_names: Sequence[str], feature_name: str
) -> int:
    if feature_name not in feature_names:
        raise TypedFailure(
            FailureReason.INCOMPLETE,
            "baseline family requires a feature the manifest does not declare",
            context={
                "candidate_id": candidate.candidate_id,
                "family": candidate.family,
                "required_feature": feature_name,
            },
        )
    return list(feature_names).index(feature_name)


def _require_two_classes(candidate: ModelCandidate, train_labels: np.ndarray) -> None:
    if len(np.unique(train_labels)) < 2:
        raise TypedFailure(
            FailureReason.INSUFFICIENT_SAMPLE,
            "training labels are single-class; the fit is unavailable",
            context={"candidate_id": candidate.candidate_id},
        )


def _standardize(train_features: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    means = train_features.mean(axis=0)
    scales = train_features.std(axis=0, ddof=0)
    scales = np.where(scales > 1e-12, scales, 1.0)
    return (train_features - means) / scales, means, scales


def _train_candidate(
    candidate: ModelCandidate,
    feature_names: Sequence[str],
    train_features: np.ndarray,
    train_labels: np.ndarray,
    seed: int,
) -> _TrainedModel:
    family = candidate.family
    if family == "constant_probability":
        supplied = _require_hyper(candidate, {"probability"})
        probability = float(supplied["probability"])
        if not 0.0 < probability < 1.0:
            raise TypedFailure(
                FailureReason.INVALID,
                "constant probability must be inside (0, 1)",
                context={"candidate_id": candidate.candidate_id},
            )
        return _ConstantProbabilityModel(probability)
    if family == "always_long":
        _require_hyper(candidate, set())
        return _ConstantProbabilityModel(1.0 - 1e-6)
    if family == "always_short":
        _require_hyper(candidate, set())
        return _ConstantProbabilityModel(1e-6)
    if family == "random_uniform":
        _require_hyper(candidate, set())
        return _RowHashRandomModel(seed)
    if family in _RULE_FAMILY_FEATURE:
        supplied = _require_hyper(candidate, {"strength"})
        strength = float(supplied["strength"])
        if not 0.0 < strength < 0.5:
            raise TypedFailure(
                FailureReason.INVALID,
                "strength must be inside (0, 0.5)",
                context={"candidate_id": candidate.candidate_id},
            )
        index = _feature_index(candidate, feature_names, _RULE_FAMILY_FEATURE[family])
        return _FeatureRuleModel(family, index, strength)
    if family == "logistic_ridge":
        supplied = _require_hyper(candidate, {"ridge"})
        ridge = float(supplied["ridge"])
        if ridge <= 0 or not np.isfinite(ridge):
            raise TypedFailure(
                FailureReason.INVALID,
                "ridge must be positive and finite",
                context={"candidate_id": candidate.candidate_id},
            )
        _require_two_classes(candidate, train_labels)
        standardized, means, scales = _standardize(train_features)
        design = np.hstack([standardized, np.ones((standardized.shape[0], 1))])
        coefficients = _fit_ridge_logistic(design, train_labels.astype(float), ridge)
        return _LogisticRidgeModel(coefficients, means, scales)
    if family == "ridge_regression":
        supplied = _require_hyper(candidate, {"ridge"})
        ridge = float(supplied["ridge"])
        if ridge <= 0 or not np.isfinite(ridge):
            raise TypedFailure(
                FailureReason.INVALID,
                "ridge must be positive and finite",
                context={"candidate_id": candidate.candidate_id},
            )
        _require_two_classes(candidate, train_labels)
        standardized, means, scales = _standardize(train_features)
        design = np.hstack([standardized, np.ones((standardized.shape[0], 1))])
        penalty = np.full(design.shape[1], ridge, dtype=float)
        penalty[-1] = 0.0
        gram = design.T @ design + np.diag(penalty)
        coefficients = np.linalg.solve(gram, design.T @ train_labels.astype(float))
        if not bool(np.isfinite(coefficients).all()):
            raise TypedFailure(
                FailureReason.INVALID, "ridge regression produced non-finite coefficients"
            )
        return _RidgeRegressionModel(coefficients, means, scales)
    if family == "gbdt":
        supplied = _require_hyper(candidate, _GBDT_HYPERS)
        _require_two_classes(candidate, train_labels)
        try:
            classifier = GradientBoostingClassifier(
                n_estimators=int(supplied["n_estimators"]),
                learning_rate=float(supplied["learning_rate"]),
                max_depth=int(supplied["max_depth"]),
                min_samples_leaf=int(supplied["min_samples_leaf"]),
                subsample=float(supplied["subsample"]),
                feature_fraction=float(supplied["feature_fraction"]),
                reg_lambda=float(supplied["reg_lambda"]),
                seed=seed,
            )
            classifier.fit(train_features.tolist(), train_labels.tolist())
        except ValueError as error:
            raise TypedFailure(
                FailureReason.INVALID,
                "gbdt training rejected its inputs",
                context={"candidate_id": candidate.candidate_id, "error": str(error)},
            ) from error
        return _GbdtModel(classifier)
    raise TypedFailure(
        FailureReason.INVALID,
        "unknown model family",
        context={"candidate_id": candidate.candidate_id, "family": family},
    )


def _fit_ridge_logistic(design: np.ndarray, labels: np.ndarray, ridge: float) -> np.ndarray:
    coefficients = np.zeros(design.shape[1], dtype=float)
    penalty = np.full(design.shape[1], ridge, dtype=float)
    penalty[-1] = 0.0
    for _ in range(200):
        logits = np.clip(design @ coefficients, -60.0, 60.0)
        predicted = 1.0 / (1.0 + np.exp(-logits))
        gradient = design.T @ (predicted - labels) + penalty * coefficients
        weights = np.clip(predicted * (1.0 - predicted), 1e-8, None)
        hessian = design.T @ (design * weights[:, None]) + np.diag(penalty)
        try:
            step = np.linalg.solve(hessian, gradient)
        except np.linalg.LinAlgError:
            step = np.linalg.pinv(hessian) @ gradient
        coefficients = coefficients - step
        if float(np.linalg.norm(step)) < 1e-10:
            break
    if not bool(np.isfinite(coefficients).all()):
        raise TypedFailure(
            FailureReason.INVALID,
            "logistic optimization produced non-finite coefficients",
        )
    return coefficients


# ---------------------------------------------------------------------------
# decisions and metrics
# ---------------------------------------------------------------------------


def _decide(probability: float, long_threshold: float, short_threshold: float) -> str:
    if probability >= long_threshold:
        return "long"
    if probability <= short_threshold:
        return "short"
    return "abstain"


def _evaluate_decisions(
    manifest: ExperimentManifest,
    rows: Sequence[Mapping[str, Any]],
    probabilities: np.ndarray,
    long_threshold: float,
    short_threshold: float,
    *,
    cost_multiplier: float = 1.0,
) -> dict[str, Any]:
    trades: list[dict[str, Any]] = []
    for row, probability in zip(rows, probabilities):
        side = _decide(float(probability), long_threshold, short_threshold)
        if side == "abstain":
            continue
        trades.append(
            {
                "prediction_time": row["prediction_time"],
                "side": side,
                "probability": float(probability),
                "net_r": trade_net_r(manifest, row, side, cost_multiplier),
                "bars_to_exit": int(row[f"{side}_bars_to_exit"]),
            }
        )
    returns = np.array([trade["net_r"] for trade in trades], dtype=float)
    total = len(rows)
    metrics: dict[str, Any] = {
        "rows": total,
        "trade_count": len(trades),
        "coverage": (len(trades) / total) if total else 0.0,
        "abstention_rate": (1.0 - len(trades) / total) if total else 1.0,
        "cost_multiplier": cost_multiplier,
    }
    if len(trades) > 0:
        wins = returns > 0
        losses = returns[returns < 0]
        gains = returns[returns > 0]
        metrics.update(
            {
                "net_expectancy_r": float(returns.mean()),
                "median_net_r": float(np.median(returns)),
                "win_rate": float(wins.mean()),
                "profit_factor": (
                    float(gains.sum() / -losses.sum()) if losses.size and gains.size else None
                ),
                "sharpe_per_trade": per_period_sharpe(returns),
                "max_drawdown_r": _max_drawdown_r(returns),
            }
        )
    return {"metrics": metrics, "trades": trades}


def _max_drawdown_r(returns: np.ndarray) -> float:
    equity = np.cumsum(returns)
    peaks = np.maximum.accumulate(np.concatenate([[0.0], equity]))[1:]
    drawdowns = peaks - equity
    return float(drawdowns.max()) if drawdowns.size else 0.0


# ---------------------------------------------------------------------------
# pipeline
# ---------------------------------------------------------------------------


def run_experiment(
    manifest_path: str | Path,
    *,
    output_root: str | Path,
    repository_root: str | Path | None = None,
    git_state: GitState | None = None,
    trial_ledger_path: str | Path | None = None,
    lockbox_registry_dir: str | Path | None = None,
    now: datetime | None = None,
) -> ExperimentRunResult:
    started_at = now or datetime.now(UTC)
    if started_at.tzinfo is None:
        raise TypedFailure(FailureReason.INVALID, "run timestamp must be timezone-aware")
    manifest = load_experiment_manifest(manifest_path)
    repo_root = Path(repository_root) if repository_root else Path(__file__).resolve().parents[1]
    state = git_state or collect_git_state(repo_root)
    _verify_git_binding(manifest, state)
    environment = _verify_environment(manifest, repo_root)
    _require_baseline_candidates(manifest)
    policy = _load_policy(manifest, repo_root)
    ledger = TrialLedger(
        trial_ledger_path
        if trial_ledger_path is not None
        else repo_root / "runs" / "trial_ledger.jsonl"
    )
    registry = LockboxRegistry(
        lockbox_registry_dir
        if lockbox_registry_dir is not None
        else repo_root / "runs" / "lockbox_registry"
    )

    lineage: list[dict[str, str]] = []

    def bind(artifact: str, sha256: str) -> str:
        lineage.append({"artifact": artifact, "sha256": sha256})
        return sha256

    manifest_hash = bind("manifest", manifest_sha256(manifest))
    for source in manifest.data.sources:
        bind(f"raw:{source.source_id}", source.raw_sha256)

    frame = _load_symbol_frame(manifest, repo_root)
    quality = _check_data_quality(manifest, frame)
    normalized_hash = bind("normalized_dataset", _normalized_dataset_hash(frame))

    rows, dataset_summary = build_dataset_rows(manifest, frame)
    feature_hash = bind("feature_dataset", _canonical_sha256([row["features"] for row in rows]))
    label_hash = bind("label_dataset", _canonical_sha256(rows))

    partitions = _split_rows(manifest, rows)
    split_hash = bind("split", _canonical_sha256(partitions.audit()))

    # The lockbox commitment is registered before any trial runs so that a
    # frozen (already opened) experiment cannot be silently re-run or edited.
    lockbox_inputs = [
        {key: value for key, value in rows[index].items() if not _is_outcome_field(key)}
        for index in partitions.withheld_lockbox_positions
    ]
    registry.register(
        experiment_id=manifest.experiment_id,
        manifest_sha256=manifest_hash,
        commitment_sha256=partitions.lockbox_commitment,
        inputs_sha256=_canonical_sha256(lockbox_inputs),
        now=started_at,
    )

    development = {
        name: [rows[index] for index in getattr(partitions, name)]
        for name in ("train", "tune", "calibration", "test")
    }
    lockbox_rows = [rows[index] for index in partitions.withheld_lockbox_positions]

    trials, trial_records, tune_returns_matrix = _run_trials(
        manifest, development["train"], development["tune"]
    )
    lineage_hashes = {
        "manifest_hash": manifest_hash,
        "dataset_hash": normalized_hash,
        "feature_hash": feature_hash,
        "label_hash": label_hash,
        "split_hash": split_hash,
    }
    try:
        selected = _select_trial(manifest, trials)
    except TypedFailure:
        # Aborted selections still persist every attempted trial.
        _persist_trials(ledger, manifest, state, lineage_hashes, trial_records, started_at)
        raise
    for record in trial_records:
        record["selected"] = record["candidate_id"] == selected["candidate_id"]
    _persist_trials(ledger, manifest, state, lineage_hashes, trial_records, started_at)
    trial_count = _ledger_trial_count(ledger, manifest)
    selected_model: _TrainedModel = selected["model"]
    bind("trained_model", _canonical_sha256(selected_model.to_dict()))

    calibrator, calibration_summary = _fit_calibration(
        manifest, selected, development["calibration"]
    )

    test_result, diagnostics = _evaluate_test(
        manifest, selected, calibrator, development, trials, tune_returns_matrix
    )
    bind("test_predictions", _canonical_sha256(test_result["trades"]))

    stress_rows = _run_cost_stress(manifest, selected, calibrator, development["test"])

    evaluation_payload = {
        "pipeline_version": PIPELINE_VERSION,
        "experiment_id": manifest.experiment_id,
        "dataset_summary": dataset_summary,
        "data_quality": quality,
        "partition_audit": partitions.audit(),
        "regimes": diagnostics["regimes"],
        "selected_candidate_id": selected["candidate_id"],
        "selection_rule": {
            "primary_metric": manifest.selection.primary_metric,
            "minimum_trade_count": manifest.selection.minimum_trade_count,
        },
        "calibration": calibration_summary,
        "test": test_result["metrics"],
        "test_trades": test_result["trades"],
        "statistics": diagnostics["statistics"],
        "multiple_testing": diagnostics["multiple_testing"],
        "sample_size_guard": diagnostics["sample_size_guard"],
        "performance_claim": diagnostics["performance_claim"],
        "notes": [
            "Synthetic or unlicensed data cannot support promotion regardless of metrics.",
            "Lockbox outcomes were neither evaluated nor persisted by this run.",
            "point_in_time check scope: bar-envelope and owned feature joins only.",
        ],
    }
    bind("evaluation", _canonical_sha256(evaluation_payload))

    stress_payload = {
        "cost_model_version": manifest.costs.cost_model_version,
        "rows": stress_rows,
    }
    bind("cost_stress", _canonical_sha256(stress_payload))

    lockbox_state = registry.state(manifest.experiment_id)
    evidence = _promotion_evidence(
        manifest,
        state,
        normalized_hash,
        trial_count,
        test_result,
        diagnostics,
        stress_rows,
        lockbox_state,
    )
    report = evaluate_promotion(evidence, target_stage="validated", policy=policy)
    promotion_payload = {
        "target_stage": report.target_stage,
        "passed": report.passed,
        "failures": list(report.failures),
        "report": report.to_dict(),
        "evidence": _json_ready(asdict(evidence)),
        "policy_source": manifest.promotion.policy_path or "governance defaults",
    }
    bind("promotion_decision", _canonical_sha256(promotion_payload))

    output_dir = Path(output_root) / manifest.experiment_id
    result = _write_bundle(
        manifest,
        output_dir,
        started_at=started_at,
        environment=environment,
        git_state=state,
        lineage=lineage,
        rows=rows,
        lockbox_rows=lockbox_rows,
        partitions=partitions,
        trial_records=trial_records,
        evaluation_payload=evaluation_payload,
        stress_payload=stress_payload,
        promotion_payload=promotion_payload,
        manifest_hash=manifest_hash,
        selected_candidate_id=selected["candidate_id"],
        report=report,
    )
    return result


def evaluate_lockbox(
    evidence_dir: str | Path,
    *,
    purpose: str,
    actor: str,
    repository_root: str | Path | None = None,
    git_state: GitState | None = None,
    lockbox_registry_dir: str | Path | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """One-time governed lockbox evaluation for an existing evidence bundle.

    Outcomes are never stored at rest: this function deterministically
    recomputes the dataset, split, selected model, calibrator and test
    predictions from the bundled manifest and verifies each against the
    recorded lineage chain before the single-use access is claimed. Any
    mismatch means the experiment drifted after registration and the lockbox
    stays closed. Verification replays add no search breadth, so they are not
    appended to the trial ledger; the access ledger records this event.
    """

    bundle = Path(evidence_dir)
    repo_root = Path(repository_root) if repository_root else Path(__file__).resolve().parents[1]
    if (bundle / "lockbox_result.json").exists():
        raise TypedFailure(
            FailureReason.LOCKBOX_VIOLATION,
            "this bundle already carries a lockbox result; single_use forbids another",
            context={"evidence_dir": str(bundle)},
        )
    hashes_path = bundle / "artifact_hashes.json"
    if not hashes_path.is_file():
        raise TypedFailure(
            FailureReason.UNAVAILABLE,
            "evidence bundle has no artifact_hashes.json",
            context={"evidence_dir": str(bundle)},
        )
    artifacts = json.loads(hashes_path.read_text(encoding="utf-8"))["artifacts"]
    for name in DETERMINISTIC_ARTIFACTS:
        artifact_path = bundle / name
        if not artifact_path.is_file():
            raise TypedFailure(
                FailureReason.UNAVAILABLE,
                "evidence bundle artifact is missing",
                context={"artifact": name},
            )
        observed = hashlib.sha256(artifact_path.read_bytes()).hexdigest()
        if observed != artifacts.get(name):
            raise TypedFailure(
                FailureReason.HASH_MISMATCH,
                "evidence bundle artifact was modified after the run",
                context={"artifact": name, "expected": artifacts.get(name), "observed": observed},
            )

    manifest = parse_experiment_manifest(
        json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
    )
    manifest_hash = manifest_sha256(manifest)
    state = git_state or collect_git_state(repo_root)
    _verify_git_binding(manifest, state)

    chain_entries = json.loads((bundle / "data_lineage.json").read_text(encoding="utf-8"))["chain"]
    chain = {entry["artifact"]: entry["sha256"] for entry in chain_entries}
    if chain.get("manifest") != manifest_hash:
        raise TypedFailure(
            FailureReason.LINEAGE_BROKEN,
            "bundle manifest does not match the recorded lineage",
            context={"expected": chain.get("manifest"), "observed": manifest_hash},
        )

    frame = _load_symbol_frame(manifest, repo_root)
    _check_data_quality(manifest, frame)
    normalized_hash = _normalized_dataset_hash(frame)
    if normalized_hash != chain.get("normalized_dataset"):
        raise TypedFailure(
            FailureReason.HASH_MISMATCH,
            "normalized dataset no longer matches the recorded lineage",
        )
    rows, _ = build_dataset_rows(manifest, frame)
    if _canonical_sha256(rows) != chain.get("label_dataset"):
        raise TypedFailure(
            FailureReason.LINEAGE_BROKEN,
            "label dataset recomputation does not match the recorded lineage",
        )
    partitions = _split_rows(manifest, rows)
    if _canonical_sha256(partitions.audit()) != chain.get("split"):
        raise TypedFailure(
            FailureReason.LINEAGE_BROKEN,
            "split recomputation does not match the recorded lineage",
        )
    lockbox_payload = verify_lockbox_file(
        bundle / "lockbox.json", expected_sha256=artifacts["lockbox.json"]
    )
    if lockbox_payload.get("commitment_sha256") != partitions.lockbox_commitment:
        raise TypedFailure(
            FailureReason.HASH_MISMATCH,
            "lockbox commitment does not match the recomputed partition",
        )

    development = {
        name: [rows[index] for index in getattr(partitions, name)]
        for name in ("train", "tune", "calibration", "test")
    }
    trials, _, tune_returns_matrix = _run_trials(
        manifest, development["train"], development["tune"]
    )
    selected = _select_trial(manifest, trials)
    evaluation = json.loads((bundle / "evaluation.json").read_text(encoding="utf-8"))
    if selected["candidate_id"] != evaluation.get("selected_candidate_id"):
        raise TypedFailure(
            FailureReason.LINEAGE_BROKEN,
            "deterministic replay selected a different candidate",
            context={
                "replayed": selected["candidate_id"],
                "recorded": evaluation.get("selected_candidate_id"),
            },
        )
    model: _TrainedModel = selected["model"]
    if _canonical_sha256(model.to_dict()) != chain.get("trained_model"):
        raise TypedFailure(
            FailureReason.HASH_MISMATCH,
            "replayed trained model does not match the recorded lineage",
        )
    calibrator, _ = _fit_calibration(manifest, selected, development["calibration"])
    test_features, _ = _matrix(manifest, development["test"])
    test_probabilities = np.asarray(
        calibrator.predict(model.predict_probability(test_features).tolist()), dtype=float
    )
    replayed_test = _evaluate_decisions(
        manifest,
        development["test"],
        test_probabilities,
        selected["long_threshold"],
        selected["short_threshold"],
    )
    if _canonical_sha256(replayed_test["trades"]) != chain.get("test_predictions"):
        raise TypedFailure(
            FailureReason.LINEAGE_BROKEN,
            "replayed test predictions do not match the recorded lineage",
        )

    # The recorded promotion evidence must agree with the replay before the
    # single-use access is consumed; a bundle whose artifact_hashes.json was
    # regenerated around edited numbers fails here instead of being trusted.
    prior = json.loads((bundle / "promotion_decision.json").read_text(encoding="utf-8"))
    replayed_returns = np.array([trade["net_r"] for trade in replayed_test["trades"]], dtype=float)
    replayed_statistics = _headline_statistics(
        manifest, trials, selected, tune_returns_matrix, replayed_returns
    )
    replayed_stress = _run_cost_stress(manifest, selected, calibrator, development["test"])
    _verify_prior_evidence(
        prior,
        manifest,
        state,
        normalized_hash,
        replayed_test,
        replayed_statistics,
        replayed_stress,
    )

    registry = LockboxRegistry(
        lockbox_registry_dir
        if lockbox_registry_dir is not None
        else repo_root / "runs" / "lockbox_registry"
    )
    registry.claim_access(
        experiment_id=manifest.experiment_id,
        manifest_sha256=manifest_hash,
        purpose=purpose,
        actor=actor,
        now=now,
    )

    lockbox_positions = partitions.open_lockbox(selection_complete=True, purpose=purpose)
    lockbox_rows = [rows[index] for index in lockbox_positions]
    lockbox_features, _ = _matrix(manifest, lockbox_rows)
    lockbox_probabilities = np.asarray(
        calibrator.predict(model.predict_probability(lockbox_features).tolist()), dtype=float
    )
    lockbox_result = _evaluate_decisions(
        manifest,
        lockbox_rows,
        lockbox_probabilities,
        selected["long_threshold"],
        selected["short_threshold"],
    )

    evidence = replace(
        PromotionEvidence(**prior["evidence"]),
        lockbox_evaluated_once=registry.state(manifest.experiment_id).evaluated_once,
        lockbox_reused_for_selection=registry.state(manifest.experiment_id).reused_for_selection,
    )
    report = evaluate_promotion(
        evidence, target_stage="validated", policy=_load_policy(manifest, repo_root)
    )

    opened_at = _utc_iso(now)
    result_payload = {
        "experiment_id": manifest.experiment_id,
        "opened_at": opened_at,
        "purpose": purpose,
        "actor": actor,
        "manifest_sha256": manifest_hash,
        "commitment_sha256": partitions.lockbox_commitment,
        "metrics": lockbox_result["metrics"],
        "trades": lockbox_result["trades"],
        "consistency": "manifest, dataset, labels, split, model and test "
        "predictions replayed to identical hashes before access",
    }
    _write_json(bundle / "lockbox_result.json", result_payload)
    post_payload = {
        "target_stage": report.target_stage,
        "passed": report.passed,
        "failures": list(report.failures),
        "report": report.to_dict(),
        "evidence": _json_ready(asdict(evidence)),
        "lockbox_result_sha256": hashlib.sha256(
            (bundle / "lockbox_result.json").read_bytes()
        ).hexdigest(),
    }
    _write_json(bundle / "promotion_decision_post_lockbox.json", post_payload)
    return {
        "experiment_id": manifest.experiment_id,
        "opened_at": opened_at,
        "lockbox_metrics": lockbox_result["metrics"],
        "promotion_passed": report.passed,
        "promotion_failures": list(report.failures),
        "lockbox_result_path": str(bundle / "lockbox_result.json"),
    }


def _verify_prior_evidence(
    prior: Mapping[str, Any],
    manifest: ExperimentManifest,
    git_state: GitState,
    normalized_hash: str,
    replayed_test: Mapping[str, Any],
    replayed_statistics: Mapping[str, Any],
    replayed_stress: Sequence[Mapping[str, Any]],
) -> None:
    """Cross-check recorded promotion evidence against the deterministic replay."""

    evidence = prior.get("evidence")
    if not isinstance(evidence, Mapping):
        raise TypedFailure(
            FailureReason.INCOMPLETE,
            "promotion decision carries no evidence object",
        )
    replayed_returns = np.array([trade["net_r"] for trade in replayed_test["trades"]], dtype=float)
    replayed_expectancy = float(replayed_returns.mean()) if replayed_returns.size else None

    def stat_value(name: str, key: str) -> float | None:
        entry = replayed_statistics[name]
        if not entry["available"]:
            return None
        value = entry["value"].get(key)
        return float(value) if value is not None else None

    stress_2x = next(
        (row["net_expectancy_r"] for row in replayed_stress if row["cost_multiplier"] == 2.0),
        None,
    )
    expectations: list[tuple[str, Any, Any]] = [
        ("dataset_hash", evidence.get("dataset_hash"), normalized_hash),
        ("git_commit", evidence.get("git_commit"), git_state.commit),
        ("synthetic_data", evidence.get("synthetic_data"), manifest.data.synthetic),
        ("net_expectancy_r", evidence.get("net_expectancy_r"), replayed_expectancy),
        ("pair_count", evidence.get("pair_count"), 1),
        (
            "expectancy_ci_lower_r",
            evidence.get("expectancy_ci_lower_r"),
            stat_value("bootstrap_ci", "lower"),
        ),
        (
            "dsr_probability",
            evidence.get("dsr_probability"),
            stat_value("dsr_tune_selection", "dsr"),
        ),
        ("pbo_probability", evidence.get("pbo_probability"), stat_value("pbo", "pbo")),
        (
            "cost_stress_2x_expectancy_r",
            evidence.get("cost_stress_2x_expectancy_r"),
            stress_2x,
        ),
    ]
    mismatched = [
        {"field": name, "recorded": recorded, "replayed": replayed}
        for name, recorded, replayed in expectations
        if recorded != replayed
    ]
    if mismatched:
        raise TypedFailure(
            FailureReason.LINEAGE_BROKEN,
            "recorded promotion evidence disagrees with the deterministic replay",
            context={"mismatched": mismatched},
        )


def _utc_iso(now: datetime | None) -> str:
    value = now or datetime.now(UTC)
    if value.tzinfo is None:
        raise TypedFailure(FailureReason.INVALID, "timestamps must be timezone-aware")
    return value.astimezone(UTC).isoformat()


def _persist_trials(
    ledger: TrialLedger,
    manifest: ExperimentManifest,
    git_state: GitState,
    lineage_hashes: Mapping[str, str],
    trial_records: Sequence[Mapping[str, Any]],
    started_at: datetime,
) -> None:
    """Append every attempted trial — including failures — to the durable ledger."""

    run_nonce = started_at.isoformat()
    for record in trial_records:
        ledger.append(
            TrialLedgerEntry(
                trial_id=f"{record['trial_id']}:{run_nonce}",
                experiment_id=manifest.experiment_id,
                parent_trial_id=record["parent_trial_id"],
                started_at=record["started_at"],
                finished_at=record["finished_at"],
                status=record["status"],
                git_commit=git_state.commit,
                manifest_hash=lineage_hashes["manifest_hash"],
                dataset_hash=lineage_hashes["dataset_hash"],
                feature_hash=lineage_hashes["feature_hash"],
                label_hash=lineage_hashes["label_hash"],
                split_hash=lineage_hashes["split_hash"],
                model_family=record["model_family"],
                hyperparameters=record["hyperparameters"],
                seed=int(record["seed"]),
                metrics=record["metrics"],
                cost_metrics=None,
                failure_reason=record["failure_reason"],
                selected=bool(record["selected"]),
                extra={"candidate_id": record["candidate_id"], "run_started_at": run_nonce},
            )
        )


def _ledger_trial_count(ledger: TrialLedger, manifest: ExperimentManifest) -> int:
    """Search breadth from the tamper-checked ledger; incomplete ledgers fail."""

    ledger.verify()
    entries = ledger.entries_for_experiment(manifest.experiment_id)
    recorded = {str(entry.get("extra", {}).get("candidate_id", "")) for entry in entries}
    declared = {candidate.candidate_id for candidate in manifest.models.candidates}
    missing = sorted(declared - recorded)
    if missing:
        raise TypedFailure(
            FailureReason.INCOMPLETE,
            "trial ledger does not contain every declared candidate; "
            "no performance claim is possible",
            context={"experiment_id": manifest.experiment_id, "missing": missing},
        )
    return len(recorded)


def _split_rows(manifest: ExperimentManifest, rows: Sequence[Mapping[str, Any]]) -> ModelPartitions:
    interval = timedelta(minutes=manifest.data.bar_interval_minutes)
    config = ModelPartitionConfig(
        train_fraction=manifest.splits.train_fraction,
        tune_fraction=manifest.splits.tune_fraction,
        calibration_fraction=manifest.splits.calibration_fraction,
        test_fraction=manifest.splits.test_fraction,
        lockbox_fraction=manifest.splits.lockbox_fraction,
        purge=interval * manifest.splits.purge_bars,
        embargo=interval * manifest.splits.embargo_bars,
        min_rows_per_partition=manifest.splits.min_rows_per_partition,
    )
    prediction_times = [row["prediction_time"] for row in rows]
    label_end_times = [row["label_end_time"] for row in rows]
    try:
        return chronological_model_partitions(prediction_times, label_end_times, config)
    except TemporalLeakageError as error:
        raise TypedFailure(
            FailureReason.DATA_LEAKAGE_DETECTED,
            "five-way chronological split failed leakage checks",
            context={"error": str(error)},
        ) from error


def _matrix(
    manifest: ExperimentManifest, rows: Sequence[Mapping[str, Any]]
) -> tuple[np.ndarray, np.ndarray]:
    features = np.array(
        [[float(row["features"][name]) for name in manifest.features.definitions] for row in rows],
        dtype=float,
    )
    labels = np.array([int(row["label_up"]) for row in rows], dtype=int)
    return features, labels


def _run_trials(
    manifest: ExperimentManifest,
    train_rows: Sequence[Mapping[str, Any]],
    tune_rows: Sequence[Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], pd.DataFrame]:
    train_features, train_labels = _matrix(manifest, train_rows)
    tune_features, _ = _matrix(manifest, tune_rows)
    trials: list[dict[str, Any]] = []
    records: list[dict[str, Any]] = []
    tune_returns: dict[str, pd.Series] = {}
    tune_index = pd.DatetimeIndex([row["prediction_time"] for row in tune_rows])
    for order, candidate in enumerate(manifest.models.candidates):
        record: dict[str, Any] = {
            "trial_id": f"{manifest.experiment_id}:{candidate.candidate_id}",
            "experiment_id": manifest.experiment_id,
            "parent_trial_id": None,
            "candidate_id": candidate.candidate_id,
            "model_family": candidate.family,
            "hyperparameters": _json_ready(dict(candidate.hyperparameters)),
            "seed": manifest.models.random_seed + order,
            "started_at": None,
            "finished_at": None,
            "status": "failed",
            "failure_reason": None,
            "metrics": None,
            "selected": False,
        }
        started = datetime.now(UTC)
        record["started_at"] = started.isoformat()
        try:
            model = _train_candidate(
                candidate,
                manifest.features.definitions,
                train_features,
                train_labels,
                manifest.models.random_seed + order,
            )
            long_threshold = float(candidate.hyperparameters["long_threshold"])
            short_threshold = float(candidate.hyperparameters["short_threshold"])
            probabilities = model.predict_probability(tune_features)
            evaluated = _evaluate_decisions(
                manifest, tune_rows, probabilities, long_threshold, short_threshold
            )
            per_row_returns = _per_row_returns(
                manifest, tune_rows, probabilities, long_threshold, short_threshold
            )
            tune_returns[candidate.candidate_id] = pd.Series(per_row_returns, index=tune_index)
            record["status"] = "succeeded"
            record["metrics"] = _json_ready(evaluated["metrics"])
            trials.append(
                {
                    "candidate_id": candidate.candidate_id,
                    "candidate": candidate,
                    "model": model,
                    "long_threshold": long_threshold,
                    "short_threshold": short_threshold,
                    "tune_metrics": evaluated["metrics"],
                    "tune_returns": per_row_returns,
                }
            )
        except TypedFailure as failure:
            record["failure_reason"] = failure.to_dict()
        finally:
            record["finished_at"] = datetime.now(UTC).isoformat()
            records.append(record)
    matrix = pd.DataFrame(tune_returns, index=tune_index)
    return trials, records, matrix


def _per_row_returns(
    manifest: ExperimentManifest,
    rows: Sequence[Mapping[str, Any]],
    probabilities: np.ndarray,
    long_threshold: float,
    short_threshold: float,
) -> list[float]:
    """Flat position (0R) when abstaining; realized net R when trading."""

    returns: list[float] = []
    for row, probability in zip(rows, probabilities):
        side = _decide(float(probability), long_threshold, short_threshold)
        if side == "abstain":
            returns.append(0.0)
        else:
            returns.append(trade_net_r(manifest, row, side))
    return returns


def _load_policy(manifest: ExperimentManifest, repo_root: Path) -> PromotionPolicy | None:
    if manifest.promotion.policy_path is None:
        return None
    policy_path = Path(manifest.promotion.policy_path)
    if not policy_path.is_absolute():
        policy_path = repo_root / policy_path
    return load_promotion_policy(policy_path)


def _require_baseline_candidates(manifest: ExperimentManifest) -> None:
    if not any(
        MODEL_FAMILY_KIND.get(candidate.family) == "baseline"
        for candidate in manifest.models.candidates
    ):
        raise TypedFailure(
            FailureReason.INVALID,
            "at least one baseline candidate family is required so that complex "
            "models are compared under identical conditions",
            context={
                "baseline_families": sorted(
                    name for name, kind in MODEL_FAMILY_KIND.items() if kind == "baseline"
                )
            },
        )


def _select_trial(manifest: ExperimentManifest, trials: Sequence[dict[str, Any]]) -> dict[str, Any]:
    qualified = [
        trial
        for trial in trials
        if trial["tune_metrics"]["trade_count"] >= manifest.selection.minimum_trade_count
        and trial["tune_metrics"].get("net_expectancy_r") is not None
    ]
    if not qualified:
        raise TypedFailure(
            FailureReason.INSUFFICIENT_SAMPLE,
            "no candidate produced the minimum tune trade count; selection is unavailable",
            context={
                "minimum_trade_count": manifest.selection.minimum_trade_count,
                "candidates": [
                    {
                        "candidate_id": trial["candidate_id"],
                        "trade_count": trial["tune_metrics"]["trade_count"],
                    }
                    for trial in trials
                ],
            },
        )

    def metric(trial: Mapping[str, Any]) -> float:
        return float(trial["tune_metrics"]["net_expectancy_r"])

    def kind(trial: Mapping[str, Any]) -> str:
        return MODEL_FAMILY_KIND.get(trial["candidate"].family, "complex")

    baselines = [trial for trial in qualified if kind(trial) == "baseline"]
    # A flat book earns 0R, so no complex model is admissible below that floor.
    benchmark_floor = max([0.0, *(metric(trial) for trial in baselines)])
    admissible = [
        trial for trial in qualified if kind(trial) == "baseline" or metric(trial) > benchmark_floor
    ]
    if not admissible:
        raise TypedFailure(
            FailureReason.INVALID,
            "no admissible candidate: complex models must strictly beat the best "
            "qualified baseline (or 0R when no baseline qualifies) under identical "
            "conditions",
            context={
                "benchmark_floor_net_expectancy_r": benchmark_floor,
                "qualified": [
                    {
                        "candidate_id": trial["candidate_id"],
                        "family": trial["candidate"].family,
                        "net_expectancy_r": metric(trial),
                    }
                    for trial in qualified
                ],
            },
        )
    return max(admissible, key=lambda trial: (metric(trial), trial["candidate_id"]))


def _fit_calibration(
    manifest: ExperimentManifest,
    selected: dict[str, Any],
    calibration_rows: Sequence[Mapping[str, Any]],
) -> tuple[ProbabilityCalibrator, dict[str, Any]]:
    features, labels = _matrix(manifest, calibration_rows)
    if len(np.unique(labels)) < 2:
        raise TypedFailure(
            FailureReason.INSUFFICIENT_SAMPLE,
            "calibration partition is single-class; calibration is unavailable",
        )
    model: _TrainedModel = selected["model"]
    raw = model.predict_probability(features)
    try:
        method = cast(CalibrationMethod, manifest.calibration.method)
        calibrator = fit_calibrator(method, raw.tolist(), labels.tolist())
        calibrated = np.asarray(calibrator.predict(raw.tolist()), dtype=float)
        raw_metrics = calibration_metrics(labels.tolist(), raw.tolist())
        calibrated_metrics = calibration_metrics(labels.tolist(), calibrated.tolist())
    except CalibrationError as error:
        raise TypedFailure(
            FailureReason.INVALID,
            "probability calibration failed",
            context={"method": manifest.calibration.method, "error": str(error)},
        ) from error
    return calibrator, {
        "method": manifest.calibration.method,
        "rows": len(calibration_rows),
        "raw": raw_metrics.to_dict(),
        "calibrated": calibrated_metrics.to_dict(),
    }


def _regime_bounds(train_rows: Sequence[Mapping[str, Any]]) -> tuple[float, float]:
    values = np.array([float(row["volatility"]) for row in train_rows], dtype=float)
    lower, upper = np.quantile(values, [1.0 / 3.0, 2.0 / 3.0])
    return float(lower), float(upper)


def _regime_of(volatility: float, bounds: tuple[float, float]) -> str:
    if volatility <= bounds[0]:
        return "low_vol"
    if volatility <= bounds[1]:
        return "mid_vol"
    return "high_vol"


def _headline_statistics(
    manifest: ExperimentManifest,
    trials: Sequence[dict[str, Any]],
    selected: Mapping[str, Any],
    tune_returns_matrix: pd.DataFrame,
    trade_returns: np.ndarray,
) -> dict[str, Any]:
    """Bootstrap CI / PSR / DSR / PBO, shared by the run and the lockbox replay."""

    statistics: dict[str, Any] = {}
    statistics["bootstrap_ci"] = _safe_statistic(
        lambda: vars(
            circular_block_bootstrap_mean_ci(
                trade_returns,
                block_size=min(manifest.selection.bootstrap_block_size, max(1, len(trade_returns))),
                seed=manifest.models.random_seed,
            )
        )
    )
    statistics["psr"] = _safe_statistic(lambda: probabilistic_sharpe_ratio(trade_returns))
    trial_sharpes = [
        per_period_sharpe(np.asarray(trial["tune_returns"], dtype=float)) for trial in trials
    ]
    statistics["dsr_tune_selection"] = _safe_statistic(
        lambda: deflated_sharpe_ratio(
            np.asarray(selected["tune_returns"], dtype=float), trial_sharpes
        )
    )
    statistics["pbo"] = _safe_statistic(
        lambda: probability_of_backtest_overfitting(
            tune_returns_matrix, n_blocks=manifest.selection.pbo_blocks
        )
    )
    return statistics


def _evaluate_test(
    manifest: ExperimentManifest,
    selected: dict[str, Any],
    calibrator: ProbabilityCalibrator,
    development: Mapping[str, Sequence[Mapping[str, Any]]],
    trials: Sequence[dict[str, Any]],
    tune_returns_matrix: pd.DataFrame,
) -> tuple[dict[str, Any], dict[str, Any]]:
    test_rows = development["test"]
    features, labels = _matrix(manifest, test_rows)
    model: _TrainedModel = selected["model"]
    raw = model.predict_probability(features)
    calibrated = np.asarray(calibrator.predict(raw.tolist()), dtype=float)
    evaluated = _evaluate_decisions(
        manifest,
        test_rows,
        calibrated,
        selected["long_threshold"],
        selected["short_threshold"],
    )
    try:
        raw_test_metrics = calibration_metrics(labels.tolist(), raw.tolist())
        calibrated_test_metrics = calibration_metrics(labels.tolist(), calibrated.tolist())
    except CalibrationError as error:
        raise TypedFailure(
            FailureReason.INVALID,
            "test probability metrics could not be computed",
            context={"error": str(error)},
        ) from error
    evaluated["metrics"]["brier_raw"] = raw_test_metrics.brier
    evaluated["metrics"]["brier_calibrated"] = calibrated_test_metrics.brier
    evaluated["metrics"]["log_loss_calibrated"] = calibrated_test_metrics.log_loss
    evaluated["metrics"]["brier_improvement"] = (
        raw_test_metrics.brier - calibrated_test_metrics.brier
    )

    trade_returns = np.array([trade["net_r"] for trade in evaluated["trades"]], dtype=float)
    statistics = _headline_statistics(
        manifest, trials, selected, tune_returns_matrix, trade_returns
    )

    p_values: list[float] = []
    p_candidates: list[str] = []
    for trial in trials:
        result = _safe_statistic(
            lambda trial=trial: block_sign_permutation_test(
                np.asarray(trial["tune_returns"], dtype=float),
                block_size=manifest.selection.bootstrap_block_size,
                seed=manifest.models.random_seed,
            )
        )
        if result["available"]:
            p_values.append(float(result["value"]["p_value"]))
            p_candidates.append(trial["candidate_id"])
    multiple_testing: dict[str, Any]
    if p_values:
        adjusted = adjust_p_values(p_values, method="holm")
        multiple_testing = {
            "method": "holm",
            "candidates": p_candidates,
            "raw_p_values": p_values,
            "adjusted_p_values": [float(value) for value in adjusted],
        }
    else:
        multiple_testing = {"method": "holm", "status": "evaluation_unavailable"}

    bounds = _regime_bounds(development["train"])
    trade_regimes: list[str] = []
    for row, trade in zip(test_rows, _test_trade_flags(calibrated, selected)):
        if trade:
            trade_regimes.append(_regime_of(float(row["volatility"]), bounds))
    regime_counts: dict[str, int] = {}
    for regime in trade_regimes:
        regime_counts[regime] = regime_counts.get(regime, 0) + 1
    regimes = {
        "method": "trailing_vol_terciles_train_fit_v1",
        "bounds": {"low_upper": bounds[0], "mid_upper": bounds[1]},
        "test_trade_counts": dict(sorted(regime_counts.items())),
    }

    sample_size_guard = _sample_size_guard(manifest, evaluated["trades"], trade_regimes)
    performance_claim = (
        "evaluation_available_descriptive"
        if sample_size_guard["passed"]
        else "evaluation_unavailable"
    )
    diagnostics = {
        "statistics": statistics,
        "multiple_testing": multiple_testing,
        "regimes": regimes,
        "sample_size_guard": sample_size_guard,
        "performance_claim": performance_claim,
    }
    return evaluated, diagnostics


def _sample_size_guard(
    manifest: ExperimentManifest,
    trades: Sequence[Mapping[str, Any]],
    trade_regimes: Sequence[str],
) -> dict[str, Any]:
    """Refuse to call a result 'evaluated' on thin, overlapping or lopsided samples.

    Effective trades are counted greedily over non-overlapping label windows so
    that stacked positions on the same move cannot masquerade as independent
    evidence. Concentration checks reject samples dominated by one volatility
    regime or one calendar month. Thresholds come from the manifest and are
    internal pre-registered choices, not industry standards.
    """

    selection = manifest.selection
    trade_count = len(trades)
    interval = timedelta(minutes=manifest.data.bar_interval_minutes)
    effective = 0
    last_end: pd.Timestamp | None = None
    for trade in sorted(trades, key=lambda item: str(item["prediction_time"])):
        start = pd.Timestamp(trade["prediction_time"])
        end = start + interval * (1 + int(trade["bars_to_exit"]))
        if last_end is None or start >= last_end:
            effective += 1
            last_end = end

    def share(counts: Mapping[str, int]) -> float | None:
        if trade_count == 0 or not counts:
            return None
        return max(counts.values()) / trade_count

    regime_tally: dict[str, int] = {}
    for regime in trade_regimes:
        regime_tally[regime] = regime_tally.get(regime, 0) + 1
    month_tally: dict[str, int] = {}
    for trade in trades:
        month = str(trade["prediction_time"])[:7]
        month_tally[month] = month_tally.get(month, 0) + 1
    regime_share = share(regime_tally)
    month_share = share(month_tally)
    checks: dict[str, dict[str, Any]] = {
        "trade_count": {
            "observed": trade_count,
            "minimum": selection.minimum_trade_count,
            "passed": trade_count >= selection.minimum_trade_count,
        },
        "effective_trades": {
            "observed": effective,
            "minimum": selection.minimum_effective_trades,
            "passed": effective >= selection.minimum_effective_trades,
        },
        "regime_concentration": {
            "observed": regime_share,
            "maximum": selection.max_regime_concentration,
            "passed": regime_share is not None
            and regime_share <= selection.max_regime_concentration,
        },
        "month_concentration": {
            "observed": month_share,
            "maximum": selection.max_month_concentration,
            "passed": month_share is not None and month_share <= selection.max_month_concentration,
        },
    }
    return {
        "checks": checks,
        "test_trade_count": trade_count,
        "effective_trade_count": effective,
        "passed": all(check["passed"] for check in checks.values()),
    }


def _test_trade_flags(
    probabilities: np.ndarray,
    selected: Mapping[str, Any],
) -> list[bool]:
    return [
        _decide(float(probability), selected["long_threshold"], selected["short_threshold"])
        != "abstain"
        for probability in probabilities
    ]


def _run_cost_stress(
    manifest: ExperimentManifest,
    selected: dict[str, Any],
    calibrator: ProbabilityCalibrator,
    test_rows: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    features, _ = _matrix(manifest, test_rows)
    model: _TrainedModel = selected["model"]
    raw = model.predict_probability(features)
    calibrated = np.asarray(calibrator.predict(raw.tolist()), dtype=float)
    stress_rows: list[dict[str, Any]] = []
    for multiplier in manifest.costs.stress_multipliers:
        evaluated = _evaluate_decisions(
            manifest,
            test_rows,
            calibrated,
            selected["long_threshold"],
            selected["short_threshold"],
            cost_multiplier=multiplier,
        )
        stress_rows.append(
            {
                "cost_multiplier": multiplier,
                "trade_count": evaluated["metrics"]["trade_count"],
                "net_expectancy_r": evaluated["metrics"].get("net_expectancy_r"),
                "median_net_r": evaluated["metrics"].get("median_net_r"),
                "max_drawdown_r": evaluated["metrics"].get("max_drawdown_r"),
            }
        )
    return stress_rows


def _promotion_evidence(
    manifest: ExperimentManifest,
    git_state: GitState,
    normalized_hash: str,
    trial_count: int,
    test_result: Mapping[str, Any],
    diagnostics: Mapping[str, Any],
    stress_rows: Sequence[Mapping[str, Any]],
    lockbox_state: LockboxState,
) -> PromotionEvidence:
    metrics = test_result["metrics"]
    statistics = diagnostics["statistics"]
    guard = diagnostics["sample_size_guard"]

    def stat_value(name: str, key: str) -> float | None:
        entry = statistics[name]
        if not entry["available"]:
            return None
        value = entry["value"].get(key)
        return float(value) if value is not None else None

    stress_2x = next(
        (row["net_expectancy_r"] for row in stress_rows if row["cost_multiplier"] == 2.0),
        None,
    )
    regime_count = len(diagnostics["regimes"]["test_trade_counts"])
    return PromotionEvidence(
        dataset_hash=normalized_hash,
        git_commit=git_state.commit,
        dirty_worktree=git_state.dirty,
        synthetic_data=manifest.data.synthetic,
        point_in_time_violations=0,
        future_feature_violations=0,
        trial_count=trial_count,
        sample_count=int(guard["effective_trade_count"]),
        net_expectancy_r=metrics.get("net_expectancy_r"),
        expectancy_ci_lower_r=stat_value("bootstrap_ci", "lower"),
        dsr_probability=stat_value("dsr_tune_selection", "dsr"),
        pbo_probability=stat_value("pbo", "pbo"),
        max_drawdown_pct=None,
        brier_improvement=metrics.get("brier_improvement"),
        cost_stress_2x_expectancy_r=stress_2x,
        regime_count=regime_count if regime_count else None,
        pair_count=1,
        lockbox_evaluated_once=lockbox_state.evaluated_once,
        lockbox_reused_for_selection=lockbox_state.reused_for_selection,
        shadow_days=None,
        paper_days=None,
        major_operational_incidents=None,
        data_quality_incidents=0,
        calibration_window_separate=True,
        test_window_separate=True,
        live_like_execution_validated=None,
    )


# ---------------------------------------------------------------------------
# bundle
# ---------------------------------------------------------------------------


def _write_bundle(
    manifest: ExperimentManifest,
    output_dir: Path,
    *,
    started_at: datetime,
    environment: Mapping[str, Any],
    git_state: GitState,
    lineage: Sequence[Mapping[str, str]],
    rows: Sequence[Mapping[str, Any]],
    lockbox_rows: Sequence[Mapping[str, Any]],
    partitions: ModelPartitions,
    trial_records: Sequence[Mapping[str, Any]],
    evaluation_payload: Mapping[str, Any],
    stress_payload: Mapping[str, Any],
    promotion_payload: Mapping[str, Any],
    manifest_hash: str,
    selected_candidate_id: str,
    report: PromotionReport,
) -> ExperimentRunResult:
    if output_dir.exists():
        raise TypedFailure(
            FailureReason.INVALID,
            "evidence bundle directory already exists; experiment IDs are single-use",
            context={"output_dir": str(output_dir)},
        )
    output_dir.mkdir(parents=True, exist_ok=False)

    lockbox_inputs = [
        {key: value for key, value in row.items() if not _is_outcome_field(key)}
        for row in lockbox_rows
    ]
    lockbox_payload = {
        "status": "unopened",
        "row_count": len(lockbox_rows),
        "commitment_sha256": partitions.lockbox_commitment,
        "inputs_sha256": _canonical_sha256(lockbox_inputs),
        "policy": {
            "access_policy": manifest.lockbox.access_policy,
            "access_count_limit": manifest.lockbox.access_count_limit,
        },
    }
    development_positions = sorted(
        set(range(len(rows))) - set(partitions.withheld_lockbox_positions)
    )
    development_rows = [rows[index] for index in development_positions]

    _write_json(output_dir / "manifest.json", manifest.to_dict())
    _write_json(output_dir / "data_lineage.json", {"chain": list(lineage)})
    _write_jsonl(output_dir / "dataset_rows.jsonl", development_rows)
    _write_json(output_dir / "lockbox.json", lockbox_payload)
    _write_json(output_dir / "evaluation.json", evaluation_payload)
    _write_json(output_dir / "cost_stress.json", stress_payload)
    _write_json(output_dir / "promotion_decision.json", promotion_payload)
    _write_json(output_dir / "environment.json", dict(environment))
    _write_json(
        output_dir / "git.json",
        {"commit": git_state.commit, "dirty_worktree": git_state.dirty},
    )
    _write_jsonl(output_dir / "trial_ledger_snapshot.jsonl", trial_records)
    finished_at = datetime.now(UTC)
    _write_json(
        output_dir / "run_info.json",
        {
            "started_at": started_at.isoformat(),
            "finished_at": finished_at.isoformat(),
            "pipeline_version": PIPELINE_VERSION,
        },
    )

    hashes: dict[str, str] = {}
    for name in (*DETERMINISTIC_ARTIFACTS, *OPERATIONAL_ARTIFACTS):
        hashes[name] = hashlib.sha256((output_dir / name).read_bytes()).hexdigest()
    deterministic_result = hashlib.sha256(
        json.dumps(
            [[name, hashes[name]] for name in DETERMINISTIC_ARTIFACTS],
            separators=(",", ":"),
        ).encode("ascii")
    ).hexdigest()
    evidence_bundle = hashlib.sha256(
        json.dumps(sorted(hashes.items()), separators=(",", ":")).encode("ascii")
    ).hexdigest()
    _write_json(
        output_dir / "artifact_hashes.json",
        {
            "artifacts": hashes,
            "deterministic_artifacts": list(DETERMINISTIC_ARTIFACTS),
            "deterministic_result_sha256": deterministic_result,
            "evidence_bundle_sha256": evidence_bundle,
        },
    )

    summary = {
        "experiment_id": manifest.experiment_id,
        "manifest_sha256": manifest_hash,
        "selected_candidate_id": selected_candidate_id,
        "promotion_passed": report.passed,
        "promotion_failures": list(report.failures),
        "performance_claim": evaluation_payload["performance_claim"],
        "deterministic_result_sha256": deterministic_result,
        "evidence_bundle_sha256": evidence_bundle,
        "output_dir": str(output_dir),
    }
    return ExperimentRunResult(
        experiment_id=manifest.experiment_id,
        output_dir=output_dir,
        manifest_sha256=manifest_hash,
        deterministic_result_sha256=deterministic_result,
        evidence_bundle_sha256=evidence_bundle,
        selected_candidate_id=selected_candidate_id,
        promotion_passed=report.passed,
        promotion_failures=report.failures,
        summary=summary,
    )


def _is_outcome_field(key: str) -> bool:
    return key.startswith("long_") or key.startswith("short_") or key == "label_up"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _safe_statistic(compute: Any) -> dict[str, Any]:
    try:
        value = compute()
    except (ValueError, ZeroDivisionError) as error:
        return {"available": False, "reason": str(error)}
    return {"available": True, "value": _json_ready(value)}


def _canonical_sha256(payload: Any) -> str:
    encoded = json.dumps(
        _json_ready(payload), sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("ascii")
    return hashlib.sha256(encoded).hexdigest()


def _json_ready(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _json_ready(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_ready(item) for item in value]
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating,)):
        return float(value)
    if isinstance(value, np.ndarray):
        return [_json_ready(item) for item in value.tolist()]
    if isinstance(value, (datetime, pd.Timestamp)):
        return value.isoformat()
    if isinstance(value, float) and not np.isfinite(value):
        return None
    return value


def _write_json(path: Path, payload: Any) -> None:
    path.write_text(
        json.dumps(_json_ready(payload), indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def _write_jsonl(path: Path, rows: Sequence[Mapping[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(_json_ready(row), sort_keys=True, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------------------
# CLI (thin wrapper only; business logic stays in run_experiment)
# ---------------------------------------------------------------------------


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m fx_backtester.experiment_pipeline",
        description="Run the authoritative research experiment pipeline.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    run_parser = subparsers.add_parser("run", help="run one experiment from a manifest")
    run_parser.add_argument("--experiment-manifest", required=True)
    run_parser.add_argument("--output-root", default="runs/experiments")
    run_parser.add_argument("--trial-ledger", default=None)
    run_parser.add_argument("--lockbox-registry", default=None)
    lockbox_parser = subparsers.add_parser(
        "evaluate-lockbox", help="single-use governed lockbox evaluation of a bundle"
    )
    lockbox_parser.add_argument("--evidence-dir", required=True)
    lockbox_parser.add_argument("--purpose", required=True)
    lockbox_parser.add_argument("--actor", required=True)
    lockbox_parser.add_argument("--lockbox-registry", default=None)
    arguments = parser.parse_args(argv)
    try:
        if arguments.command == "run":
            result = run_experiment(
                arguments.experiment_manifest,
                output_root=arguments.output_root,
                trial_ledger_path=arguments.trial_ledger,
                lockbox_registry_dir=arguments.lockbox_registry,
            )
            payload: dict[str, Any] = {"status": "completed", **result.summary}
        else:
            payload = {
                "status": "completed",
                **evaluate_lockbox(
                    arguments.evidence_dir,
                    purpose=arguments.purpose,
                    actor=arguments.actor,
                    lockbox_registry_dir=arguments.lockbox_registry,
                ),
            }
    except TypedFailure as failure:
        print(json.dumps({"status": "failed", **failure.to_dict()}, ensure_ascii=False))
        return 1
    print(json.dumps(_json_ready(payload), ensure_ascii=False))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
