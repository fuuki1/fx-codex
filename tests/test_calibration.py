from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from fx_backtester.calibration import (
    AbstentionPolicy,
    BetaCalibrator,
    CalibrationError,
    IsotonicCalibrator,
    PlattCalibrator,
    abstention_rate,
    calibration_metrics,
    select_calibrator,
    sigmoid,
    wilson_interval,
)


def _overconfident_sample(rows: int = 2400) -> tuple[np.ndarray, np.ndarray]:
    rng = np.random.default_rng(17)
    latent = rng.normal(0.0, 1.0, rows)
    true_probability = sigmoid(latent)
    labels = (rng.random(rows) < true_probability).astype(int)
    raw_probability = sigmoid(2.7 * latent + 0.3)
    return raw_probability, labels


def test_platt_calibration_improves_overconfident_probabilities_out_of_sample() -> None:
    raw, labels = _overconfident_sample()
    calibrator = PlattCalibrator().fit(raw[:1000], labels[:1000])

    before = calibration_metrics(labels[1000:], raw[1000:])
    after = calibration_metrics(labels[1000:], calibrator.predict(raw[1000:]))

    assert calibrator.fitted
    assert calibrator.scale < 1.0
    assert after.log_loss < before.log_loss
    assert after.brier < before.brier


def test_isotonic_predictions_are_monotone_and_beta_is_bounded() -> None:
    raw, labels = _overconfident_sample(rows=1000)
    grid = np.linspace(0.0, 1.0, 101)

    isotonic = IsotonicCalibrator().fit(raw[:600], labels[:600])
    beta = BetaCalibrator().fit(raw[:600], labels[:600])
    isotonic_values = isotonic.predict(grid)
    beta_values = beta.predict(grid)

    assert bool((np.diff(isotonic_values) >= 0).all())
    assert bool((np.diff(beta_values) >= 0).all())
    assert bool(((beta_values > 0) & (beta_values < 1)).all())


def test_calibrator_selection_requires_a_later_disjoint_window() -> None:
    raw, labels = _overconfident_sample(rows=900)
    times = pd.date_range("2024-01-01T00:00:00Z", periods=900, freq="h")

    selected = select_calibrator(
        raw[:400],
        labels[:400],
        times[:400],
        raw[400:700],
        labels[400:700],
        times[400:700],
    )

    assert selected.method in {"platt", "isotonic", "beta"}
    assert set(selected.all_selection_metrics) == {"platt", "isotonic", "beta"}
    assert selected.calibrator.fitted

    with pytest.raises(CalibrationError, match="overlap"):
        select_calibrator(
            raw[:400],
            labels[:400],
            times[:400],
            raw[399:700],
            labels[399:700],
            times[399:700],
        )


def test_calibration_metrics_reject_invalid_values_and_report_slope() -> None:
    raw, labels = _overconfident_sample(rows=600)
    report = calibration_metrics(labels, raw)
    assert report.rows == 600
    assert report.calibration_slope is not None
    assert 0 <= report.expected_calibration_error <= 1
    with pytest.raises(CalibrationError, match="finite"):
        calibration_metrics([0, 1], [0.2, float("nan")])


def test_hard_vetoes_cannot_be_overridden_by_high_probability() -> None:
    policy = AbstentionPolicy()
    data_veto = policy.decide(
        0.99,
        net_expected_r=2.0,
        uncertainty_interval=(0.90, 1.0),
        data_quality_vetoes=("stale_data",),
    )
    risk_veto = policy.decide(
        0.99,
        net_expected_r=2.0,
        uncertainty_interval=(0.90, 1.0),
        risk_vetoes=("daily_loss_breach",),
    )

    assert data_veto.action == risk_veto.action == "no_trade"
    assert data_veto.hard_vetoes == ("stale_data",)
    assert risk_veto.hard_vetoes == ("daily_loss_breach",)


def test_abstention_policy_trades_only_calibrated_positive_edge_with_low_uncertainty() -> None:
    policy = AbstentionPolicy(max_interval_width=0.2)
    long = policy.decide(
        0.72, net_expected_r=0.25, uncertainty_interval=(0.64, 0.78), calibrated=True
    )
    short = policy.decide(
        0.28, net_expected_r=0.20, uncertainty_interval=(0.20, 0.36), calibrated=True
    )
    uncertain = policy.decide(
        0.75, net_expected_r=0.30, uncertainty_interval=(0.45, 0.85), calibrated=True
    )
    unprofitable = policy.decide(
        0.75, net_expected_r=-0.01, uncertainty_interval=(0.70, 0.80), calibrated=True
    )

    assert long.action == "long"
    assert short.action == "short"
    assert uncertain.reason == "uncertainty_too_wide"
    assert unprofitable.reason == "non_positive_net_expectancy"
    assert abstention_rate([long, short, uncertain, unprofitable]) == pytest.approx(0.5)


def test_abstention_rejects_probability_outside_reported_uncertainty_interval() -> None:
    decision = AbstentionPolicy().decide(
        0.99,
        net_expected_r=0.5,
        uncertainty_interval=(0.10, 0.20),
        calibrated=True,
    )

    assert decision.action == "no_trade"
    assert decision.reason == "probability_outside_uncertainty_interval"


def test_wilson_interval_is_bounded_and_shrinks_with_more_observations() -> None:
    small = wilson_interval(7, 10)
    large = wilson_interval(700, 1000)
    assert 0 <= small[0] < small[1] <= 1
    assert large[1] - large[0] < small[1] - small[0]
