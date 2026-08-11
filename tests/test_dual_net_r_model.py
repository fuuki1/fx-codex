"""Decision tests for the dual long/short expected-net-R model contract."""

from __future__ import annotations

import numpy as np
import pytest

from fx_backtester.experiment_pipeline import (
    LEGACY_CALIBRATED_DECISION_VERSION,
    PIPELINE_VERSION,
    _decision_probabilities,
    _model_decisions,
)
from fx_backtester.failures import TypedFailure


class _ExpectedReturnModel:
    def predict_expected_net_r(self, features: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        assert len(features) == 3
        return (
            np.asarray([0.12, -0.04, 0.01]),
            np.asarray([-0.08, 0.09, 0.00]),
        )

    def minimum_expected_net_r(self) -> float:
        return 0.02


def test_expected_net_r_selects_side_independently_of_direction_probability() -> None:
    model = _ExpectedReturnModel()
    features = np.asarray([[1.0], [2.0], [3.0]], dtype=float)
    probabilities = np.asarray([0.20, 0.80, 0.99], dtype=float)

    sides, expected = _model_decisions(
        model,  # type: ignore[arg-type]
        features,
        probabilities,
        long_threshold=0.55,
        short_threshold=0.45,
    )

    assert sides == ["long", "short", "abstain"]
    assert expected is not None


def test_v2_decisions_keep_the_raw_tune_score_space() -> None:
    raw = np.asarray([0.44, 0.50, 0.56])
    calibrated = np.asarray([0.25, 0.33, 0.42])

    assert np.array_equal(
        _decision_probabilities(raw, calibrated, pipeline_version=PIPELINE_VERSION),
        raw,
    )
    assert np.array_equal(
        _decision_probabilities(
            raw,
            calibrated,
            pipeline_version=LEGACY_CALIBRATED_DECISION_VERSION,
        ),
        calibrated,
    )


def test_unknown_pipeline_version_fails_closed() -> None:
    values = np.asarray([0.5])

    with pytest.raises(TypedFailure):
        _decision_probabilities(values, values, pipeline_version="unknown")
