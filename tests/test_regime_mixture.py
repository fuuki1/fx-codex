"""Tests for the train-only volatility-regime mixture of experts."""

from __future__ import annotations

import json
import random

import numpy as np
import pytest

from fx_backtester.experiment_manifest import ModelCandidate
from fx_backtester.experiment_pipeline import (
    MODEL_FAMILY_KIND,
    _train_candidate,
)
from fx_backtester.failures import FailureReason, TypedFailure
from fx_backtester.regime_mixture import (
    MID_VOLATILITY,
    InsufficientRegimeSamples,
    RegimeMixtureClassifier,
)


def _regime_data(rows: int = 360) -> tuple[list[list[float]], list[int]]:
    rng = random.Random(17)
    features: list[list[float]] = []
    labels: list[int] = []
    for index in range(rows):
        volatility = index / (rows - 1)
        signal = rng.uniform(-1.0, 1.0)
        noise = rng.gauss(0.0, 0.25)
        if volatility < 1.0 / 3.0:
            target = signal + noise
        elif volatility < 2.0 / 3.0:
            target = -signal + noise
        else:
            target = 0.6 * signal + noise
        features.append([signal, volatility])
        labels.append(int(target > 0.0))
    return features, labels


def _model(seed: int = 31) -> RegimeMixtureClassifier:
    return RegimeMixtureClassifier(
        regime_feature_index=1,
        regime_feature_name="vol_24",
        min_expert_samples=50,
        shrinkage_samples=80.0,
        n_estimators=35,
        learning_rate=0.08,
        max_depth=2,
        min_samples_leaf=8,
        subsample=0.85,
        feature_fraction=1.0,
        reg_lambda=1.0,
        seed=seed,
    )


def test_regime_mixture_is_deterministic_and_serializable() -> None:
    features, labels = _regime_data()
    first = _model().fit(features, labels)
    second = _model().fit(features, labels)
    sample = features[::7]

    assert first.predict_proba_many(sample) == second.predict_proba_many(sample)
    assert set(first.experts_) == {0, 1, 2}
    assert sum(first.regime_counts_.values()) == len(features)

    payload = json.loads(json.dumps(first.to_dict()))
    restored = RegimeMixtureClassifier.from_dict(payload)
    assert restored.predict_proba_many(sample) == pytest.approx(
        first.predict_proba_many(sample),
        abs=1e-12,
    )


def test_single_class_regime_falls_back_to_global_model() -> None:
    features, labels = _regime_data()
    labels = [
        0 if 1.0 / 3.0 < row[1] <= 2.0 / 3.0 else label for row, label in zip(features, labels)
    ]
    fitted = _model().fit(features, labels)

    assert MID_VOLATILITY not in fitted.experts_
    mid_row = [0.25, 0.5]
    assert fitted.global_model_ is not None
    assert fitted.predict_proba(mid_row) == fitted.global_model_.predict_proba(mid_row)


def test_no_eligible_regime_expert_is_rejected() -> None:
    features = [[float(index), float(index)] for index in range(90)]
    labels = [0] * 30 + [1] * 30 + [0] * 30

    with pytest.raises(InsufficientRegimeSamples):
        _model().fit(features, labels)


def test_authoritative_pipeline_trains_regime_mixture_candidate() -> None:
    raw_features, raw_labels = _regime_data()
    features = np.asarray(raw_features, dtype=float)
    labels = np.asarray(raw_labels, dtype=int)
    candidate = ModelCandidate(
        candidate_id="regime-moe",
        family="regime_mixture_gbdt",
        hyperparameters={
            "regime_feature": "vol_24",
            "min_expert_samples": 50,
            "shrinkage_samples": 80.0,
            "n_estimators": 35,
            "learning_rate": 0.08,
            "max_depth": 2,
            "min_samples_leaf": 8,
            "subsample": 0.85,
            "feature_fraction": 1.0,
            "reg_lambda": 1.0,
            "long_threshold": 0.55,
            "short_threshold": 0.45,
        },
    )

    trained = _train_candidate(
        candidate,
        ("ret_1", "vol_24"),
        features,
        labels,
        seed=31,
    )

    assert MODEL_FAMILY_KIND[candidate.family] == "complex"
    assert trained.to_dict()["family"] == "regime_mixture_gbdt"
    assert len(trained.predict_probability(features[:5])) == 5


def test_authoritative_pipeline_rejects_missing_regime_feature() -> None:
    features, labels = _regime_data()
    candidate = ModelCandidate(
        candidate_id="regime-moe",
        family="regime_mixture_gbdt",
        hyperparameters={
            "regime_feature": "missing_volatility",
            "min_expert_samples": 50,
            "shrinkage_samples": 80.0,
            "n_estimators": 10,
            "learning_rate": 0.1,
            "max_depth": 2,
            "min_samples_leaf": 8,
            "subsample": 1.0,
            "feature_fraction": 1.0,
            "reg_lambda": 1.0,
            "long_threshold": 0.55,
            "short_threshold": 0.45,
        },
    )

    with pytest.raises(TypedFailure) as exc_info:
        _train_candidate(
            candidate,
            ("ret_1", "vol_24"),
            np.asarray(features, dtype=float),
            np.asarray(labels, dtype=int),
            seed=31,
        )

    assert exc_info.value.reason is FailureReason.INCOMPLETE
