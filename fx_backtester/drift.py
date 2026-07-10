"""Deterministic data, prediction, and performance drift monitoring.

Unsupervised drift can be emitted immediately. Supervised performance drift is
reported separately and remains unavailable until labels mature; the absence of
labels is never interpreted as healthy performance.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from math import isfinite
from typing import Literal

import numpy as np
import pandas as pd

from fx_backtester.calibration import CalibrationMetrics, calibration_metrics

DriftAction = Literal["allow", "warn", "reduce_size", "abstain", "human_review", "demote"]


@dataclass(frozen=True)
class DriftPolicy:
    psi_warn: float = 0.10
    psi_abstain: float = 0.25
    ks_warn: float = 0.15
    ks_abstain: float = 0.30
    missing_rate_warn_delta: float = 0.03
    missing_rate_abstain_delta: float = 0.10
    range_violation_warn: float = 0.02
    correlation_shift_warn: float = 0.20
    prediction_mean_warn_delta: float = 0.10
    abstention_warn_delta: float = 0.15
    brier_demote_delta: float = 0.05
    log_loss_demote_delta: float = 0.10
    calibration_error_demote_delta: float = 0.05


@dataclass(frozen=True)
class FeatureDrift:
    feature: str
    baseline_rows: int
    current_rows: int
    baseline_missing_rate: float
    current_missing_rate: float
    missing_rate_delta: float
    psi: float | None
    ks_distance: float | None
    wasserstein_distance: float | None
    range_violation_rate: float | None

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class DataDriftReport:
    features: tuple[FeatureDrift, ...]
    max_correlation_shift: float | None
    action: DriftAction
    reasons: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "features": [feature.to_dict() for feature in self.features],
            "max_correlation_shift": self.max_correlation_shift,
            "action": self.action,
            "reasons": list(self.reasons),
        }


@dataclass(frozen=True)
class PredictionDriftReport:
    baseline_rows: int
    current_rows: int
    probability_mean_delta: float
    probability_std_delta: float
    positive_class_rate_delta: float
    abstention_rate_delta: float | None
    expected_r_mean_delta: float | None
    disagreement_mean_delta: float | None
    action: DriftAction
    reasons: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class PerformanceDriftReport:
    labels_available: bool
    baseline_metrics: CalibrationMetrics | None
    current_metrics: CalibrationMetrics | None
    brier_delta: float | None
    log_loss_delta: float | None
    calibration_error_delta: float | None
    baseline_expected_r: float | None
    current_expected_r: float | None
    action: DriftAction
    reasons: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "labels_available": self.labels_available,
            "baseline_metrics": (
                self.baseline_metrics.to_dict() if self.baseline_metrics is not None else None
            ),
            "current_metrics": (
                self.current_metrics.to_dict() if self.current_metrics is not None else None
            ),
            "brier_delta": self.brier_delta,
            "log_loss_delta": self.log_loss_delta,
            "calibration_error_delta": self.calibration_error_delta,
            "baseline_expected_r": self.baseline_expected_r,
            "current_expected_r": self.current_expected_r,
            "action": self.action,
            "reasons": list(self.reasons),
        }


@dataclass(frozen=True)
class DriftMonitoringReport:
    data: DataDriftReport
    prediction: PredictionDriftReport
    performance: PerformanceDriftReport
    final_action: DriftAction
    automated_retraining_allowed: bool = False
    automated_live_promotion_allowed: bool = False

    def to_dict(self) -> dict[str, object]:
        return {
            "data": self.data.to_dict(),
            "prediction": self.prediction.to_dict(),
            "performance": self.performance.to_dict(),
            "final_action": self.final_action,
            "automated_retraining_allowed": self.automated_retraining_allowed,
            "automated_live_promotion_allowed": self.automated_live_promotion_allowed,
        }


def data_drift_report(
    baseline: pd.DataFrame,
    current: pd.DataFrame,
    *,
    policy: DriftPolicy | None = None,
) -> DataDriftReport:
    settings = policy or DriftPolicy()
    common = sorted(set(baseline.columns) & set(current.columns))
    if not common:
        raise ValueError("baseline and current data have no common features")
    features: list[FeatureDrift] = []
    severe: list[str] = []
    warnings: list[str] = []
    for name in common:
        reference = pd.to_numeric(baseline[name], errors="coerce").to_numpy(dtype=float)
        observed = pd.to_numeric(current[name], errors="coerce").to_numpy(dtype=float)
        reference_finite = reference[np.isfinite(reference)]
        observed_finite = observed[np.isfinite(observed)]
        baseline_missing = 1.0 - len(reference_finite) / len(reference) if len(reference) else 1.0
        current_missing = 1.0 - len(observed_finite) / len(observed) if len(observed) else 1.0
        missing_delta = current_missing - baseline_missing
        psi = population_stability_index(reference_finite, observed_finite)
        ks = ks_distance(reference_finite, observed_finite)
        wasserstein = wasserstein_distance(reference_finite, observed_finite)
        range_violation = _range_violation_rate(reference_finite, observed_finite)
        feature = FeatureDrift(
            feature=str(name),
            baseline_rows=len(reference),
            current_rows=len(observed),
            baseline_missing_rate=baseline_missing,
            current_missing_rate=current_missing,
            missing_rate_delta=missing_delta,
            psi=psi,
            ks_distance=ks,
            wasserstein_distance=wasserstein,
            range_violation_rate=range_violation,
        )
        features.append(feature)
        if psi is not None and psi >= settings.psi_abstain:
            severe.append(f"{name}:psi={psi:.3f}")
        elif psi is not None and psi >= settings.psi_warn:
            warnings.append(f"{name}:psi={psi:.3f}")
        if ks is not None and ks >= settings.ks_abstain:
            severe.append(f"{name}:ks={ks:.3f}")
        elif ks is not None and ks >= settings.ks_warn:
            warnings.append(f"{name}:ks={ks:.3f}")
        if missing_delta >= settings.missing_rate_abstain_delta:
            severe.append(f"{name}:missing_delta={missing_delta:.3f}")
        elif missing_delta >= settings.missing_rate_warn_delta:
            warnings.append(f"{name}:missing_delta={missing_delta:.3f}")
        if range_violation is not None and range_violation >= settings.range_violation_warn:
            warnings.append(f"{name}:range_violation={range_violation:.3f}")

    correlation_shift = _max_correlation_shift(baseline[common], current[common])
    if correlation_shift is not None and correlation_shift >= settings.correlation_shift_warn:
        warnings.append(f"correlation_shift={correlation_shift:.3f}")
    reasons = tuple(dict.fromkeys([*severe, *warnings]))
    action: DriftAction = "abstain" if severe else "warn" if warnings else "allow"
    return DataDriftReport(tuple(features), correlation_shift, action, reasons)


def prediction_drift_report(
    baseline_probabilities: list[float] | np.ndarray,
    current_probabilities: list[float] | np.ndarray,
    *,
    baseline_abstained: list[bool] | np.ndarray | None = None,
    current_abstained: list[bool] | np.ndarray | None = None,
    baseline_expected_r: list[float] | np.ndarray | None = None,
    current_expected_r: list[float] | np.ndarray | None = None,
    baseline_disagreement: list[float] | np.ndarray | None = None,
    current_disagreement: list[float] | np.ndarray | None = None,
    policy: DriftPolicy | None = None,
) -> PredictionDriftReport:
    settings = policy or DriftPolicy()
    baseline = _probabilities(baseline_probabilities)
    current = _probabilities(current_probabilities)
    mean_delta = float(current.mean() - baseline.mean())
    std_delta = float(current.std() - baseline.std())
    class_delta = float((current >= 0.5).mean() - (baseline >= 0.5).mean())
    abstention_delta = _boolean_rate_delta(baseline_abstained, current_abstained, "abstention")
    expected_r_delta = _mean_delta(baseline_expected_r, current_expected_r, "expected_r")
    disagreement_delta = _mean_delta(baseline_disagreement, current_disagreement, "disagreement")
    reasons: list[str] = []
    if abs(mean_delta) >= settings.prediction_mean_warn_delta:
        reasons.append(f"probability_mean_delta={mean_delta:+.3f}")
    if abstention_delta is not None and abs(abstention_delta) >= settings.abstention_warn_delta:
        reasons.append(f"abstention_rate_delta={abstention_delta:+.3f}")
    action: DriftAction = "warn" if reasons else "allow"
    return PredictionDriftReport(
        baseline_rows=len(baseline),
        current_rows=len(current),
        probability_mean_delta=mean_delta,
        probability_std_delta=std_delta,
        positive_class_rate_delta=class_delta,
        abstention_rate_delta=abstention_delta,
        expected_r_mean_delta=expected_r_delta,
        disagreement_mean_delta=disagreement_delta,
        action=action,
        reasons=tuple(reasons),
    )


def performance_drift_report(
    baseline_labels: list[int] | np.ndarray | None,
    baseline_probabilities: list[float] | np.ndarray | None,
    current_labels: list[int] | np.ndarray | None,
    current_probabilities: list[float] | np.ndarray | None,
    *,
    baseline_returns_r: list[float] | np.ndarray | None = None,
    current_returns_r: list[float] | np.ndarray | None = None,
    policy: DriftPolicy | None = None,
) -> PerformanceDriftReport:
    settings = policy or DriftPolicy()
    if any(
        value is None
        for value in (
            baseline_labels,
            baseline_probabilities,
            current_labels,
            current_probabilities,
        )
    ):
        return PerformanceDriftReport(
            labels_available=False,
            baseline_metrics=None,
            current_metrics=None,
            brier_delta=None,
            log_loss_delta=None,
            calibration_error_delta=None,
            baseline_expected_r=_optional_mean(baseline_returns_r),
            current_expected_r=_optional_mean(current_returns_r),
            action="human_review",
            reasons=("ground_truth_not_mature",),
        )
    baseline_report = calibration_metrics(
        list(np.asarray(baseline_labels, dtype=int)),
        list(np.asarray(baseline_probabilities, dtype=float)),
    )
    current_report = calibration_metrics(
        list(np.asarray(current_labels, dtype=int)),
        list(np.asarray(current_probabilities, dtype=float)),
    )
    brier_delta = current_report.brier - baseline_report.brier
    loss_delta = current_report.log_loss - baseline_report.log_loss
    calibration_delta = (
        current_report.expected_calibration_error - baseline_report.expected_calibration_error
    )
    baseline_expectancy = _optional_mean(baseline_returns_r)
    current_expectancy = _optional_mean(current_returns_r)
    reasons: list[str] = []
    if brier_delta >= settings.brier_demote_delta:
        reasons.append(f"brier_delta={brier_delta:+.3f}")
    if loss_delta >= settings.log_loss_demote_delta:
        reasons.append(f"log_loss_delta={loss_delta:+.3f}")
    if calibration_delta >= settings.calibration_error_demote_delta:
        reasons.append(f"calibration_error_delta={calibration_delta:+.3f}")
    if current_expectancy is not None and current_expectancy < 0:
        reasons.append(f"current_expected_r={current_expectancy:+.3f}")
    action: DriftAction = "demote" if reasons else "allow"
    return PerformanceDriftReport(
        labels_available=True,
        baseline_metrics=baseline_report,
        current_metrics=current_report,
        brier_delta=brier_delta,
        log_loss_delta=loss_delta,
        calibration_error_delta=calibration_delta,
        baseline_expected_r=baseline_expectancy,
        current_expected_r=current_expectancy,
        action=action,
        reasons=tuple(reasons),
    )


def monitor_drift(
    baseline_features: pd.DataFrame,
    current_features: pd.DataFrame,
    baseline_probabilities: list[float] | np.ndarray,
    current_probabilities: list[float] | np.ndarray,
    *,
    baseline_labels: list[int] | np.ndarray | None = None,
    current_labels: list[int] | np.ndarray | None = None,
    baseline_returns_r: list[float] | np.ndarray | None = None,
    current_returns_r: list[float] | np.ndarray | None = None,
    policy: DriftPolicy | None = None,
) -> DriftMonitoringReport:
    settings = policy or DriftPolicy()
    data = data_drift_report(baseline_features, current_features, policy=settings)
    prediction = prediction_drift_report(
        baseline_probabilities, current_probabilities, policy=settings
    )
    performance = performance_drift_report(
        baseline_labels,
        baseline_probabilities,
        current_labels,
        current_probabilities,
        baseline_returns_r=baseline_returns_r,
        current_returns_r=current_returns_r,
        policy=settings,
    )
    rank: dict[DriftAction, int] = {
        "allow": 0,
        "warn": 1,
        "reduce_size": 2,
        "human_review": 3,
        "abstain": 4,
        "demote": 5,
    }
    final = max((data.action, prediction.action, performance.action), key=rank.__getitem__)
    return DriftMonitoringReport(data, prediction, performance, final)


def population_stability_index(
    baseline: np.ndarray | list[float], current: np.ndarray | list[float], bins: int = 10
) -> float | None:
    reference = _finite(baseline)
    observed = _finite(current)
    if len(reference) < 2 or len(observed) < 2:
        return None
    quantiles = np.unique(np.quantile(reference, np.linspace(0.0, 1.0, bins + 1)))
    if len(quantiles) < 2:
        return 0.0 if bool(np.allclose(observed, reference[0])) else float("inf")
    edges = np.concatenate(([-np.inf], quantiles[1:-1], [np.inf]))
    expected = np.histogram(reference, bins=edges)[0] / len(reference)
    actual = np.histogram(observed, bins=edges)[0] / len(observed)
    expected = np.clip(expected, 1e-6, None)
    actual = np.clip(actual, 1e-6, None)
    return float(np.sum((actual - expected) * np.log(actual / expected)))


def ks_distance(
    baseline: np.ndarray | list[float], current: np.ndarray | list[float]
) -> float | None:
    reference = np.sort(_finite(baseline))
    observed = np.sort(_finite(current))
    if not len(reference) or not len(observed):
        return None
    support = np.sort(np.unique(np.concatenate([reference, observed])))
    reference_cdf = np.searchsorted(reference, support, side="right") / len(reference)
    observed_cdf = np.searchsorted(observed, support, side="right") / len(observed)
    return float(np.max(np.abs(reference_cdf - observed_cdf)))


def wasserstein_distance(
    baseline: np.ndarray | list[float], current: np.ndarray | list[float]
) -> float | None:
    reference = _finite(baseline)
    observed = _finite(current)
    if not len(reference) or not len(observed):
        return None
    grid = np.linspace(0.0, 1.0, max(len(reference), len(observed)))
    return float(np.mean(np.abs(np.quantile(reference, grid) - np.quantile(observed, grid))))


def _range_violation_rate(baseline: np.ndarray, current: np.ndarray) -> float | None:
    if not len(baseline) or not len(current):
        return None
    return float(((current < baseline.min()) | (current > baseline.max())).mean())


def _max_correlation_shift(baseline: pd.DataFrame, current: pd.DataFrame) -> float | None:
    if baseline.shape[1] < 2:
        return None
    base_corr = baseline.apply(pd.to_numeric, errors="coerce").corr()
    current_corr = current.apply(pd.to_numeric, errors="coerce").corr()
    difference = (base_corr - current_corr).abs().to_numpy(dtype=float)
    finite = difference[np.isfinite(difference)]
    return float(finite.max()) if len(finite) else None


def _probabilities(values: list[float] | np.ndarray) -> np.ndarray:
    array = np.asarray(values, dtype=float)
    if array.ndim != 1 or not len(array):
        raise ValueError("probabilities must be a non-empty one-dimensional series")
    if not bool(np.isfinite(array).all()) or bool(((array < 0) | (array > 1)).any()):
        raise ValueError("probabilities must be finite and in [0, 1]")
    return array


def _finite(values: np.ndarray | list[float]) -> np.ndarray:
    array = np.asarray(values, dtype=float)
    return array[np.isfinite(array)]


def _boolean_rate_delta(
    baseline: list[bool] | np.ndarray | None,
    current: list[bool] | np.ndarray | None,
    name: str,
) -> float | None:
    if baseline is None and current is None:
        return None
    if baseline is None or current is None:
        raise ValueError(f"both baseline and current {name} arrays are required")
    first = np.asarray(baseline, dtype=bool)
    second = np.asarray(current, dtype=bool)
    if not len(first) or not len(second):
        raise ValueError(f"{name} arrays cannot be empty")
    return float(second.mean() - first.mean())


def _mean_delta(
    baseline: list[float] | np.ndarray | None,
    current: list[float] | np.ndarray | None,
    name: str,
) -> float | None:
    if baseline is None and current is None:
        return None
    if baseline is None or current is None:
        raise ValueError(f"both baseline and current {name} arrays are required")
    first = _finite(baseline)
    second = _finite(current)
    if not len(first) or not len(second):
        raise ValueError(f"{name} arrays have no finite values")
    return float(second.mean() - first.mean())


def _optional_mean(values: list[float] | np.ndarray | None) -> float | None:
    if values is None:
        return None
    finite = _finite(values)
    if not len(finite):
        return None
    value = float(finite.mean())
    return value if isfinite(value) else None
