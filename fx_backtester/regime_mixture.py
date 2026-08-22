"""Deterministic volatility-regime mixture of GBDT experts.

The gate and every expert are fitted from the training partition only.  A
global model remains the prior for all predictions; eligible regime experts
are shrunk toward that prior according to their training sample size.  This
keeps small regime cells from producing unjustifiably extreme probabilities.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import Any

from fx_intel.gbm import GradientBoostingClassifier

LOW_VOLATILITY = 0
MID_VOLATILITY = 1
HIGH_VOLATILITY = 2
REGIME_IDS = (LOW_VOLATILITY, MID_VOLATILITY, HIGH_VOLATILITY)
PROBABILITY_EPSILON = 1e-6


class InsufficientRegimeSamples(ValueError):
    """Raised when no regime cell can support a two-class expert."""


def _quantile(values: Sequence[float], probability: float) -> float:
    """Return a deterministic linearly interpolated empirical quantile."""

    ordered = sorted(float(value) for value in values)
    if not ordered:
        raise ValueError("regime feature is empty")
    position = (len(ordered) - 1) * probability
    lower_index = math.floor(position)
    upper_index = math.ceil(position)
    if lower_index == upper_index:
        return ordered[lower_index]
    fraction = position - lower_index
    return ordered[lower_index] * (1.0 - fraction) + ordered[upper_index] * fraction


def _regime_from_bounds(value: float, bounds: tuple[float, float]) -> int:
    lower, upper = bounds
    if value <= lower:
        return LOW_VOLATILITY
    if value <= upper:
        return MID_VOLATILITY
    return HIGH_VOLATILITY


class RegimeMixtureClassifier:
    """Global GBDT plus low/mid/high-volatility GBDT experts."""

    def __init__(
        self,
        *,
        regime_feature_index: int,
        regime_feature_name: str,
        min_expert_samples: int,
        shrinkage_samples: float,
        n_estimators: int,
        learning_rate: float,
        max_depth: int,
        min_samples_leaf: int,
        subsample: float,
        feature_fraction: float,
        reg_lambda: float,
        seed: int,
    ) -> None:
        if regime_feature_index < 0:
            raise ValueError("regime_feature_index must be non-negative")
        if not regime_feature_name:
            raise ValueError("regime_feature_name must not be empty")
        if min_expert_samples < 2:
            raise ValueError("min_expert_samples must be at least 2")
        if not math.isfinite(shrinkage_samples) or shrinkage_samples <= 0:
            raise ValueError("shrinkage_samples must be positive and finite")
        if n_estimators < 1:
            raise ValueError("n_estimators must be positive")
        if max_depth < 1:
            raise ValueError("max_depth must be positive")
        if min_samples_leaf < 1:
            raise ValueError("min_samples_leaf must be positive")
        if min_expert_samples < 2 * min_samples_leaf:
            raise ValueError("min_expert_samples must support two minimum-size leaves")
        if not math.isfinite(reg_lambda) or reg_lambda < 0:
            raise ValueError("reg_lambda must be non-negative and finite")
        if seed < 0:
            raise ValueError("seed must be non-negative")

        self.regime_feature_index = regime_feature_index
        self.regime_feature_name = regime_feature_name
        self.min_expert_samples = min_expert_samples
        self.shrinkage_samples = shrinkage_samples
        self.model_parameters: dict[str, int | float] = {
            "n_estimators": n_estimators,
            "learning_rate": learning_rate,
            "max_depth": max_depth,
            "min_samples_leaf": min_samples_leaf,
            "subsample": subsample,
            "feature_fraction": feature_fraction,
            "reg_lambda": reg_lambda,
        }
        self.seed = seed
        self.regime_bounds_: tuple[float, float] | None = None
        self.regime_counts_: dict[int, int] = {}
        self.global_model_: GradientBoostingClassifier | None = None
        self.experts_: dict[int, GradientBoostingClassifier] = {}

    def _new_model(self, seed: int) -> GradientBoostingClassifier:
        return GradientBoostingClassifier(
            n_estimators=int(self.model_parameters["n_estimators"]),
            learning_rate=float(self.model_parameters["learning_rate"]),
            max_depth=int(self.model_parameters["max_depth"]),
            min_samples_leaf=int(self.model_parameters["min_samples_leaf"]),
            subsample=float(self.model_parameters["subsample"]),
            feature_fraction=float(self.model_parameters["feature_fraction"]),
            reg_lambda=float(self.model_parameters["reg_lambda"]),
            seed=seed,
        )

    def _validate_rows(self, features: Sequence[Sequence[float]]) -> None:
        if not features:
            raise ValueError("features must not be empty")
        width = len(features[0])
        if self.regime_feature_index >= width:
            raise ValueError("regime feature index is outside the feature matrix")
        for row in features:
            if len(row) != width:
                raise ValueError("feature rows have inconsistent widths")
            if not math.isfinite(float(row[self.regime_feature_index])):
                raise ValueError("regime feature contains a non-finite value")

    def _regime(self, value: float) -> int:
        if self.regime_bounds_ is None:
            raise ValueError("regime mixture has not been fitted")
        return _regime_from_bounds(value, self.regime_bounds_)

    def fit(
        self,
        features: Sequence[Sequence[float]],
        labels: Sequence[int],
    ) -> RegimeMixtureClassifier:
        self._validate_rows(features)
        if len(features) != len(labels):
            raise ValueError("features and labels have different row counts")

        regime_values = [float(row[self.regime_feature_index]) for row in features]
        lower = _quantile(regime_values, 1.0 / 3.0)
        upper = _quantile(regime_values, 2.0 / 3.0)
        if not lower < upper:
            raise ValueError("regime feature does not define three distinct quantile bands")
        bounds = (lower, upper)
        regime_indices = {
            regime_id: [
                index
                for index, value in enumerate(regime_values)
                if _regime_from_bounds(value, bounds) == regime_id
            ]
            for regime_id in REGIME_IDS
        }
        eligible_regimes = [
            regime_id
            for regime_id, indices in regime_indices.items()
            if len(indices) >= self.min_expert_samples
            and len({int(labels[index]) for index in indices}) >= 2
        ]
        if not eligible_regimes:
            raise InsufficientRegimeSamples("no regime expert has enough two-class training data")

        global_model = self._new_model(self.seed).fit(features, labels)
        experts: dict[int, GradientBoostingClassifier] = {}
        for regime_id in eligible_regimes:
            indices = regime_indices[regime_id]
            expert_labels = [int(labels[index]) for index in indices]
            expert_features = [features[index] for index in indices]
            experts[regime_id] = self._new_model(self.seed + regime_id + 1).fit(
                expert_features,
                expert_labels,
            )

        self.regime_bounds_ = bounds
        self.regime_counts_ = {
            regime_id: len(indices) for regime_id, indices in regime_indices.items()
        }
        self.global_model_ = global_model
        self.experts_ = experts
        return self

    def predict_proba(self, row: Sequence[float]) -> float:
        if self.global_model_ is None:
            raise ValueError("regime mixture has not been fitted")
        if self.regime_feature_index >= len(row):
            raise ValueError("regime feature index is outside the prediction row")
        if not all(math.isfinite(float(value)) for value in row):
            raise ValueError("prediction row contains a non-finite value")
        regime_id = self._regime(float(row[self.regime_feature_index]))
        global_probability = self.global_model_.predict_proba(row)
        expert = self.experts_.get(regime_id)
        if expert is None:
            return global_probability
        count = self.regime_counts_[regime_id]
        expert_weight = count / (count + self.shrinkage_samples)
        probability = (
            expert_weight * expert.predict_proba(row) + (1.0 - expert_weight) * global_probability
        )
        return max(PROBABILITY_EPSILON, min(1.0 - PROBABILITY_EPSILON, probability))

    def predict_proba_many(self, rows: Sequence[Sequence[float]]) -> list[float]:
        return [self.predict_proba(row) for row in rows]

    def to_dict(self) -> dict[str, Any]:
        if self.global_model_ is None or self.regime_bounds_ is None:
            raise ValueError("regime mixture has not been fitted")
        return {
            "algorithm": "regime_mixture_gbdt",
            "regime_feature_index": self.regime_feature_index,
            "regime_feature_name": self.regime_feature_name,
            "regime_bounds": list(self.regime_bounds_),
            "regime_counts": {
                str(regime_id): self.regime_counts_.get(regime_id, 0) for regime_id in REGIME_IDS
            },
            "min_expert_samples": self.min_expert_samples,
            "shrinkage_samples": self.shrinkage_samples,
            "model_parameters": dict(self.model_parameters),
            "seed": self.seed,
            "global_model": self.global_model_.to_dict(),
            "experts": {
                str(regime_id): model.to_dict()
                for regime_id, model in sorted(self.experts_.items())
            },
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> RegimeMixtureClassifier:
        if payload.get("algorithm") != "regime_mixture_gbdt":
            raise ValueError("serialized algorithm is not regime_mixture_gbdt")
        parameters = payload.get("model_parameters")
        if not isinstance(parameters, Mapping):
            raise ValueError("model_parameters is missing or invalid")
        model = cls(
            regime_feature_index=int(payload["regime_feature_index"]),
            regime_feature_name=str(payload["regime_feature_name"]),
            min_expert_samples=int(payload["min_expert_samples"]),
            shrinkage_samples=float(payload["shrinkage_samples"]),
            n_estimators=int(parameters["n_estimators"]),
            learning_rate=float(parameters["learning_rate"]),
            max_depth=int(parameters["max_depth"]),
            min_samples_leaf=int(parameters["min_samples_leaf"]),
            subsample=float(parameters["subsample"]),
            feature_fraction=float(parameters["feature_fraction"]),
            reg_lambda=float(parameters["reg_lambda"]),
            seed=int(payload["seed"]),
        )
        bounds = payload.get("regime_bounds")
        if not isinstance(bounds, list) or len(bounds) != 2:
            raise ValueError("regime_bounds is missing or invalid")
        lower, upper = float(bounds[0]), float(bounds[1])
        if not lower < upper:
            raise ValueError("regime_bounds must be strictly increasing")
        model.regime_bounds_ = (lower, upper)

        counts = payload.get("regime_counts")
        if not isinstance(counts, Mapping):
            raise ValueError("regime_counts is missing or invalid")
        model.regime_counts_ = {
            regime_id: int(counts.get(str(regime_id), 0)) for regime_id in REGIME_IDS
        }

        global_payload = payload.get("global_model")
        experts_payload = payload.get("experts")
        if not isinstance(global_payload, Mapping) or not isinstance(experts_payload, Mapping):
            raise ValueError("serialized global model or experts are invalid")
        model.global_model_ = GradientBoostingClassifier.from_dict(global_payload)
        model.experts_ = {}
        for raw_regime_id, expert_payload in experts_payload.items():
            regime_id = int(raw_regime_id)
            if regime_id not in REGIME_IDS or not isinstance(expert_payload, Mapping):
                raise ValueError("serialized expert is invalid")
            model.experts_[regime_id] = GradientBoostingClassifier.from_dict(expert_payload)
        if not model.experts_:
            raise ValueError("serialized regime mixture contains no experts")
        return model
