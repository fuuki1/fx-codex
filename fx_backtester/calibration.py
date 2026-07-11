"""Probability calibration, diagnostics, and explicit no-trade decisions.

Calibrators are fitted only on a dedicated calibration window.  When more than
one method is compared, a later selection window is required; test and lockbox
observations are never accepted by the selection API.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass, field
from math import isfinite, sqrt
from typing import Literal, Protocol

import numpy as np
import pandas as pd

CalibrationMethod = Literal["platt", "isotonic", "beta"]
DecisionAction = Literal["long", "short", "no_trade"]
_EPS = 1e-7


class CalibrationError(ValueError):
    """Raised when calibration is invalid or would use a leaky split."""


class ProbabilityCalibrator(Protocol):
    method: str
    fitted: bool

    def fit(
        self, probabilities: Sequence[float], labels: Sequence[int]
    ) -> ProbabilityCalibrator: ...

    def predict(self, probabilities: Sequence[float]) -> np.ndarray: ...

    def to_dict(self) -> dict[str, object]: ...


def sigmoid(values: np.ndarray | Sequence[float] | float) -> np.ndarray:
    array = np.asarray(values, dtype=float)
    clipped = np.clip(array, -35.0, 35.0)
    return 1.0 / (1.0 + np.exp(-clipped))


def logit(probabilities: np.ndarray | Sequence[float]) -> np.ndarray:
    array = np.clip(np.asarray(probabilities, dtype=float), _EPS, 1.0 - _EPS)
    return np.log(array / (1.0 - array))


@dataclass
class PlattCalibrator:
    method: str = field(default="platt", init=False)
    scale: float = 1.0
    offset: float = 0.0
    fitted: bool = False

    def fit(self, probabilities: Sequence[float], labels: Sequence[int]) -> PlattCalibrator:
        values, targets = _validated_fit_data(probabilities, labels)
        features = np.column_stack([logit(values), np.ones(len(values))])
        coefficients = _fit_logistic(
            features,
            targets,
            initial=np.array([1.0, 0.0]),
            positive_coefficients=(0,),
        )
        self.scale = float(coefficients[0])
        self.offset = float(coefficients[1])
        self.fitted = True
        return self

    def predict(self, probabilities: Sequence[float]) -> np.ndarray:
        values = _validated_probabilities(probabilities)
        if not self.fitted:
            raise CalibrationError("Platt calibrator has not been fitted")
        return sigmoid(self.scale * logit(values) + self.offset)

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass
class BetaCalibrator:
    """Monotone beta calibration using log(p) and -log(1-p)."""

    method: str = field(default="beta", init=False)
    log_p_scale: float = 1.0
    log_one_minus_p_scale: float = 1.0
    offset: float = 0.0
    fitted: bool = False

    def fit(self, probabilities: Sequence[float], labels: Sequence[int]) -> BetaCalibrator:
        values, targets = _validated_fit_data(probabilities, labels)
        clipped = np.clip(values, _EPS, 1.0 - _EPS)
        features = np.column_stack([np.log(clipped), -np.log1p(-clipped), np.ones(len(clipped))])
        coefficients = _fit_logistic(
            features,
            targets,
            initial=np.array([1.0, 1.0, 0.0]),
            positive_coefficients=(0, 1),
        )
        self.log_p_scale = float(coefficients[0])
        self.log_one_minus_p_scale = float(coefficients[1])
        self.offset = float(coefficients[2])
        self.fitted = True
        return self

    def predict(self, probabilities: Sequence[float]) -> np.ndarray:
        values = _validated_probabilities(probabilities)
        if not self.fitted:
            raise CalibrationError("beta calibrator has not been fitted")
        clipped = np.clip(values, _EPS, 1.0 - _EPS)
        margin = (
            self.log_p_scale * np.log(clipped)
            - self.log_one_minus_p_scale * np.log1p(-clipped)
            + self.offset
        )
        return sigmoid(margin)

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass
class IsotonicCalibrator:
    method: str = field(default="isotonic", init=False)
    upper_bounds: list[float] = field(default_factory=list)
    values: list[float] = field(default_factory=list)
    fitted: bool = False

    def fit(self, probabilities: Sequence[float], labels: Sequence[int]) -> IsotonicCalibrator:
        scores, targets = _validated_fit_data(probabilities, labels)
        order = np.argsort(scores, kind="stable")
        scores = scores[order]
        targets = targets[order]

        blocks: list[dict[str, float]] = []
        for score in np.unique(scores):
            selected = targets[scores == score]
            blocks.append(
                {
                    "upper": float(score),
                    "sum": float(selected.sum()),
                    "weight": float(len(selected)),
                }
            )
            while len(blocks) >= 2 and _block_mean(blocks[-2]) > _block_mean(blocks[-1]):
                right = blocks.pop()
                left = blocks.pop()
                blocks.append(
                    {
                        "upper": right["upper"],
                        "sum": left["sum"] + right["sum"],
                        "weight": left["weight"] + right["weight"],
                    }
                )

        self.upper_bounds = [block["upper"] for block in blocks]
        self.values = [float(np.clip(_block_mean(block), _EPS, 1.0 - _EPS)) for block in blocks]
        self.fitted = True
        return self

    def predict(self, probabilities: Sequence[float]) -> np.ndarray:
        scores = _validated_probabilities(probabilities)
        if not self.fitted or not self.upper_bounds:
            raise CalibrationError("isotonic calibrator has not been fitted")
        positions = np.searchsorted(np.asarray(self.upper_bounds), scores, side="left")
        positions = np.clip(positions, 0, len(self.values) - 1)
        return np.asarray(self.values, dtype=float)[positions]

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class CalibrationMetrics:
    rows: int
    brier: float
    log_loss: float
    expected_calibration_error: float
    maximum_calibration_error: float
    calibration_slope: float | None
    calibration_intercept: float | None

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def calibration_metrics(
    labels: Sequence[int], probabilities: Sequence[float], *, bins: int = 10
) -> CalibrationMetrics:
    values = _validated_probabilities(probabilities)
    targets = _validated_labels(labels, require_both_classes=False)
    if len(values) != len(targets) or len(values) == 0:
        raise CalibrationError("labels and probabilities must have equal non-zero length")
    if bins < 2:
        raise ValueError("bins must be at least 2")
    clipped = np.clip(values, _EPS, 1.0 - _EPS)
    brier = float(np.mean((clipped - targets) ** 2))
    loss = float(-np.mean(targets * np.log(clipped) + (1.0 - targets) * np.log1p(-clipped)))
    ece, maximum = _calibration_errors(targets, clipped, bins)
    slope, intercept = _calibration_slope_intercept(targets, clipped)
    return CalibrationMetrics(
        rows=len(values),
        brier=brier,
        log_loss=loss,
        expected_calibration_error=ece,
        maximum_calibration_error=maximum,
        calibration_slope=slope,
        calibration_intercept=intercept,
    )


@dataclass(frozen=True)
class CalibratorSelection:
    method: str
    calibrator: ProbabilityCalibrator
    selection_metrics: MappingLike
    all_selection_metrics: dict[str, dict[str, object]]


MappingLike = dict[str, object]


def select_calibrator(
    calibration_probabilities: Sequence[float],
    calibration_labels: Sequence[int],
    calibration_times: Sequence[object] | pd.DatetimeIndex,
    selection_probabilities: Sequence[float],
    selection_labels: Sequence[int],
    selection_times: Sequence[object] | pd.DatetimeIndex,
    *,
    methods: Sequence[CalibrationMethod] = ("platt", "isotonic", "beta"),
) -> CalibratorSelection:
    """Fit on one window and choose the method on a strictly later window."""

    calibration_index = _aware_times(calibration_times, "calibration_times")
    selection_index = _aware_times(selection_times, "selection_times")
    if len(calibration_index) != len(calibration_probabilities):
        raise CalibrationError("calibration timestamps and probabilities differ in length")
    if len(selection_index) != len(selection_probabilities):
        raise CalibrationError("selection timestamps and probabilities differ in length")
    if calibration_index.max() >= selection_index.min():
        raise CalibrationError("calibration and selection windows overlap or are reversed")
    if not methods:
        raise CalibrationError("at least one calibration method is required")

    fitted: dict[str, ProbabilityCalibrator] = {}
    reports: dict[str, CalibrationMetrics] = {}
    for method in methods:
        calibrator = fit_calibrator(method, calibration_probabilities, calibration_labels)
        predictions = calibrator.predict(selection_probabilities)
        report = calibration_metrics(selection_labels, predictions.tolist())
        fitted[method] = calibrator
        reports[method] = report
    best_method = min(
        reports,
        key=lambda name: (
            reports[name].log_loss,
            reports[name].brier,
            name,
        ),
    )
    metrics = {method: report.to_dict() for method, report in reports.items()}
    return CalibratorSelection(
        method=best_method,
        calibrator=fitted[best_method],
        selection_metrics=metrics[best_method],
        all_selection_metrics=metrics,
    )


def fit_calibrator(
    method: CalibrationMethod,
    probabilities: Sequence[float],
    labels: Sequence[int],
) -> ProbabilityCalibrator:
    if method == "platt":
        return PlattCalibrator().fit(probabilities, labels)
    if method == "isotonic":
        return IsotonicCalibrator().fit(probabilities, labels)
    if method == "beta":
        return BetaCalibrator().fit(probabilities, labels)
    raise CalibrationError(f"unsupported calibration method: {method}")


@dataclass(frozen=True)
class AbstentionPolicy:
    long_probability: float = 0.60
    short_probability: float = 0.40
    min_net_expected_r: float = 0.0
    max_interval_width: float = 0.30
    max_model_disagreement: float = 0.20
    require_calibrated_probability: bool = True

    def __post_init__(self) -> None:
        if not 0.5 < self.long_probability < 1.0:
            raise ValueError("long_probability must be between 0.5 and 1")
        if not 0.0 < self.short_probability < 0.5:
            raise ValueError("short_probability must be between 0 and 0.5")
        if self.short_probability >= self.long_probability:
            raise ValueError("short threshold must be below long threshold")
        if self.max_interval_width <= 0 or self.max_model_disagreement < 0:
            raise ValueError("uncertainty thresholds must be non-negative")

    def decide(
        self,
        probability_up: float,
        *,
        net_expected_r: float | None,
        uncertainty_interval: tuple[float, float] | None,
        model_disagreement: float | None = None,
        calibrated: bool = True,
        data_quality_vetoes: Iterable[str] = (),
        risk_vetoes: Iterable[str] = (),
    ) -> AbstentionDecision:
        probability = float(probability_up)
        if not isfinite(probability) or not 0.0 <= probability <= 1.0:
            return AbstentionDecision.no_trade(probability, "invalid_probability")
        data_reasons = tuple(dict.fromkeys(str(reason) for reason in data_quality_vetoes if reason))
        risk_reasons = tuple(dict.fromkeys(str(reason) for reason in risk_vetoes if reason))
        if data_reasons:
            return AbstentionDecision.no_trade(
                probability, "data_quality_veto", hard_vetoes=data_reasons
            )
        if risk_reasons:
            return AbstentionDecision.no_trade(probability, "risk_veto", hard_vetoes=risk_reasons)
        if self.require_calibrated_probability and not calibrated:
            return AbstentionDecision.no_trade(probability, "uncalibrated_probability")
        if uncertainty_interval is None:
            return AbstentionDecision.no_trade(probability, "uncertainty_unavailable")
        lower, upper = uncertainty_interval
        if not all(isfinite(value) for value in (lower, upper)) or not 0 <= lower <= upper <= 1:
            return AbstentionDecision.no_trade(probability, "invalid_uncertainty_interval")
        if not lower <= probability <= upper:
            return AbstentionDecision.no_trade(
                probability, "probability_outside_uncertainty_interval"
            )
        if upper - lower > self.max_interval_width:
            return AbstentionDecision.no_trade(probability, "uncertainty_too_wide")
        if model_disagreement is not None:
            if not isfinite(model_disagreement) or model_disagreement < 0:
                return AbstentionDecision.no_trade(probability, "invalid_model_disagreement")
            if model_disagreement > self.max_model_disagreement:
                return AbstentionDecision.no_trade(probability, "model_disagreement")
        if net_expected_r is None or not isfinite(net_expected_r):
            return AbstentionDecision.no_trade(probability, "net_expectancy_unavailable")
        if net_expected_r <= self.min_net_expected_r:
            return AbstentionDecision.no_trade(probability, "non_positive_net_expectancy")
        if probability >= self.long_probability:
            return AbstentionDecision("long", probability, "calibrated_edge", net_expected_r)
        if probability <= self.short_probability:
            return AbstentionDecision("short", probability, "calibrated_edge", net_expected_r)
        return AbstentionDecision.no_trade(probability, "probability_in_no_trade_band")


@dataclass(frozen=True)
class AbstentionDecision:
    action: DecisionAction
    probability_up: float
    reason: str
    net_expected_r: float | None = None
    hard_vetoes: tuple[str, ...] = ()

    @property
    def abstained(self) -> bool:
        return self.action == "no_trade"

    @classmethod
    def no_trade(
        cls,
        probability: float,
        reason: str,
        *,
        hard_vetoes: tuple[str, ...] = (),
    ) -> AbstentionDecision:
        return cls("no_trade", probability, reason, None, hard_vetoes)


def abstention_rate(decisions: Sequence[AbstentionDecision]) -> float:
    if not decisions:
        return 0.0
    return sum(decision.abstained for decision in decisions) / len(decisions)


def wilson_interval(
    successes: int, observations: int, confidence_z: float = 1.96
) -> tuple[float, float]:
    """Wilson score interval for an empirical probability bucket."""

    if observations < 1 or not 0 <= successes <= observations:
        raise ValueError("successes and observations are inconsistent")
    if confidence_z <= 0:
        raise ValueError("confidence_z must be positive")
    rate = successes / observations
    z2 = confidence_z**2
    denominator = 1.0 + z2 / observations
    centre = (rate + z2 / (2.0 * observations)) / denominator
    margin = (
        confidence_z
        * sqrt(rate * (1.0 - rate) / observations + z2 / (4.0 * observations**2))
        / denominator
    )
    return max(0.0, centre - margin), min(1.0, centre + margin)


def _fit_logistic(
    features: np.ndarray,
    labels: np.ndarray,
    *,
    initial: np.ndarray,
    positive_coefficients: tuple[int, ...],
    ridge: float = 1e-5,
    max_iterations: int = 200,
) -> np.ndarray:
    coefficients = initial.astype(float).copy()
    penalty = np.full(features.shape[1], ridge)
    penalty[-1] = 0.0
    previous_loss = _logistic_objective(features, labels, coefficients, penalty)
    for _ in range(max_iterations):
        predicted = sigmoid(features @ coefficients)
        gradient = features.T @ (predicted - labels) + penalty * coefficients
        weights = np.clip(predicted * (1.0 - predicted), 1e-8, None)
        hessian = features.T @ (features * weights[:, None]) + np.diag(penalty)
        try:
            step = np.linalg.solve(hessian, gradient)
        except np.linalg.LinAlgError:
            step = np.linalg.pinv(hessian) @ gradient
        if float(np.linalg.norm(step)) < 1e-8:
            break
        accepted = False
        factor = 1.0
        for _ in range(30):
            candidate = coefficients - factor * step
            if all(candidate[index] > 0.0 for index in positive_coefficients):
                loss = _logistic_objective(features, labels, candidate, penalty)
                if loss <= previous_loss + 1e-12:
                    coefficients = candidate
                    previous_loss = loss
                    accepted = True
                    break
            factor *= 0.5
        if not accepted:
            break
    if not bool(np.isfinite(coefficients).all()):
        raise CalibrationError("calibration optimization produced non-finite coefficients")
    if any(coefficients[index] <= 0.0 for index in positive_coefficients):
        raise CalibrationError("calibration would reverse probability ordering")
    return coefficients


def _logistic_objective(
    features: np.ndarray,
    labels: np.ndarray,
    coefficients: np.ndarray,
    penalty: np.ndarray,
) -> float:
    margin = features @ coefficients
    likelihood = np.logaddexp(0.0, margin) - labels * margin
    return float(likelihood.sum() + 0.5 * np.sum(penalty * coefficients**2))


def _validated_fit_data(
    probabilities: Sequence[float], labels: Sequence[int]
) -> tuple[np.ndarray, np.ndarray]:
    values = _validated_probabilities(probabilities)
    targets = _validated_labels(labels, require_both_classes=True)
    if len(values) != len(targets) or len(values) < 3:
        raise CalibrationError("calibration needs at least three paired observations")
    return values, targets


def _validated_probabilities(probabilities: Sequence[float]) -> np.ndarray:
    values = np.asarray(probabilities, dtype=float)
    if values.ndim != 1:
        raise CalibrationError("probabilities must be one-dimensional")
    if not bool(np.isfinite(values).all()) or bool(((values < 0) | (values > 1)).any()):
        raise CalibrationError("probabilities must be finite values in [0, 1]")
    return values


def _validated_labels(labels: Sequence[int], *, require_both_classes: bool) -> np.ndarray:
    values = np.asarray(labels)
    if values.ndim != 1 or not bool(np.isin(values, [0, 1]).all()):
        raise CalibrationError("labels must be a one-dimensional binary sequence")
    if require_both_classes and len(np.unique(values)) < 2:
        raise CalibrationError("both label classes are required for calibration")
    return values.astype(float)


def _calibration_errors(
    labels: np.ndarray, probabilities: np.ndarray, bins: int
) -> tuple[float, float]:
    edges = np.linspace(0.0, 1.0, bins + 1)
    assignments = np.clip(np.digitize(probabilities, edges[1:-1], right=False), 0, bins - 1)
    expected = 0.0
    maximum = 0.0
    for bucket in range(bins):
        selected = assignments == bucket
        count = int(selected.sum())
        if not count:
            continue
        gap = abs(float(probabilities[selected].mean() - labels[selected].mean()))
        expected += gap * count / len(labels)
        maximum = max(maximum, gap)
    return expected, maximum


def _calibration_slope_intercept(
    labels: np.ndarray, probabilities: np.ndarray
) -> tuple[float | None, float | None]:
    if len(np.unique(labels)) < 2 or float(np.std(probabilities)) < 1e-12:
        return None, None
    features = np.column_stack([logit(probabilities), np.ones(len(probabilities))])
    try:
        coefficients = _fit_logistic(
            features,
            labels,
            initial=np.array([1.0, 0.0]),
            positive_coefficients=(),
        )
    except CalibrationError:
        return None, None
    return float(coefficients[0]), float(coefficients[1])


def _aware_times(values: Sequence[object] | pd.DatetimeIndex, name: str) -> pd.DatetimeIndex:
    try:
        index = pd.DatetimeIndex(values)
    except (TypeError, ValueError) as error:
        raise CalibrationError(f"{name} could not be parsed") from error
    if len(index) == 0 or index.tz is None:
        raise CalibrationError(f"{name} must be non-empty and timezone-aware")
    index = index.tz_convert("UTC")
    if index.hasnans or not index.is_monotonic_increasing or index.has_duplicates:
        raise CalibrationError(f"{name} must be unique and monotonic")
    return index


def _block_mean(block: dict[str, float]) -> float:
    return block["sum"] / block["weight"]
